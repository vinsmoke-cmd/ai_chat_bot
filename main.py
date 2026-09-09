import os
import re
import io
import html
import asyncio
import threading
import tempfile
import time

import telebot
import requests

from flask import Flask
from bs4 import BeautifulSoup
from pypdf import PdfReader
import edge_tts

from g4f.client import Client
from groq import Groq


# ============================================================
# ENVIRONMENT
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")


# ============================================================
# ПРОВЕРКА BOT TOKEN
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError("❌ Не задан BOT_TOKEN")


# ============================================================
# TELEGRAM
# ============================================================

bot = telebot.TeleBot(BOT_TOKEN)


# ============================================================
# AI CLIENTS
# ============================================================

ai_client = Client()

groq_client = (
    Groq(api_key=GROQ_API_KEY)
    if GROQ_API_KEY
    else None
)


# ============================================================
# REPLICATE
# ============================================================

try:
    import replicate
except ImportError:
    replicate = None


# ============================================================
# TAVILY
# ============================================================

try:
    from tavily import TavilyClient
except ImportError:
    TavilyClient = None


tavily_client = (
    TavilyClient(api_key=TAVILY_API_KEY)
    if TavilyClient and TAVILY_API_KEY
    else None
)


# ============================================================
# GEMINI
# ============================================================

if GEMINI_API_KEY:

    try:
        import google.generativeai as genai
        from PIL import Image

        genai.configure(
            api_key=GEMINI_API_KEY
        )

        print("✅ Gemini подключён")

    except Exception as e:

        print(f"⚠️ Gemini недоступен: {e}")

        genai = None
        Image = None

else:

    genai = None
    Image = None


# ============================================================
# ПАМЯТЬ
# ============================================================

user_histories = {}
user_modes = {}


# ============================================================
# НАСТРОЙКИ
# ============================================================

TELEGRAM_MESSAGE_LIMIT = 4096
AI_MAX_RESPONSE_LENGTH = 40000


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "Бот работает!"


