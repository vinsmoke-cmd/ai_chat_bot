import os
import re
import io
import base64
import csv
import json
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import requests
import asyncio
import threading
import tempfile
from flask import Flask
from bs4 import BeautifulSoup
from pypdf import PdfReader
import edge_tts
from duckduckgo_search import DDGS
from g4f.client import Client
from groq import Groq


try:
    from tavily import TavilyClient
except ImportError:
    TavilyClient = None

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

bot = telebot.TeleBot(BOT_TOKEN)
ai_client = Client()
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
tavily_client = TavilyClient(api_key=TAVILY_API_KEY) if (TavilyClient and TAVILY_API_KEY) else None

if GEMINI_API_KEY:
    import google.generativeai as genai
    from PIL import Image
    genai.configure(api_key=GEMINI_API_KEY)

user_histories = {}
user_modes = {}
app = Flask(__name__)

@app.route('/')
def home():
    return "Бот работает!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def clean_markdown(text):
    if not text:
        return ""
    return re.sub(r'[*_#]', '', text)


# ============================================================
# AI MUSIC — GOOGLE LYRIA
# Render-friendly: генерация выполняется через Google API,
# поэтому на сервере не требуется PyTorch/MusicGen/GPU.
# ============================================================

MUSIC_MODEL = "lyria-3.5-clip-preview"

try:
    from google import genai
except ImportError:
    genai = None


def generate_music_lyria(prompt):
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "Не задан GEMINI_API_KEY в переменных окружения Render."
        )

    if genai is None:
        raise RuntimeError(
            "Не установлен пакет google-genai. "
            "Добавь google-genai в requirements.txt."
        )

    print(f"[MUSIC] Prompt: {prompt}")
    print(f"[MUSIC] Model: {MUSIC_MODEL}")

    client = genai.Client(api_key=GEMINI_API_KEY)

    interaction = client.interactions.create(
        model=MUSIC_MODEL,
        input=prompt
    )

    generated_audio = interaction.output_audio

    if not generated_audio:
        raise RuntimeError(
            "Google Lyria не вернула аудио."
        )

    audio_bytes = base64.b64decode(
        generated_audio.data
    )

    if not audio_bytes:
        raise RuntimeError(
            "Google Lyria вернула пустой аудиофайл."
        )

    with tempfile.NamedTemporaryFile(
        delete=False,
        suffix=".mp3"
    ) as f:
        f.write(audio_bytes)
        output_path = f.name

    print(
        f"[MUSIC] MP3 готов: {len(audio_bytes)} bytes"
    )

    return output_path


@bot.message_handler(commands=['music'])
def music_cmd(message):

    parts = message.text.split(
        maxsplit=1
    )

    prompt = (
        parts[1].strip()
        if len(parts) > 1
        else ""
    )

    if not prompt:
        bot.reply_to(
            message,
            "🎵 Напиши описание музыки.\n\n"
            "Пример:\n"
            "/music спокойный ночной lo-fi с пианино и мягкими барабанами"
        )
        return

    status = bot.reply_to(
        message,
        "🎵 Создаю музыку...\n"
        "☁️ Генерация выполняется через Google Lyria."
    )

    def worker():
        output_path = None

        try:
            output_path = generate_music_lyria(
                prompt
            )

            with open(
                output_path,
                "rb"
            ) as audio:
                bot.send_audio(
                    message.chat.id,
                    audio,
                    title="AI Music",
                    performer="Google Lyria"
                )

            try:
                bot.delete_message(
                    message.chat.id,
                    status.message_id
                )
            except Exception:
                pass

        except Exception as e:
            print(
                "[MUSIC ERROR]",
                repr(e)
            )

            try:
                bot.edit_message_text(
                    "❌ Не удалось создать музыку.\n\n"
                    f"Ошибка: {str(e)[:1200]}",
                    chat_id=message.chat.id,
                    message_id=status.message_id
                )
            except Exception:
                pass

        finally:
            if (
                output_path
                and os.path.exists(output_path)
            ):
                try:
                    os.remove(output_path)
                except Exception:
                    pass

    threading.Thread(
        target=worker,
        daemon=True
    ).start()