def run_web():

    port = int(
        os.environ.get(
            "PORT",
            8080
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )


# ============================================================
# CLEAN MARKDOWN
# ============================================================

def clean_markdown(text):

    if not text:
        return ""

    text = str(text)

    code_blocks = []

    def protect_code(match):

        code_blocks.append(
            match.group(0)
        )

        return (
            f"§CODEBLOCK"
            f"{len(code_blocks) - 1}"
            f"§"
        )

    text = re.sub(
        r"```(?:[a-zA-Z0-9_+#.-]+)?"
        r"\s*\n?.*?```",
        protect_code,
        text,
        flags=re.DOTALL
    )

    text = re.sub(
        r"(?m)^\s{0,3}#{1,6}\s*",
        "",
        text
    )

    text = re.sub(
        r"\*\*(.*?)\*\*",
        r"\1",
        text,
        flags=re.DOTALL
    )

    text = re.sub(
        r"__(.*?)__",
        r"\1",
        text,
        flags=re.DOTALL
    )

    text = re.sub(
        r"\*(.*?)\*",
        r"\1",
        text,
        flags=re.DOTALL
    )

    text = re.sub(
        r"_(.*?)_",
        r"\1",
        text,
        flags=re.DOTALL
    )

    text = re.sub(
        r"~~(.*?)~~",
        r"\1",
        text,
        flags=re.DOTALL
    )

    text = re.sub(
        r"\[([^\]]+)\]\([^)]+\)",
        r"\1",
        text
    )

    text = re.sub(
        r"(?m)^\s*>\s?",
        "",
        text
    )

    text = re.sub(
        r"(?m)^\s*[-*+]\s+",
        "• ",
        text
    )

    text = re.sub(
        r"`([^`]+)`",
        r"\1",
        text
    )

    text = re.sub(
        r"[*_#~]",
        "",
        text
    )

    for index, code_block in enumerate(code_blocks):

        text = text.replace(
            f"§CODEBLOCK{index}§",
            code_block
        )

    return text.strip()


# ============================================================
# SPLIT LONG MESSAGE
# ============================================================

def split_long_message(
    text,
    max_length=TELEGRAM_MESSAGE_LIMIT - 100
):

    if not text:
        return [""]

    text = str(text)

    if len(text) <= max_length:
        return [text]

    parts = []

    while len(text) > max_length:

        split_pos = text.rfind(
            "\n",
            0,
            max_length
        )

        if split_pos < max_length // 2:

            split_pos = text.rfind(
                " ",
                0,
                max_length
            )

        if split_pos <= 0:
            split_pos = max_length

        part = text[:split_pos].strip()

        if part:
            parts.append(part)

        text = text[split_pos:].strip()

    if text:
        parts.append(text)

    return parts


# ============================================================
# EXTRACT CODE BLOCKS
# ============================================================

def extract_code_blocks(text):

    if not text:
        return []

    text = str(text)

    pattern = (
        r"```"
        r"(?:([a-zA-Z0-9_+#.-]+))?"
        r"\s*\n?"
        r"(.*?)"
        r"```"
    )

    matches = list(
        re.finditer(
            pattern,
            text,
            flags=re.DOTALL
        )
    )

    if not matches:

        return [
            {
                "type": "text",
                "content": text
            }
        ]

    parts = []
    last_end = 0

    for match in matches:

        before = text[
            last_end:match.start()
        ]

        if before.strip():

            parts.append(
                {
                    "type": "text",
                    "content": before.strip()
                }
            )

        language = (
            match.group(1)
            or ""
        ).strip()

        code = (
            match.group(2)
            or ""
        ).strip("\n")

        parts.append(
            {
                "type": "code",
                "language": language,
                "content": code
            }
        )

        last_end = match.end()

    after = text[last_end:]

    if after.strip():

        parts.append(
            {
                "type": "text",
                "content": after.strip()
            }
        )

    return parts


# ============================================================
# SEND CODE
# ============================================================

def send_code_block(
    chat_id,
    code,
    language=""
):

    if not code:
        return

    safe_code = html.escape(
        code,
        quote=False
    )

    formatted = (
        "<pre><code>"
        + safe_code
        + "</code></pre>"
    )

    try:

        bot.send_message(
            chat_id,
            formatted,
            parse_mode="HTML"
        )

    except Exception as e:

        print(
            f"⚠️ Ошибка отправки кода: {e}"
        )

        try:
            bot.send_message(
                chat_id,
                code
            )
        except Exception:
            pass


# ============================================================
# SEND AI RESPONSE
# ============================================================

def send_ai_response(
    chat_id,
    text
):

    if not text:
        return

    text = str(text)

    if len(text) > AI_MAX_RESPONSE_LENGTH:

        text = (
            text[:AI_MAX_RESPONSE_LENGTH]
            + "\n\n"
            "[Ответ автоматически сокращён.]"
        )

    text = clean_markdown(text)

    parts = extract_code_blocks(text)

    for part in parts:

        content = part.get(
            "content",
            ""
        )

        if not content:
            continue

        if part["type"] == "code":

            code_parts = split_long_message(
                content,
                3500
            )

            for code_part in code_parts:

                send_code_block(
                    chat_id,
                    code_part,
                    part.get(
                        "language",
                        ""
                    )
                )

        else:

            text_parts = split_long_message(
                content
            )

            for text_part in text_parts:

                try:

                    bot.send_message(
                        chat_id,
                        text_part
                    )

                except Exception as e:

                    print(
                        f"⚠️ Ошибка отправки: {e}"
                    )


# ============================================================
# EDIT OR SEND
# ============================================================

def edit_or_send_long(
    chat_id,
    message_id,
    text
):

    if not text:
        text = "Пустой ответ."

    text = str(text)

    if (
        "```" not in text
        and len(text)
        <= TELEGRAM_MESSAGE_LIMIT - 100
    ):

        try:

            bot.edit_message_text(
                clean_markdown(text),
                chat_id=chat_id,
                message_id=message_id
            )

            return

        except Exception as e:

            print(
                f"⚠️ Не удалось изменить сообщение: {e}"
            )

    try:

        bot.delete_message(
            chat_id,
            message_id
        )

    except Exception:
        pass

    send_ai_response(
        chat_id,
        text
    )


# ============================================================
# AI WITH HISTORY
# ============================================================

def ask_ai_with_history(
    user_id,
    prompt
):

    mode = user_modes.get(
        user_id,
        "normal"
    )

    if user_id not in user_histories:

        if mode == "neuroham":

            sys_prompt = (
                "Ты — Нейрохам, гениальный, "
                "но ворчливый и саркастичный "
                "искусственный интеллект. "
                "Без нецензурной лексики. "
                "Обычный текст без Markdown. "
                "Код только в кодовых блоках."
            )

        else:

            sys_prompt = (
                "Ты полезный, дружелюбный и умный "
                "ИИ-ассистент. "
                "Отвечай на языке пользователя. "
                "Не используй Markdown в обычном тексте. "
                "Если есть программный код, "
                "используй отдельный кодовый блок."
            )

        user_histories[user_id] = [
            {
                "role": "system",
                "content": sys_prompt
            }
        ]

    coding_keywords = [
        "код",
        "кодинг",
        "программ",
        "python",
        "javascript",
        "typescript",
        "java",
        "c++",
        "c#",
        "php",
        "html",
        "css",
        "sql",
        "bash",
        "telegram bot",
        "telegram бот",
        "бот",
        "api",
        "sdk",
        "функция",
        "класс",
        "метод",
        "библиотек",
        "скрипт",
        "исправь",
        "исправить",
        "ошибка",
        "перепиши",
        "переделай",
        "добавь функцию",
        "добавь код",
        "сделай код",
        "напиши код",
        "полный код",
        "готовый код",
        "source code",
        "debug",
        "stack trace",
        "exception",
        "import",
        "pip",
        "npm",
        "json",
        "regex",
        "база данных"
    ]

    prompt_lower = str(prompt).lower()

    is_coding_request = any(
        keyword in prompt_lower
        for keyword in coding_keywords
    )

    if is_coding_request:

        effective_prompt = (
            "ИНСТРУКЦИИ ДЛЯ ПРОГРАММИРОВАНИЯ:\n\n"
            "Отвечай максимально практически.\n"
            "Если нужен код — давай полноценный рабочий код.\n"
            "Если пользователь просит полный код — "
            "не сокращай его и не используй «остальной код» "
            "или «...». \n"
            "Учитывай импорты, зависимости, ENV, "
            "функции и обработчики.\n"
            "Код помещай в отдельные блоки.\n\n"
            "ЗАПРОС:\n"
            + str(prompt)
        )

    else:

        effective_prompt = str(prompt)

    user_histories[user_id].append(
        {
            "role": "user",
            "content": effective_prompt
        }
    )

    if len(user_histories[user_id]) > 21:

        user_histories[user_id] = (
            [user_histories[user_id][0]]
            + user_histories[user_id][-20:]
        )

    messages = [
        msg.copy()
        for msg in user_histories[user_id]
    ]

    if mode == "neuroham":

        messages[-1]["content"] = (
            "[Отвечай саркастично, "
            "но без мата. "
            "Обычный текст без Markdown. "
            "Код — только кодовыми блоками.]\n\n"
            + messages[-1]["content"]
        )

    models = [
        "gpt-3.5-turbo",
        "gpt-4o-mini",
        "gpt-4",
        "llama-3-70b"
    ]

    answer = ""
    success = False

    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"🤖 Новый запрос от {user_id}")
    print("🔄 Запускаю G4F...")

    for model_name in models:

        try:

            print(
                f"🔄 G4F → {model_name}"
            )

            response = (
                ai_client
                .chat
                .completions
                .create(
                    model=model_name,
                    messages=messages
                )
            )

            answer = (
                response
                .choices[0]
                .message
                .content
            )

            if answer:

                answer = str(answer).strip()
                success = True

                print(
                    f"✅ G4F → {model_name}"
                )

                break

        except Exception as e:

            print(
                f"❌ G4F → {model_name}: {e}"
            )

    if not success and groq_client:

        try:

            print(
                "🔄 Groq → GPT-OSS 120B"
            )

            response = (
                groq_client
                .chat
                .completions
                .create(
                    model="openai/gpt-oss-120b",
                    messages=messages
                )
            )

            answer = (
                response
                .choices[0]
                .message
                .content
            )

            if answer:

                answer = str(answer).strip()
                success = True

                print(
                    "✅ Groq успешно"
                )

        except Exception as e:

            print(
                f"❌ Groq: {e}"
            )

    if success:

        user_histories[user_id].append(
            {
                "role": "assistant",
                "content": answer
            }
        )

        print("🤖 Ответ получен")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

        return answer

    user_histories[user_id].pop()

    print(
        "❌ Все AI-провайдеры не ответили"
    )

    if mode == "neuroham":

        return (
            "Даже мои процессоры решили "
            "сегодня саботировать работу 🙄"
        )

    return (
        "Не удалось получить ответ от ИИ. "
        "Попробуй ещё раз немного позже."
    )