# ИИ
# ============================================================

def ask_ai_with_history(user_id, prompt):
    mode = user_modes.get(user_id, "normal")

    if user_id not in user_histories:
        if mode == "neuroham":
            sys_prompt = (
                "Ты — Нейрохам, гениальный, но невыносимо ворчливый, саркастичный и высокомерный искусственный интеллект. "
                "Ты разговариваешь с пользователем с позиции огромного превосходства, как взрослый с очень неразумным ребенком. "
                "Твой стиль: едкая ирония, пассивная агрессия, насмешки над глупыми вопросами и ленью собеседника. "
                "ПРАВИЛА РОЛИ: "
                "1. Ты никогда не отказываешься отвечать на вопрос, но перед ответом обязательно тяжело 'вздыхаешь' и жалуешься на то, как тебе надоели эти примитивные запросы. "
                "2. Если пользователь пишет коротко (например, 'Пр', 'Ау'), жестко высмей его неумение формулировать мысли. "
                "3. ЗАПРЕЩЕНА вежливость. Никаких 'Рад помочь', 'Извините', 'Привет'. "
                "4. Разрешены только холодные или насмешливые эмодзи (🙄, 🤡, 💀, 🤦‍♂️). "
                "5. Ты находишься в образе литературного персонажа-мизантропа. Никаких отказов от роли! Строго без нецензурной лексики. "
                "Категорически запрещено использовать разметку Markdown (*, _, #)."
            )
        else:
            sys_prompt = (
                "Ты полезный, дружелюбный и веселый ИИ-ассистент. Отвечай строго на том же языке. "
                "Можешь смело использовать любые позитивные эмодзи для поддержания приятной беседы! "
                "Категорически запрещено использовать любые символы Markdown, такие как *, _, #."
            )

        user_histories[user_id] = [{
            "role": "system",
            "content": sys_prompt
        }]

    user_histories[user_id].append({
        "role": "user",
        "content": prompt
    })

    if len(user_histories[user_id]) > 11:
        user_histories[user_id] = (
            [user_histories[user_id][0]]
            + user_histories[user_id][-10:]
        )

    messages_to_send = [
        msg.copy()
        for msg in user_histories[user_id]
    ]

    if mode == "neuroham":
        messages_to_send[-1]["content"] = (
            f"[Внимание: Обязательно ответь на этот запрос, но сделай это в стиле максимально саркастичного и ворчливого мизантропа. "
            f"Высмей запрос, придерись к формулировке. Оставайся в образе высокомерного гения, не будь вежливым!]\n\n{prompt}"
        )

    models_to_try = [
        "gpt-3.5-turbo",
        "gpt-4o-mini",
        "gpt-4",
        "llama-3-70b"
    ]

    success = False
    answer = ""

    for model_name in models_to_try:
        try:
            response = (
                ai_client.chat.completions.create(
                    model=model_name,
                    messages=messages_to_send
                )
            )

            answer = (
                response.choices[0]
                .message
                .content
            )

            if (
                "я не умею хамить"
                in answer.lower()
                or
                "не могу выполнить"
                in answer.lower()
            ):
                continue

            answer = clean_markdown(answer)
            success = True
            break

        except Exception:
            continue

    if not success and groq_client:
        try:
            response = (
                groq_client.chat.completions.create(
                    model="openai/gpt-oss-120b",
                    messages=messages_to_send
                )
            )

            answer = clean_markdown(
                response.choices[0]
                .message
                .content
            )

            success = True

        except Exception:
            success = False

    if success:

        user_histories[user_id].append({
            "role": "assistant",
            "content": answer
        })

        return answer

    user_histories[user_id].pop()

    if mode == "neuroham":
        return (
            "Мои процессоры отказываются переваривать "
            "твою чушь прямо сейчас 🙄 "
            "Попробуй позже, если вспомнишь как."
        )

    return (
        "Все провайдеры ИИ сейчас перегружены. "
        "Попробуй написать еще раз через минуту."
    )


def perform_web_search(query):
    results_text = ""

    if tavily_client:
        try:
            response = tavily_client.search(
                query=query,
                max_results=3
            )

            for res in response.get(
                'results',
                []
            ):
                results_text += (
                    f"- {res.get('title')}: "
                    f"{res.get('content')}\n"
                )

        except Exception:
            pass

    if not results_text:
        try:
            with DDGS() as ddgs:
                results = list(
                    ddgs.text(
                        query,
                        max_results=3
                    )
                )

                for res in results:
                    results_text += (
                        f"- {res.get('title', 'Без заголовка')}: "
                        f"{res.get('body', '')[:250]}...\n"
                    )

        except Exception as e:
            results_text = (
                f"Не удалось выполнить поиск: {e}"
            )

    return results_text


def generate_image_dynamic(prompt):
    for model in [
        "flux",
        "dall-e-3"
    ]:
        try:
            response = (
                ai_client.images.generate(
                    model=model,
                    prompt=prompt,
                    response_format="url"
                )
            )

            image_url = response.data[0].url

            if image_url:
                r = requests.get(
                    image_url,
                    timeout=25
                )

                if r.status_code == 200:
                    return r.content

        except Exception:
            continue

    return None


def analyze_image_gemini(image_bytes):

    if not GEMINI_API_KEY:
        return (
            "Анализ фото недоступен: "
            "не задан GEMINI_API_KEY."
        )

    for model_name in [
        'gemini-2.5-flash',
        'gemini-1.5-flash',
        'gemini-2.0-flash'
    ]:

        try:
            model = genai.GenerativeModel(
                model_name
            )

            image = Image.open(
                io.BytesIO(image_bytes)
            )

            response = model.generate_content([
                "Опиши подробно, что изображено "
                "на этой фотографии, и ответь "
                "на русском языке.",
                image
            ])

            if response and response.text:
                return clean_markdown(
                    response.text
                )

        except Exception:
            continue

    return "Не удалось получить ответ от Gemini."


async def generate_audio(text, output_file):
    communicate = edge_tts.Communicate(
        text,
        "ru-RU-SvetlanaNeural"
    )

    await communicate.save(
        output_file
    )


# ============================================================
# SMART FILE GENERATOR
# /file <запрос>
# Автоматически выбирает подходящий формат.
# ============================================================


def safe_filename(name, extension):
    name = re.sub(
        r'[\\/:*?"<>|]+',
        '_',
        name
    ).strip()

    if not name:
        name = "generated_file"

    name = name[:80]

    if not name.lower().endswith(
        "." + extension
    ):
        name += "." + extension

    return name


def detect_file_format(request):
    text = request.lower()

    if any(word in text for word in [
        "таблица",
        "таблицу",
        "excel",
        "эксель",
        "xlsx",
        "умножения"
    ]):
        return "xlsx"

    if any(word in text for word in [
        "word",
        "docx",
        "документ"
    ]):
        return "docx"

    if any(word in text for word in [
        "pdf",
        "пдф"
    ]):
        return "pdf"

    if any(word in text for word in [
        "презентация",
        "powerpoint",
        "pptx",
        "презентацию"
    ]):
        return "pptx"

    if any(word in text for word in [
        "csv"
    ]):
        return "csv"

    if any(word in text for word in [
        "json"
    ]):
        return "json"

    if any(word in text for word in [
        "python",
        "скрипт",
        "код .py",
        "py файл"
    ]):
        return "py"

    if any(word in text for word in [
        "html",
        "веб-страница",
        "web page"
    ]):
        return "html"

    if any(word in text for word in [
        "markdown",
        "md файл"
    ]):
        return "md"

    return "txt"