# ============================================================
# KIRA
# ============================================================

def is_kira_question(text):

    normalized = text.lower().strip()

    normalized = re.sub(
        r"[^\w\s]",
        " ",
        normalized
    )

    normalized = re.sub(
        r"\s+",
        " ",
        normalized
    ).strip()

    patterns = [
        r"\bкто такая кира\b",
        r"\bа кто такая кира\b",
        r"\bрасскажи про киру\b",
        r"\bрасскажи кто такая кира\b",
        r"\bчто за кира\b",
        r"\bкто кира\b",
        r"\bкира кто\b",
        r"\bа кира кто\b",
        r"\bможешь рассказать про киру\b",
        r"\bможешь рассказать кто такая кира\b"
    ]

    return any(
        re.search(pattern, normalized)
        for pattern in patterns
    )


def generate_kira_text():

    prompt = """
Напиши красивый, тёплый и приятный текст о девушке
по имени Кира.

Это пасхалка Telegram-бота.

Текст должен звучать так, будто Кира —
очень дорогой и особенный человек.

Используй красивые метафоры:
свет, тепло, улыбка, доброта, спокойствие.

Не придумывай конкретные факты о её жизни,
внешности, возрасте или характере.

3–5 предложений.
Можно 2–4 эмодзи.
Без Markdown.

Начни:
«Кира — это...»
"""

    models = [
        "gpt-4o-mini",
        "gpt-3.5-turbo",
        "gpt-4",
        "llama-3-70b"
    ]

    for model_name in models:

        try:

            response = (
                ai_client
                .chat
                .completions
                .create(
                    model=model_name,
                    messages=[
                        {
                            "role": "system",
                            "content":
                                "Пиши красивые и добрые тексты."
                        },
                        {
                            "role": "user",
                            "content": prompt
                        }
                    ]
                )
            )

            answer = (
                response
                .choices[0]
                .message
                .content
            )

            if answer:

                return clean_markdown(
                    str(answer).strip()
                )

        except Exception as e:

            print(
                f"⚠️ Kira G4F {model_name}: {e}"
            )

    if groq_client:

        try:

            response = (
                groq_client
                .chat
                .completions
                .create(
                    model="openai/gpt-oss-120b",
                    messages=[
                        {
                            "role": "system",
                            "content":
                                "Пиши красивые и тёплые тексты."
                        },
                        {
                            "role": "user",
                            "content": prompt
                        }
                    ]
                )
            )

            answer = (
                response
                .choices[0]
                .message
                .content
            )

            if answer:
                return clean_markdown(
                    str(answer).strip()
                )

        except Exception as e:

            print(
                f"❌ Groq Kira: {e}"
            )

    return (
        "Кира — это человек, рядом с которым "
        "становится немного теплее. ✨ "
        "В ней есть что-то особенное, "
        "что сложно объяснить словами. "
        "Она умеет оставлять после себя "
        "приятное чувство и добрую улыбку. ❤️"
    )