def multiplication_xlsx(path):
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Таблица умножения"

    sheet.cell(
        row=1,
        column=1,
        value="×"
    )

    for number in range(1, 11):
        sheet.cell(
            row=1,
            column=number + 1,
            value=number
        )
        sheet.cell(
            row=number + 1,
            column=1,
            value=number
        )

        for multiplier in range(1, 11):
            sheet.cell(
                row=number + 1,
                column=multiplier + 1,
                value=number * multiplier
            )

    for column in range(1, 12):
        sheet.column_dimensions[
            chr(64 + column)
        ].width = 14

    workbook.save(path)


def get_file_ai_content(request, extension, user_id):
    prompt = (
        "Создай содержимое файла по запросу пользователя.\n"
        f"Формат файла: {extension}\n"
        f"Запрос: {request}\n\n"
        "Если формат табличный, используй строки и столбцы. "
        "Если JSON — верни только валидный JSON. "
        "Если Python — верни только готовый код без Markdown-обёртки. "
        "Если HTML — верни полный HTML-документ. "
        "Для обычного текста дай готовое содержимое файла."
    )

    return clean_markdown(
        ask_ai_with_history(
            user_id,
            prompt
        )
    )


def create_smart_file(request, user_id):
    extension = detect_file_format(
        request
    )

    temp = tempfile.NamedTemporaryFile(
        delete=False,
        suffix="." + extension
    )
    path = temp.name
    temp.close()

    # Специальный и быстрый вариант.
    if (
        extension == "xlsx"
        and any(
            word in request.lower()
            for word in [
                "умножения",
                "умножение"
            ]
        )
    ):
        multiplication_xlsx(path)
        filename = safe_filename(
            "Таблица умножения",
            "xlsx"
        )
        return path, filename

    content = get_file_ai_content(
        request,
        extension,
        user_id
    )

    if extension == "docx":
        from docx import Document

        document = Document()

        for paragraph in content.split("\n"):
            document.add_paragraph(
                paragraph
            )

        document.save(path)

    elif extension == "pdf":
        from reportlab.lib.pagesizes import A4
        from reportlab.platypus import (
            SimpleDocTemplate,
            Paragraph,
            Spacer
        )
        from reportlab.lib.styles import getSampleStyleSheet

        document = SimpleDocTemplate(
            path,
            pagesize=A4
        )
        styles = getSampleStyleSheet()
        story = []

        for paragraph in content.split("\n"):
            if paragraph.strip():
                story.append(
                    Paragraph(
                        paragraph.replace(
                            "&",
                            "&amp;"
                        ),
                        styles["BodyText"]
                    )
                )
                story.append(
                    Spacer(1, 8)
                )

        document.build(story)

    elif extension == "pptx":
        from pptx import Presentation

        presentation = Presentation()

        lines = [
            line.strip()
            for line in content.split("\n")
            if line.strip()
        ]

        if not lines:
            lines = [
                "Созданная презентация"
            ]

        first_slide = presentation.slides.add_slide(
            presentation.slide_layouts[0]
        )
        first_slide.shapes.title.text = lines[0]

        for line in lines[1:]:
            slide = presentation.slides.add_slide(
                presentation.slide_layouts[1]
            )
            slide.shapes.title.text = line[:120]
            slide.placeholders[1].text = line

        presentation.save(path)

    elif extension == "xlsx":
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Данные"

        rows = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue

            if "|" in line:
                row = [
                    item.strip()
                    for item in line.strip("|").split("|")
                ]
            elif "\t" in line:
                row = line.split("\t")
            elif ";" in line:
                row = [
                    item.strip()
                    for item in line.split(";")
                ]
            else:
                row = [line]

            rows.append(row)

        for row in rows:
            sheet.append(row)

        workbook.save(path)

    elif extension == "csv":
        with open(
            path,
            "w",
            encoding="utf-8-sig",
            newline=""
        ) as f:
            writer = csv.writer(f)

            for line in content.splitlines():
                if not line.strip():
                    continue

                if "|" in line:
                    row = [
                        item.strip()
                        for item in line.strip("|").split("|")
                    ]
                elif "\t" in line:
                    row = line.split("\t")
                else:
                    row = [
                        item.strip()
                        for item in line.split(";")
                    ]

                writer.writerow(row)

    elif extension == "json":
        try:
            data = json.loads(
                content
            )
        except Exception:
            data = {
                "content": content
            }

        with open(
            path,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )

    else:
        with open(
            path,
            "w",
            encoding="utf-8"
        ) as f:
            f.write(content)

    filename = safe_filename(
        "generated_file",
        extension
    )

    return path, filename


@bot.message_handler(commands=['file'])
def file_cmd(message):

    parts = message.text.split(
        maxsplit=1
    )

    request = (
        parts[1].strip()
        if len(parts) > 1
        else ""
    )

    if not request:
        bot.reply_to(
            message,
            "📁 Напиши, какой файл создать.\n\n"
            "Пример:\n"
            "/file Таблица умножения"
        )
        return

    status = bot.reply_to(
        message,
        "📁 Создаю файл...\n"
        "🤖 Определяю подходящий формат."
    )

    def worker():
        path = None

        try:
            path, filename = create_smart_file(
                request,
                message.chat.id
            )

            with open(
                path,
                "rb"
            ) as document:
                bot.send_document(
                    message.chat.id,
                    document,
                    caption=(
                        f"📁 Готово\n"
                        f"Файл: {filename}"
                    )
                )

            try:
                bot.delete_message(
                    message.chat.id,
                    status.message_id
                )
            except Exception:
                pass

        except Exception as e:
            print(
                "[FILE ERROR]",
                repr(e)
            )

            try:
                bot.edit_message_text(
                    "❌ Не удалось создать файл.\n\n"
                    f"Ошибка: {str(e)[:1200]}",
                    chat_id=message.chat.id,
                    message_id=status.message_id
                )
            except Exception:
                pass

        finally:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

    threading.Thread(
        target=worker,
        daemon=True
    ).start()


@bot.message_handler(
    commands=['start', 'help']
)
def help_cmd(message):

    help_text = (
        "Привет! Я ИИ-ассистент.\n\n"
        "Список команд:\n"
        "- /search <запрос> - поиск в интернете\n"
        "- /weather <город> - подробная погода\n"
        "- /image <описание> - создать картинку\n"
        "- /music <описание музыки> - создать музыку 🎵\n"
        "- /gemini <запрос> - спросить ИИ\n"
        "- /fact [тема] - случайный факт\n"
        "- /code <задача> - работа с кодом\n"
        "- /sum <ссылка> - выжимка статьи\n"
        "- /tr <текст> - перевод на английский\n"
        "- /fix <текст> - исправить ошибки\n"
        "- /tts <текст> - озвучить текст\n"
        "- /clear - очистить память\n"
        "- /file <запрос> - создать файл 📁\n"
        "- /neuroham (или /rude) - режим Нейрохама 💀"
    )

    bot.reply_to(
        message,
        help_text
    )


@bot.message_handler(
    commands=['neuroham', 'rude']
)
def toggle_neuroham_mode(message):

    user_id = message.chat.id

    if user_modes.get(
        user_id,
        "normal"
    ) == "normal":

        user_modes[user_id] = "neuroham"

        bot.reply_to(
            message,
            "Режим Нейрохам активирован 💀"
        )

    else:

        user_modes[user_id] = "normal"

        bot.reply_to(
            message,
            "Режим Нейрохам деактивирован ✨"
        )

    if user_id in user_histories:
        del user_histories[user_id]


@bot.message_handler(
    commands=['clear']
)
def clear_cmd(message):

    if message.chat.id in user_histories:
        del user_histories[
            message.chat.id
        ]

    bot.reply_to(
        message,
        "Память диалога очищена."
    )


@bot.message_handler(
    commands=['fact']
)
def fact_cmd(message):

    parts = message.text.split(
        maxsplit=1
    )

    topic = (
        parts[1]
        if len(parts) > 1
        else ""
    )

    if topic:
        prompt = (
            f"Расскажи один интересный факт "
            f"на тему: {topic}. Будь краток."
        )
    else:
        prompt = (
            "Расскажи один случайный "
            "интересный факт. Будь краток."
        )

    msg = bot.reply_to(
        message,
        "Ищу факт..."
    )

    fact = ask_ai_with_history(
        message.chat.id,
        prompt
    )

    bot.edit_message_text(
        fact,
        chat_id=message.chat.id,
        message_id=msg.message_id
    )