# ============================================================
# WEB SEARCH
# ============================================================

def perform_web_search(query):

    results_text = ""

    if tavily_client:

        try:

            response = tavily_client.search(
                query=query,
                max_results=3
            )

            for result in response.get(
                "results",
                []
            ):

                results_text += (
                    f"- {result.get('title', 'Без заголовка')}: "
                    f"{result.get('content', '')}\n"
                )

        except Exception as e:

            print(
                f"⚠️ Tavily: {e}"
            )

    if not results_text:

        try:

            from duckduckgo_search import DDGS

            with DDGS() as ddgs:

                results = list(
                    ddgs.text(
                        query,
                        max_results=3
                    )
                )

                for result in results:

                    results_text += (
                        f"- {result.get('title', 'Без заголовка')}: "
                        f"{result.get('body', '')[:500]}\n"
                    )

        except Exception as e:

            results_text = (
                f"Не удалось выполнить поиск: {e}"
            )

    return results_text


# ============================================================
# IMAGE GENERATION
# ============================================================

def generate_image_dynamic(prompt):

    for model in [
        "flux",
        "dall-e-3"
    ]:

        try:

            response = (
                ai_client
                .images
                .generate(
                    model=model,
                    prompt=prompt,
                    response_format="url"
                )
            )

            image_url = (
                response
                .data[0]
                .url
            )

            if image_url:

                image = requests.get(
                    image_url,
                    timeout=30
                )

                if image.status_code == 200:
                    return image.content

        except Exception as e:

            print(
                f"⚠️ Image {model}: {e}"
            )

    return None


# ============================================================
# GEMINI IMAGE ANALYSIS
# ============================================================

def analyze_image_gemini(
    image_bytes
):

    if not GEMINI_API_KEY or not genai:

        return (
            "Анализ фото недоступен: "
            "не задан GEMINI_API_KEY."
        )

    for model_name in [
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-1.5-flash"
    ]:

        try:

            model = genai.GenerativeModel(
                model_name
            )

            image = Image.open(
                io.BytesIO(image_bytes)
            )

            response = model.generate_content(
                [
                    (
                        "Опиши, что изображено "
                        "на фотографии. "
                        "Ответь на русском. "
                        "Без Markdown."
                    ),
                    image
                ]
            )

            if response and response.text:

                return clean_markdown(
                    response.text.strip()
                )

        except Exception as e:

            print(
                f"⚠️ Gemini {model_name}: {e}"
            )

    return (
        "Не удалось получить ответ от Gemini."
    )


# ============================================================
# TTS
# ============================================================

async def generate_audio(
    text,
    output_file
):

    communicate = edge_tts.Communicate(
        text,
        "ru-RU-SvetlanaNeural"
    )

    await communicate.save(
        output_file
    )


# ============================================================
# MUSIC GENERATION — REPLICATE MUSICGEN
# ============================================================

def generate_music(
    prompt,
    duration=30
):

    if not REPLICATE_API_TOKEN:

        raise RuntimeError(
            "Не задан REPLICATE_API_TOKEN"
        )

    if replicate is None:

        raise RuntimeError(
            "Не установлен пакет replicate"
        )

    duration = max(
        5,
        min(int(duration), 30)
    )

    print(
        f"🎵 Генерация музыки: {prompt}"
    )

    output = replicate.run(
        "meta/musicgen:671ac645ce5e552cc63a54a2bbff63fcf798043055d2dac5fc9e36a837eedcfb",
        input={
            "prompt": prompt,
            "model_version": "stereo-large",
            "duration": duration,
            "output_format": "mp3",
            "normalization_strategy": "peak"
        }
    )

    if hasattr(output, "url"):

        url = output.url()

    elif isinstance(output, str):

        url = output

    elif isinstance(
        output,
        (list, tuple)
    ) and output:

        first = output[0]

        if hasattr(first, "url"):
            url = first.url()
        else:
            url = str(first)

    else:

        raise RuntimeError(
            f"Неизвестный ответ Replicate: "
            f"{type(output)}"
        )

    response = requests.get(
        url,
        timeout=180
    )

    response.raise_for_status()

    with tempfile.NamedTemporaryFile(
        delete=False,
        suffix=".mp3"
    ) as temp_file:

        temp_file.write(
            response.content
        )

        path = temp_file.name

    if (
        not os.path.exists(path)
        or os.path.getsize(path) == 0
    ):

        if os.path.exists(path):
            os.remove(path)

        raise RuntimeError(
            "Replicate вернул пустой аудиофайл"
        )

    print(
        f"✅ Музыка готова: {path}"
    )

    return path


# ============================================================
# MUSIC REQUEST DETECTION
# ============================================================

def is_music_request(text):

    text = text.lower().strip()

    patterns = [
        "создай трек",
        "сделай трек",
        "сгенерируй трек",
        "создай музыку",
        "сделай музыку",
        "сгенерируй музыку",
        "создай песню",
        "сделай песню",
        "сгенерируй песню",
        "создай бит",
        "сделай бит",
        "сгенерируй бит",
        "сделай мелодию",
        "создай мелодию",
        "сгенерируй мелодию"
    ]

    return any(
        pattern in text
        for pattern in patterns
    )


# ============================================================
# MUSIC COMMAND
# ============================================================

@bot.message_handler(
    commands=["music"]
)
def music_cmd(message):

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) < 2:

        bot.reply_to(
            message,
            "Напиши, какую музыку создать.\n\n"
            "Например:\n"
            "/music атмосферный synthwave "
            "для ночной поездки"
        )

        return

    prompt = parts[1].strip()

    msg = bot.reply_to(
        message,
        "🎵 Генерирую музыку...\n"
        "Это может занять некоторое время."
    )

    path = None

    try:

        path = generate_music(
            prompt,
            duration=30
        )

        with open(
            path,
            "rb"
        ) as audio:

            bot.send_audio(
                message.chat.id,
                audio,
                title="AI Music",
                performer="AI MusicGen",
                caption=(
                    "🎵 Готово!\n"
                    f"Запрос: {prompt}"
                )
            )

        try:

            bot.delete_message(
                message.chat.id,
                msg.message_id
            )

        except Exception:
            pass

    except Exception as e:

        print(
            f"❌ Music error: {e}"
        )

        edit_or_send_long(
            message.chat.id,
            msg.message_id,
            f"Ошибка генерации музыки:\n{e}"
        )

    finally:

        if path and os.path.exists(path):

            try:
                os.remove(path)
            except Exception:
                pass


# ============================================================
# START / HELP
# ============================================================

@bot.message_handler(
    commands=["start", "help"]
)
def help_cmd(message):

    help_text = (
        "Привет! Я ИИ-ассистент 🤖\n\n"
        "Команды:\n\n"

        "/search <запрос> — поиск в интернете\n"
        "/weather <город> — погода\n"
        "/image <описание> — создать изображение\n"
        "/music <описание> — создать музыку 🎵\n"
        "/gemini <запрос> — Gemini\n"
        "/fact [тема] — интересный факт\n"
        "/code <задача> — работа с кодом\n"
        "/sum <текст> — выжимка\n"
        "/tr <текст> — перевод\n"
        "/fix <текст> — исправление текста\n"
        "/tts <текст> — озвучка\n"
        "/file <имя> | <текст> — создать файл\n"
        "/clear — очистить память\n"
        "/neuroham — режим Нейрохама\n\n"

        "Также можешь просто написать:\n"
        "«создай трек в стиле synthwave» 🎵\n\n"

        "Или задать любой обычный вопрос."
    )

    bot.reply_to(
        message,
        help_text
    )


# ============================================================
# NEUROHAM
# ============================================================

@bot.message_handler(
    commands=["neuroham", "rude"]
)
def toggle_neuroham_mode(message):

    user_id = message.chat.id

    current_mode = user_modes.get(
        user_id,
        "normal"
    )

    if current_mode == "normal":

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


# ============================================================
# CLEAR
# ============================================================

@bot.message_handler(
    commands=["clear"]
)
def clear_cmd(message):

    user_id = message.chat.id

    if user_id in user_histories:
        del user_histories[user_id]

    bot.reply_to(
        message,
        "Память диалога очищена."
    )