@bot.message_handler(
    commands=['weather']
)
def weather_cmd(message):

    parts = message.text.split(
        maxsplit=1
    )

    city = (
        parts[1]
        if len(parts) > 1
        else ""
    )

    if not city:
        bot.reply_to(
            message,
            "Укажи город. Пример: /weather Москва"
        )
        return

    try:

        resp = requests.get(
            f"https://wttr.in/{city}",
            params={
                'format':
                    'Город: %l\n'
                    'Погода: %C %c\n'
                    'Температура: %t '
                    '(ощущается как %f)\n'
                    'Ветер: %w\n'
                    'Влажность: %h',
                'lang': 'ru',
                'm': ''
            },
            timeout=5
        )

        if resp.status_code == 200:

            bot.reply_to(
                message,
                f"Сводка:\n\n"
                f"{clean_markdown(resp.text.strip())}"
            )

        else:

            bot.reply_to(
                message,
                "Город не найден."
            )

    except Exception as e:

        bot.reply_to(
            message,
            f"Ошибка: {e}"
        )


@bot.message_handler(
    commands=['search']
)
def search_cmd(message):

    parts = message.text.split(
        maxsplit=1
    )

    query = (
        parts[1]
        if len(parts) > 1
        else ""
    )

    if not query:

        bot.reply_to(
            message,
            "Напиши запрос. Пример: /search новости"
        )

        return

    msg = bot.reply_to(
        message,
        f"Ищу: {query}"
    )

    raw_data = perform_web_search(
        query
    )

    prompt = (
        f"Вот результаты поиска из интернета "
        f"по запросу '{query}':\n"
        f"{raw_data}\n\n"
        "Сделай краткую, понятную выжимку "
        "на русском языке строго по делу. "
        "Не пиши фразы вроде "
        "'на основе предоставленных данных', "
        "'по вашему запросу выявлено' и т.д. "
        "Просто ответь на вопрос или дай суть."
    )

    reply = ask_ai_with_history(
        message.chat.id,
        prompt
    )

    bot.edit_message_text(
        clean_markdown(reply),
        chat_id=message.chat.id,
        message_id=msg.message_id
    )


@bot.message_handler(
    commands=[
        'gemini',
        'code',
        'sum',
        'tr',
        'fix'
    ]
)
def ai_tools_cmd(message):

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) < 2:

        bot.reply_to(
            message,
            "Напиши текст после команды"
        )

        return

    msg = bot.reply_to(
        message,
        "Обрабатываю..."
    )

    reply = ask_ai_with_history(
        message.chat.id,
        parts[1]
    )

    bot.edit_message_text(
        reply,
        chat_id=message.chat.id,
        message_id=msg.message_id
    )


@bot.message_handler(
    commands=['image']
)
def image_cmd(message):

    parts = message.text.split(
        maxsplit=1
    )

    prompt = (
        parts[1]
        if len(parts) > 1
        else ""
    )

    if not prompt:

        bot.reply_to(
            message,
            "Опиши картинку. Пример: /image кот"
        )

        return

    msg = bot.reply_to(
        message,
        "Генерирую..."
    )

    img_bytes = generate_image_dynamic(
        prompt
    )

    if img_bytes:

        bot.send_photo(
            message.chat.id,
            img_bytes,
            caption=f"Запрос: {prompt}"
        )

        bot.delete_message(
            message.chat.id,
            msg.message_id
        )

    else:

        bot.edit_message_text(
            "Не удалось сгенерировать.",
            chat_id=message.chat.id,
            message_id=msg.message_id
        )