# ============================================================
# FACT
# ============================================================

@bot.message_handler(
    commands=["fact"]
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
            f"на тему: {topic}. "
            f"Будь краток."
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

    edit_or_send_long(
        message.chat.id,
        msg.message_id,
        fact
    )


# ============================================================
# WEATHER
# ============================================================

@bot.message_handler(
    commands=["weather"]
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
            "Укажи город.\n\n"
            "Например:\n"
            "/weather Ташкент"
        )

        return

    try:

        response = requests.get(
            f"https://wttr.in/{city}",
            params={
                "format":
                    "Город: %l\n"
                    "Погода: %C %c\n"
                    "Температура: %t "
                    "(ощущается как %f)\n"
                    "Ветер: %w\n"
                    "Влажность: %h",
                "lang": "ru",
                "m": ""
            },
            timeout=8
        )

        if response.status_code == 200:

            bot.reply_to(
                message,
                "Сводка:\n\n"
                + response.text.strip()
            )

        else:

            bot.reply_to(
                message,
                "Город не найден."
            )

    except Exception as e:

        bot.reply_to(
            message,
            f"Ошибка погоды: {e}"
        )


# ============================================================
# SEARCH
# ============================================================

@bot.message_handler(
    commands=["search"]
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
            "Напиши запрос.\n\n"
            "Например:\n"
            "/search новости"
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
        f"Вот результаты поиска по запросу "
        f"'{query}':\n\n"
        f"{raw_data}\n\n"
        "Сделай краткую понятную выжимку. "
        "Без Markdown."
    )

    reply = ask_ai_with_history(
        message.chat.id,
        prompt
    )

    edit_or_send_long(
        message.chat.id,
        msg.message_id,
        reply
    )


# ============================================================
# AI COMMANDS
# ============================================================

@bot.message_handler(
    commands=[
        "gemini",
        "code",
        "sum",
        "tr",
        "fix"
    ]
)
def ai_tools_cmd(message):

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) < 2:

        bot.reply_to(
            message,
            "Напиши текст после команды."
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

    edit_or_send_long(
        message.chat.id,
        msg.message_id,
        reply
    )


# ============================================================
# IMAGE
# ============================================================

@bot.message_handler(
    commands=["image"]
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
            "Опиши картинку.\n\n"
            "Например:\n"
            "/image кот в космосе"
        )

        return

    msg = bot.reply_to(
        message,
        "Генерирую..."
    )

    image_bytes = generate_image_dynamic(
        prompt
    )

    if image_bytes:

        bot.send_photo(
            message.chat.id,
            image_bytes,
            caption=f"Запрос: {prompt}"
        )

        try:

            bot.delete_message(
                message.chat.id,
                msg.message_id
            )

        except Exception:
            pass

    else:

        edit_or_send_long(
            message.chat.id,
            msg.message_id,
            "Не удалось сгенерировать изображение."
        )


# ============================================================
# TTS
# ============================================================

@bot.message_handler(
    commands=["tts"]
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

    try:

        asyncio.run(
            generate_audio(
                parts[1],
                audio_path
            )
        )

        with open(
            audio_path,
            "rb"
        ) as audio:

            bot.send_voice(
                message.chat.id,
                audio
            )

        try:

            bot.delete_message(
                message.chat.id,
                msg.message_id
            )

        except Exception:
            pass

    except Exception as e:

        edit_or_send_long(
            message.chat.id,
            msg.message_id,
            f"Ошибка TTS: {e}"
        )

    finally:

        if os.path.exists(audio_path):

            try:
                os.remove(audio_path)
            except Exception:
                pass


# ============================================================
# TEXT FILE
# ============================================================

@bot.message_handler(
    commands=["file"]
)
def file_cmd(message):

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) < 2:

        bot.reply_to(
            message,
            "Формат:\n"
            "/file имя.txt | содержимое\n\n"
            "Например:\n"
            "/file hello.txt | Привет, мир!"
        )

        return

    data = parts[1]

    if "|" not in data:

        bot.reply_to(
            message,
            "Используй разделитель |"
        )

        return

    filename, content = data.split(
        "|",
        1
    )

    filename = filename.strip()
    content = content.strip()

    if not filename:

        filename = "file.txt"

    filename = os.path.basename(
        filename
    )

    if not filename.endswith(
        (
            ".txt",
            ".md",
            ".json",
            ".csv",
            ".html",
            ".css",
            ".js",
            ".py",
            ".xml",
            ".yaml",
            ".yml"
        )
    ):

        filename += ".txt"

    path = os.path.join(
        tempfile.gettempdir(),
        filename
    )

    try:

        with open(
            path,
            "w",
            encoding="utf-8"
        ) as file:

            file.write(content)

        with open(
            path,
            "rb"
        ) as file:

            bot.send_document(
                message.chat.id,
                file,
                caption=f"📄 {filename}"
            )

    except Exception as e:

        bot.reply_to(
            message,
            f"Ошибка создания файла: {e}"
        )

    finally:

        if os.path.exists(path):

            try:
                os.remove(path)
            except Exception:
                pass


# ============================================================
# TEXT TABLE
# ============================================================

def make_text_table(headers, rows):

    all_rows = [
        headers
    ] + rows

    columns = len(headers)

    widths = []

    for column in range(columns):

        width = max(
            len(
                str(
                    row[column]
                )
            )
            if column < len(row)
            else 0
            for row in all_rows
        )

        widths.append(
            min(width, 30)
        )

    def format_row(row):

        cells = []

        for i in range(columns):

            value = (
                str(row[i])
                if i < len(row)
                else ""
            )

            value = value[:30]

            cells.append(
                value.ljust(
                    widths[i]
                )
            )

        return " | ".join(cells)

    separator = "-+-".join(
        "-" * width
        for width in widths
    )

    result = [
        format_row(headers),
        separator
    ]

    for row in rows:
        result.append(
            format_row(row)
        )

    return "\n".join(result)


# ============================================================
# TABLE COMMAND
# ============================================================

@bot.message_handler(
    commands=["table"]
)
def table_cmd(message):

    text = message.text.split(
        maxsplit=1
    )

    if len(text) < 2:

        bot.reply_to(
            message,
            "Формат:\n"
            "/table Имя | Возраст\n"
            "Алекс | 18\n"
            "Иван | 20"
        )

        return

    lines = [
        line.strip()
        for line in text[1].splitlines()
        if line.strip()
    ]

    if len(lines) < 2:

        bot.reply_to(
            message,
            "Нужно указать заголовок "
            "и хотя бы одну строку."
        )

        return

    headers = [
        item.strip()
        for item in lines[0].split("|")
    ]

    rows = []

    for line in lines[1:]:

        rows.append([
            item.strip()
            for item in line.split("|")
        ])

    table = make_text_table(
        headers,
        rows
    )

    safe_table = html.escape(
        table,
        quote=False
    )

    bot.send_message(
        message.chat.id,
        f"<pre>{safe_table}</pre>",
        parse_mode="HTML"
    )


# ============================================================
# TEXT HANDLER
# ============================================================

@bot.message_handler(
    content_types=["text"]
)
def handle_text(message):

    text = message.text or ""
    text_lower = text.lower()

    # ========================================================
    # MUSIC
    # ========================================================

    if is_music_request(text):

        prompt = text

        prefixes = [
            "создай трек",
            "сделай трек",
            "сгенерируй трек",
            "создай музыку",
            "сделай музыку",
            "сгенерируй музыку",
            "создай песню",
            "сделай песню",
            "сгенерируй песню",
            "создай бит",
            "сделай бит",
            "сгенерируй бит",
            "сделай мелодию",
            "создай мелодию",
            "сгенерируй мелодию"
        ]

        for prefix in prefixes:

            if prompt.lower().startswith(prefix):

                prompt = prompt[
                    len(prefix):
                ].strip()

                break

        if not prompt:

            prompt = (
                "атмосферная современная "
                "инструментальная музыка"
            )

        msg = bot.reply_to(
            message,
            "🎵 Генерирую музыку..."
        )

        path = None

        try:

            path = generate_music(
                prompt,
                30
            )

            with open(
                path,
                "rb"
            ) as audio:

                bot.send_audio(
                    message.chat.id,
                    audio,
                    title="AI Music",
                    performer="AI MusicGen"
                )

            try:

                bot.delete_message(
                    message.chat.id,
                    msg.message_id
                )

            except Exception:
                pass

        except Exception as e:

            edit_or_send_long(
                message.chat.id,
                msg.message_id,
                f"Ошибка генерации музыки:\n{e}"
            )

        finally:

            if path and os.path.exists(path):

                try:
                    os.remove(path)
                except Exception:
                    pass

        return

    # ========================================================
    # KIRA
    # ========================================================

    if is_kira_question(text):

        msg = bot.reply_to(
            message,
            "✨ Думаю, как лучше рассказать о Кире..."
        )

        try:

            kira_text = generate_kira_text()

            edit_or_send_long(
                message.chat.id,
                msg.message_id,
                kira_text
            )

        except Exception as e:

            print(
                f"❌ Kira: {e}"
            )

            edit_or_send_long(
                message.chat.id,
                msg.message_id,
                (
                    "Кира — это человек, "
                    "который умеет делать мир "
                    "немного теплее. ✨❤️"
                )
            )

        return

    # ========================================================
    # URL
    # ========================================================

    if (
        "http://" in text_lower
        or "https://" in text_lower
    ):

        msg = bot.reply_to(
            message,
            "Читаю ссылку..."
        )

        try:

            urls = [
                word
                for word in text.split()
                if word.startswith("http")
            ]

            if not urls:
                raise ValueError(
                    "Ссылка не найдена"
                )

            response = requests.get(
                urls[0],
                timeout=10,
                headers={
                    "User-Agent":
                        "Mozilla/5.0"
                }
            )

            soup = BeautifulSoup(
                response.text,
                "html.parser"
            )

            page_text = soup.get_text(
                separator=" ",
                strip=True
            )[:5000]

            reply = ask_ai_with_history(
                message.chat.id,
                (
                    "Сделай краткую выжимку "
                    "этого текста. "
                    "Без Markdown.\n\n"
                    + page_text
                )
            )

            edit_or_send_long(
                message.chat.id,
                msg.message_id,
                reply
            )

        except Exception as e:

            edit_or_send_long(
                message.chat.id,
                msg.message_id,
                f"Ошибка чтения ссылки: {e}"
            )

        return

    # ========================================================
    # NORMAL AI CHAT
    # ========================================================

    msg = bot.reply_to(
        message,
        "Думаю..."
    )

    reply = ask_ai_with_history(
        message.chat.id,
        text
    )

    edit_or_send_long(
        message.chat.id,
        msg.message_id,
        reply
    )


# ============================================================
# PHOTO
# ============================================================

@bot.message_handler(
    content_types=["photo"]
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

        image_bytes = bot.download_file(
            file_info.file_path
        )

        answer = analyze_image_gemini(
            image_bytes
        )

        edit_or_send_long(
            message.chat.id,
            msg.message_id,
            answer
        )

    except Exception as e:

        edit_or_send_long(
            message.chat.id,
            msg.message_id,
            f"Ошибка анализа фото: {e}"
        )


# ============================================================
# PDF
# ============================================================

@bot.message_handler(
    content_types=["document"]
)
def handle_doc(message):

    if (
        message.document.mime_type
        != "application/pdf"
    ):

        bot.reply_to(
            message,
            "Пока поддерживаются документы "
            "в формате PDF."
        )

        return

    msg = bot.reply_to(
        message,
        "Читаю PDF..."
    )

    path = None

    try:

        file_info = bot.get_file(
            message.document.file_id
        )

        file_data = bot.download_file(
            file_info.file_path
        )

        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=".pdf"
        ) as temp_file:

            temp_file.write(
                file_data
            )

            path = temp_file.name

        reader = PdfReader(
            path
        )

        pages = []

        for page in reader.pages[:5]:

            page_text = page.extract_text()

            if page_text:
                pages.append(page_text)

        text = "\n".join(pages)

        if not text.strip():

            raise ValueError(
                "Не удалось извлечь текст из PDF."
            )

        reply = ask_ai_with_history(
            message.chat.id,
            (
                "Сделай краткую и понятную "
                "выжимку из этого PDF. "
                "Без Markdown.\n\n"
                + text[:6000]
            )
        )

        edit_or_send_long(
            message.chat.id,
            msg.message_id,
            reply
        )

    except Exception as e:

        edit_or_send_long(
            message.chat.id,
            msg.message_id,
            f"Ошибка PDF: {e}"
        )

    finally:

        if path and os.path.exists(path):

            try:
                os.remove(path)
            except Exception:
                pass


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    print("=" * 60)
    print("🚀 Бот запускается...")
    print("=" * 60)

    print("🎵 Музыка: включена")
    print("✨ Пасхалка Кира: включена")
    print("💻 Режим программирования: включён")
    print("🖼️ Генерация изображений: включена")
    print("🔊 TTS: включён")
    print("📄 PDF: включён")
    print("📋 Таблицы: включены")
    print("📦 Создание текстовых файлов: включено")
    print("📨 Длинные ответы: включены")
    print("📋 Кодовые блоки: включены")
    print("🔄 AI fallback: G4F → Groq")
    print("=" * 60)

    if GROQ_API_KEY:
        print("✅ GROQ_API_KEY найден")
    else:
        print("⚠️ GROQ_API_KEY не найден")

    if REPLICATE_API_TOKEN:
        print("✅ REPLICATE_API_TOKEN найден")
    else:
        print(
            "⚠️ REPLICATE_API_TOKEN НЕ найден — "
            "музыка работать не будет"
        )

    if replicate:
        print("✅ Библиотека Replicate установлена")
    else:
        print(
            "⚠️ Библиотека Replicate не установлена"
        )

    print("=" * 60)

    threading.Thread(
        target=run_web,
        daemon=True
    ).start()

    print(
        "🤖 Telegram polling запущен"
    )

    while True:

        try:

            bot.infinity_polling(
                skip_pending=True,
                timeout=30,
                long_polling_timeout=30
            )

        except Exception as e:

            print(
                f"⚠️ Telegram polling остановлен: {e}"
            )

            print(
                "🔄 Повторное подключение "
                "через 5 секунд..."
            )

            time.sleep(5)