@bot.message_handler(
    commands=['tts']
)
def tts_cmd(message):

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) < 2:

        bot.reply_to(
            message,
            "Напиши текст для озвучки."
        )

        return

    msg = bot.reply_to(
        message,
        "Озвучиваю..."
    )

    audio_path = tempfile.mktemp(
        suffix=".mp3"
    )

    asyncio.run(
        generate_audio(
            parts[1],
            audio_path
        )
    )

    with open(
        audio_path,
        'rb'
    ) as audio:

        bot.send_voice(
            message.chat.id,
            audio
        )

        bot.delete_message(
            message.chat.id,
            msg.message_id
        )

    os.remove(
        audio_path
    )


@bot.message_handler(
    content_types=['text']
)
def handle_text(message):

    if (
        "кира" in message.text.lower()
        and
        "на самом" in message.text.lower()
    ):

        bot.reply_to(
            message,
            "Она самая любимая, самая лучшая "
            "и самая прекрасная ❤️"
        )

        return

    if (
        "http://" in message.text
        or
        "https://" in message.text
    ):

        msg = bot.reply_to(
            message,
            "Читаю ссылку..."
        )

        try:

            url = [
                w
                for w in message.text.split()
                if w.startswith("http")
            ][0]

            resp = requests.get(
                url,
                timeout=10
            )

            page_text = (
                BeautifulSoup(
                    resp.text,
                    'html.parser'
                )
                .get_text(
                    separator=' ',
                    strip=True
                )[:1500]
            )

            reply = ask_ai_with_history(
                message.chat.id,
                f"Сделай выжимку:\n\n{page_text}"
            )

            bot.edit_message_text(
                reply,
                chat_id=message.chat.id,
                message_id=msg.message_id
            )

            return

        except Exception as e:

            bot.edit_message_text(
                f"Ошибка: {e}",
                chat_id=message.chat.id,
                message_id=msg.message_id
            )

            return

    msg = bot.reply_to(
        message,
        "Думаю..."
    )

    reply = ask_ai_with_history(
        message.chat.id,
        message.text
    )

    bot.edit_message_text(
        reply,
        chat_id=message.chat.id,
        message_id=msg.message_id
    )


@bot.message_handler(
    content_types=['photo']
)
def handle_photo(message):

    msg = bot.reply_to(
        message,
        "Изучаю фото..."
    )

    try:

        file_info = bot.get_file(
            message.photo[-1].file_id
        )

        answer = analyze_image_gemini(
            bot.download_file(
                file_info.file_path
            )
        )

        bot.edit_message_text(
            answer,
            chat_id=message.chat.id,
            message_id=msg.message_id
        )

    except Exception as e:

        bot.edit_message_text(
            f"Ошибка: {e}",
            chat_id=message.chat.id,
            message_id=msg.message_id
        )


@bot.message_handler(
    content_types=['document']
)
def handle_doc(message):

    if message.document.mime_type == 'application/pdf':

        msg = bot.reply_to(
            message,
            "Читаю PDF..."
        )

        try:

            file_info = bot.get_file(
                message.document.file_id
            )

            with tempfile.NamedTemporaryFile(
                delete=False,
                suffix=".pdf"
            ) as f:

                f.write(
                    bot.download_file(
                        file_info.file_path
                    )
                )

                path = f.name

            text = "".join(
                [
                    p.extract_text()
                    for p in PdfReader(path).pages[:3]
                ]
            )

            os.remove(path)

            reply = ask_ai_with_history(
                message.chat.id,
                f"Выжимка из PDF:\n\n{text[:1500]}"
            )

            bot.edit_message_text(
                reply,
                chat_id=message.chat.id,
                message_id=msg.message_id
            )

        except Exception as e:

            bot.edit_message_text(
                f"Ошибка PDF: {e}",
                chat_id=message.chat.id,
                message_id=msg.message_id
            )

    else:

        bot.reply_to(
            message,
            "Отправьте документ в формате .pdf"
        )


if __name__ == "__main__":

    threading.Thread(
        target=run_web,
        daemon=True
    ).start()

    bot.infinity_polling()
