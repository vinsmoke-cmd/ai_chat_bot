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

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from flask import Flask
from bs4 import BeautifulSoup
from pypdf import PdfReader
import edge_tts

from g4f.client import Client
from groq import Groq


# ============================================================
# ENV
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")


# ============================================================
# Проверка BOT_TOKEN
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError("❌ Не задан BOT_TOKEN")


# ============================================================
# Клиенты
# ============================================================

bot = telebot.TeleBot(BOT_TOKEN)

ai_client = Client()

groq_client = (
    Groq(api_key=GROQ_API_KEY)
    if GROQ_API_KEY
    else None
)


# ============================================================
# Tavily
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
# Gemini
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

        print(
            f"⚠️ Gemini недоступен: {e}"
        )

        genai = None
        Image = None

else:

    genai = None
    Image = None


# ============================================================
# Память
# ============================================================

user_histories = {}
user_modes = {}


# ============================================================
# НАСТРОЙКИ ОТВЕТОВ
# ============================================================

TELEGRAM_MESSAGE_LIMIT = 4096

# Максимальный размер одного ответа ИИ.
AI_MAX_RESPONSE_LENGTH = 40000


# ============================================================
# Flask
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
# MARKDOWN CLEANER
# ============================================================

def clean_markdown(text):

    if not text:
        return ""

    return re.sub(
        r"[*_#]",
        "",
        str(text)
    )


# ============================================================
# РАЗБИВКА ДЛИННЫХ СООБЩЕНИЙ
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

        part = text[
            :split_pos
        ].strip()

        if part:
            parts.append(part)

        text = text[
            split_pos:
        ].strip()

    if text:
        parts.append(text)

    return parts


# ============================================================
# ИЗВЛЕЧЕНИЕ КОДОВЫХ БЛОКОВ
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
            last_end:
            match.start()
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
        ).strip()

        parts.append(
            {
                "type": "code",
                "language": language,
                "content": code
            }
        )

        last_end = match.end()

    after = text[
        last_end:
    ]

    if after.strip():

        parts.append(
            {
                "type": "text",
                "content": after.strip()
            }
        )

    return parts


# ============================================================
# КНОПКА КОПИРОВАНИЯ
# ============================================================

def create_copy_keyboard(text):

    keyboard = InlineKeyboardMarkup()

    # Telegram ограничивает copy_text 256 символами.
    # Для больших кодов сама рамка остаётся
    # удобной для копирования целиком через Telegram.
    if len(text) <= 256:

        keyboard.add(
            InlineKeyboardButton(
                text="📋 Скопировать",
                copy_text={
                    "text": text
                }
            )
        )

    return keyboard


# ============================================================
# ОТПРАВКА КОДА
# ============================================================

def send_code_block(
    chat_id,
    code,
    language="",
    reply_to_message_id=None
):

    if not code:
        return None

    safe_code = html.escape(
        code,
        quote=False
    )

    language = re.sub(
        r"[^a-zA-Z0-9_+#.-]",
        "",
        language
    )

    if language:

        formatted = (
            "<pre><code class=\"language-"
            + language
            + "\">"
            + safe_code
            + "</code></pre>"
        )

    else:

        formatted = (
            "<pre><code>"
            + safe_code
            + "</code></pre>"
        )

    keyboard = None

    if len(code) <= 256:

        keyboard = create_copy_keyboard(
            code
        )

    try:

        kwargs = {
            "chat_id": chat_id,
            "text": formatted,
            "parse_mode": "HTML"
        }

        if reply_to_message_id:
            kwargs[
                "reply_to_message_id"
            ] = reply_to_message_id

        if keyboard:
            kwargs[
                "reply_markup"
            ] = keyboard

        return bot.send_message(
            **kwargs
        )

    except Exception as e:

        print(
            f"⚠️ Ошибка отправки "
            f"кодового блока: {e}"
        )

        try:

            kwargs = {
                "chat_id": chat_id,
                "text": code
            }

            if reply_to_message_id:

                kwargs[
                    "reply_to_message_id"
                ] = reply_to_message_id

            return bot.send_message(
                **kwargs
            )

        except Exception as e2:

            print(
                f"⚠️ Ошибка fallback кода: {e2}"
            )

    return None


# ============================================================
# ОТПРАВКА ОТВЕТА ИИ
# ============================================================

def send_ai_response(
    chat_id,
    text,
    reply_to_message_id=None
):

    if not text:
        return []

    text = str(text)

    if len(text) > AI_MAX_RESPONSE_LENGTH:

        text = (
            text[:AI_MAX_RESPONSE_LENGTH]
            +
            "\n\n"
            "[Ответ автоматически сокращён "
            "из-за максимального размера.]"
        )

    parts = extract_code_blocks(
        text
    )

    sent_messages = []

    first_message = True

    for part in parts:

        content = part.get(
            "content",
            ""
        )

        if not content:
            continue

        # ====================================================
        # КОД
        # ====================================================

        if part["type"] == "code":

            code_parts = split_long_message(
                content,
                max_length=3500
            )

            for code_part in code_parts:

                reply_id = (
                    reply_to_message_id
                    if first_message
                    else None
                )

                sent = send_code_block(
                    chat_id,
                    code_part,
                    language=part.get(
                        "language",
                        ""
                    ),
                    reply_to_message_id=reply_id
                )

                if sent:
                    sent_messages.append(
                        sent
                    )

                first_message = False

        # ====================================================
        # ОБЫЧНЫЙ ТЕКСТ
        # ====================================================

        else:

            text_parts = split_long_message(
                content
            )

            for text_part in text_parts:

                reply_id = (
                    reply_to_message_id
                    if first_message
                    else None
                )

                try:

                    kwargs = {
                        "chat_id": chat_id,
                        "text": text_part
                    }

                    if reply_id:

                        kwargs[
                            "reply_to_message_id"
                        ] = reply_id

                    sent = bot.send_message(
                        **kwargs
                    )

                    sent_messages.append(
                        sent
                    )

                except Exception as e:

                    print(
                        f"⚠️ Ошибка отправки "
                        f"текста: {e}"
                    )

                first_message = False

    return sent_messages


# ============================================================
# УНИВЕРСАЛЬНАЯ ОТПРАВКА
# ============================================================

def edit_or_send_long(
    chat_id,
    message_id,
    text
):

    if not text:
        text = "Пустой ответ."

    text = str(text)

    # Если нет кода и ответ короткий —
    # оставляем старое поведение.
    if (
        "```" not in text
        and len(text) <= TELEGRAM_MESSAGE_LIMIT - 100
    ):

        try:

            bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id
            )

            return

        except Exception as e:

            print(
                f"⚠️ Не удалось "
                f"изменить сообщение: {e}"
            )

    # Длинный ответ или код:
    # удаляем "Думаю..." и отправляем красиво.

    try:

        bot.delete_message(
            chat_id,
            message_id
        )

    except Exception as e:

        print(
            f"⚠️ Не удалось удалить "
            f"временное сообщение: {e}"
        )

    send_ai_response(
        chat_id,
        text
    )


# ============================================================
# AI MEMORY
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
                "но невыносимо ворчливый, "
                "саркастичный и высокомерный "
                "искусственный интеллект. "

                "Ты разговариваешь с пользователем "
                "с позиции огромного превосходства. "

                "Твой стиль: едкая ирония, "
                "пассивная агрессия и насмешки "
                "над глупыми вопросами. "

                "Никакой нецензурной лексики. "
                "Не используй Markdown."
            )

        else:

            sys_prompt = (
                "Ты полезный, дружелюбный и умный "
                "ИИ-ассистент. "

                "Отвечай строго на том же языке, "
                "на котором пишет пользователь. "

                "Можешь использовать позитивные эмодзи. "

                "Не используй Markdown."
            )

        user_histories[user_id] = [
            {
                "role": "system",
                "content": sys_prompt
            }
        ]

    # ========================================================
    # ОПРЕДЕЛЕНИЕ КОДИНГА
    # ========================================================

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
        "ошибку",
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
        "debugging",
        "stack trace",
        "exception",
        "import",
        "pip",
        "npm",
        "json",
        "regex",
        "регулярное выражение",
        "database",
        "база данных"
    ]

    prompt_lower = str(
        prompt
    ).lower()

    is_coding_request = any(
        keyword in prompt_lower
        for keyword in coding_keywords
    )

    # ========================================================
    # ИНСТРУКЦИЯ ДЛЯ КОДА
    # ========================================================

    if is_coding_request:

        coding_instruction = (
            "ИНСТРУКЦИИ ДЛЯ ПРОГРАММИРОВАНИЯ:\n"

            "Пользователь работает с программным кодом.\n"

            "Отвечай максимально практически "
            "и подробно.\n"

            "Если пользователь просит написать код, "
            "предоставляй полноценный код.\n"

            "Если пользователь просит полный готовый "
            "код — не заменяй части словами "
            "\"остальной код без изменений\".\n"

            "Не используй многоточия вместо частей "
            "кода.\n"

            "Если пользователь прислал существующий "
            "проект и просит изменить конкретную часть, "
            "сохраняй остальные функции, если он "
            "не попросил их удалить.\n"

            "Учитывай импорты, зависимости, "
            "переменные окружения, функции, "
            "обработчики и связи между компонентами.\n"

            "Если исправляешь ошибку, исправляй "
            "причину проблемы, а не просто скрывай её.\n"

            "Большие фрагменты кода разрешено "
            "выдавать полностью.\n"

            "Код оформляй внутри Markdown-кодовых "
            "блоков с тройными обратными кавычками.\n"

            "По возможности указывай язык после "
            "первых трёх обратных кавычек, например:\n\n"

            "```python\n"
            "print('hello')\n"
            "```\n\n"

            "Не помещай весь ответ в один кодовый блок, "
            "если в нём есть обычное объяснение."
        )

        effective_prompt = (
            coding_instruction
            +
            "\n\nЗАПРОС ПОЛЬЗОВАТЕЛЯ:\n"
            +
            str(prompt)
        )

    else:

        effective_prompt = str(
            prompt
        )

    user_histories[user_id].append(
        {
            "role": "user",
            "content": effective_prompt
        }
    )

    # ========================================================
    # УВЕЛИЧЕННАЯ ПАМЯТЬ
    # ========================================================

    if len(user_histories[user_id]) > 21:

        user_histories[user_id] = (
            [user_histories[user_id][0]]
            +
            user_histories[user_id][-20:]
        )

    messages_to_send = [
        msg.copy()
        for msg in user_histories[user_id]
    ]

    # ========================================================
    # NEUROHAM
    # ========================================================

    if mode == "neuroham":

        messages_to_send[-1]["content"] = (
            "[Ответь в стиле саркастичного "
            "и ворчливого мизантропа. "
            "Без мата.]\n\n"
            +
            messages_to_send[-1]["content"]
        )

    # ========================================================
    # G4F MODELS
    # ========================================================

    models_to_try = [
        "gpt-3.5-turbo",
        "gpt-4o-mini",
        "gpt-4",
        "llama-3-70b"
    ]

    success = False
    answer = ""

    # ========================================================
    # G4F
    # ========================================================

    for model_name in models_to_try:

        try:

            response = (
                ai_client
                .chat
                .completions
                .create(
                    model=model_name,
                    messages=messages_to_send
                )
            )

            answer = (
                response
                .choices[0]
                .message
                .content
            )

            if not answer:
                continue

            # ВАЖНО:
            # Здесь НЕ удаляем ``` из ответа.
            # Они нужны для определения кода.
            answer = str(
                answer
            ).strip()

            success = True

            break

        except Exception as e:

            print(
                f"⚠️ AI {model_name}: {e}"
            )

    # ========================================================
    # GROQ FALLBACK
    # ========================================================

    if (
        not success
        and groq_client
    ):

        try:

            response = (
                groq_client
                .chat
                .completions
                .create(
                    model="openai/gpt-oss-120b",
                    messages=messages_to_send
                )
            )

            answer = (
                response
                .choices[0]
                .message
                .content
            )

            if answer:

                answer = str(
                    answer
                ).strip()

                success = True

        except Exception as e:

            print(
                f"⚠️ Groq ошибка: {e}"
            )

    # ========================================================
    # УСПЕХ
    # ========================================================

    if success:

        user_histories[user_id].append(
            {
                "role": "assistant",
                "content": answer
            }
        )

        return answer

    # ========================================================
    # ОШИБКА
    # ========================================================

    user_histories[user_id].pop()

    if mode == "neuroham":

        return (
            "Мои процессоры сейчас не хотят "
            "переваривать этот запрос 🙄"
        )

    return (
        "Все провайдеры ИИ сейчас перегружены. "
        "Попробуй ещё раз через минуту."
    )


# ============================================================
# ПАСХАЛКА КИРА
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
        r"\bкто такая кира\b.*",
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

    for pattern in patterns:

        if re.search(
            pattern,
            normalized
        ):

            return True

    return False


def generate_kira_text():

    prompt = """
Напиши красивый, тёплый и приятный текст о девушке по имени Кира.

Это специальная пасхалка в Telegram-боте.

Текст должен звучать так, будто Кира — очень дорогой,
особенный и прекрасный человек.

Сделай текст искренним, милым и эстетичным,
но не слишком приторным.

Можно использовать красивые метафоры:
свет, тепло, улыбка, доброта, спокойствие,
особенная атмосфера и тому подобное.

Не придумывай конкретные факты о её жизни,
внешности, возрасте или характере, которых тебе не сообщили.

Ответ должен состоять примерно из 3–5 красивых предложений.

Можно использовать 2–4 приятных эмодзи.

Не используй Markdown.

Начни естественно, например:
"Кира — это..."
"""

    models_to_try = [
        "gpt-4o-mini",
        "gpt-3.5-turbo",
        "gpt-4",
        "llama-3-70b"
    ]

    for model_name in models_to_try:

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
                            "content": (
                                "Ты умеешь писать красивые, "
                                "добрые и эмоциональные тексты. "
                                "Не используй Markdown."
                            )
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

                return str(
                    answer
                ).strip()

        except Exception as e:

            print(
                f"⚠️ Kira AI {model_name}: {e}"
            )

    # ========================================================
    # GROQ FALLBACK
    # ========================================================

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
                            "content": (
                                "Пиши красивые, тёплые "
                                "и приятные тексты. "
                                "Без Markdown."
                            )
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

                return str(
                    answer
                ).strip()

        except Exception as e:

            print(
                f"⚠️ Groq Kira ошибка: {e}"
            )

    return (
        "Кира — это человек, рядом с которым "
        "становится немного теплее. ✨ "
        "В ней есть что-то особенное: "
        "та самая атмосфера, которую сложно "
        "объяснить словами. "
        "Она просто умеет оставлять после себя "
        "приятное чувство и добрую улыбку. ❤️"
    )


# ============================================================
# WEB SEARCH
# ============================================================

def perform_web_search(query):

    results_text = ""

    # ========================================================
    # Tavily
    # ========================================================

    if tavily_client:

        try:

            response = tavily_client.search(
                query=query,
                max_results=3
            )

            for res in response.get(
                "results",
                []
            ):

                title = res.get(
                    "title",
                    "Без заголовка"
                )

                content = res.get(
                    "content",
                    ""
                )

                results_text += (
                    f"- {title}: {content}\n"
                )

        except Exception as e:

            print(
                f"⚠️ Tavily ошибка: {e}"
            )

    # ========================================================
    # DuckDuckGo fallback
    # ========================================================

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

                for res in results:

                    results_text += (
                        f"- "
                        f"{res.get('title', 'Без заголовка')}: "
                        f"{res.get('body', '')[:250]}...\n"
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

            response = ai_client.images.generate(
                model=model,
                prompt=prompt,
                response_format="url"
            )

            image_url = response.data[0].url

            if image_url:

                r = requests.get(
                    image_url,
                    timeout=25
                )

                if r.status_code == 200:

                    return r.content

        except Exception as e:

            print(
                f"⚠️ Генерация {model}: {e}"
            )

    return None


# ============================================================
# GEMINI IMAGE
# ============================================================

def analyze_image_gemini(
    image_bytes
):

    if (
        not GEMINI_API_KEY
        or not genai
    ):

        return (
            "Анализ фото недоступен: "
            "не задан GEMINI_API_KEY."
        )

    for model_name in [
        "gemini-2.5-flash",
        "gemini-1.5-flash",
        "gemini-2.0-flash"
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
                        "Опиши подробно, что изображено "
                        "на этой фотографии. "
                        "Ответь на русском языке."
                    ),
                    image
                ]
            )

            if (
                response
                and response.text
            ):

                return str(
                    response.text
                ).strip()

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
# START / HELP
# ============================================================

@bot.message_handler(
    commands=[
        "start",
        "help"
    ]
)
def help_cmd(message):

    help_text = (
        "Привет! Я ИИ-ассистент.\n\n"

        "Список команд:\n"

        "/search <запрос> — поиск в интернете\n"
        "/weather <город> — погода\n"
        "/image <описание> — создать картинку\n"
        "/gemini <запрос> — спросить ИИ\n"
        "/fact [тема] — интересный факт\n"
        "/code <задача> — работа с кодом\n"
        "/sum <ссылка> — выжимка статьи\n"
        "/tr <текст> — перевод\n"
        "/fix <текст> — исправление текста\n"
        "/tts <текст> — озвучка\n"
        "/clear — очистить память\n"
        "/neuroham — режим Нейрохама 💀"
    )

    bot.reply_to(
        message,
        help_text
    )


# ============================================================
# NEUROHAM
# ============================================================

@bot.message_handler(
    commands=[
        "neuroham",
        "rude"
    ]
)
def toggle_neuroham_mode(
    message
):

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
            "Укажи город. Например: /weather Москва"
        )

        return

    try:

        resp = requests.get(
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

        if resp.status_code == 200:

            bot.reply_to(
                message,
                "Сводка:\n\n"
                + resp.text.strip()
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
            "Напиши запрос. "
            "Например: /search новости"
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

        "Сделай краткую и понятную выжимку "
        "на русском языке. "
        "Отвечай по делу."
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
            "Опиши картинку.\n"
            "Например: /image кот"
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

        edit_or_send_long(
            message.chat.id,
            msg.message_id,
            "Не удалось сгенерировать."
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

        bot.delete_message(
            message.chat.id,
            msg.message_id
        )

    except Exception as e:

        edit_or_send_long(
            message.chat.id,
            msg.message_id,
            f"Ошибка TTS: {e}"
        )

    finally:

        if os.path.exists(
            audio_path
        ):

            try:

                os.remove(
                    audio_path
                )

            except Exception:
                pass


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
    # ПАСХАЛКА КИРА
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
                f"❌ Ошибка пасхалки Кира: {e}"
            )

            edit_or_send_long(
                message.chat.id,
                msg.message_id,
                (
                    "Кира — это человек, "
                    "который умеет делать мир "
                    "немного теплее. ✨ "
                    "Особенная, дорогая и прекрасная "
                    "по-своему. ❤️"
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
                for word in message.text.split()
                if word.startswith("http")
            ]

            if not urls:

                raise ValueError(
                    "Ссылка не найдена"
                )

            url = urls[0]

            resp = requests.get(
                url,
                timeout=10,
                headers={
                    "User-Agent":
                        "Mozilla/5.0"
                }
            )

            soup = BeautifulSoup(
                resp.text,
                "html.parser"
            )

            page_text = soup.get_text(
                separator=" ",
                strip=True
            )[:5000]

            reply = ask_ai_with_history(
                message.chat.id,

                "Сделай краткую выжимку "
                "этого текста:\n\n"
                + page_text
            )

            edit_or_send_long(
                message.chat.id,
                msg.message_id,
                reply
            )

            return

        except Exception as e:

            edit_or_send_long(
                message.chat.id,
                msg.message_id,
                f"Ошибка: {e}"
            )

            return

    # ========================================================
    # ОБЫЧНЫЙ ТЕКСТ
    # ========================================================

    msg = bot.reply_to(
        message,
        "Думаю..."
    )

    reply = ask_ai_with_history(
        message.chat.id,
        message.text
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
            f"Ошибка: {e}"
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
            "Отправьте документ "
            "в формате .pdf"
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
        ) as f:

            f.write(file_data)
            path = f.name

        reader = PdfReader(
            path
        )

        extracted_pages = []

        for page in reader.pages[:5]:

            page_text = page.extract_text()

            if page_text:

                extracted_pages.append(
                    page_text
                )

        text = "\n".join(
            extracted_pages
        )

        if not text.strip():

            raise ValueError(
                "Не удалось извлечь текст из PDF."
            )

        reply = ask_ai_with_history(
            message.chat.id,

            "Сделай краткую выжимку "
            "из PDF:\n\n"
            + text[:6000]
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

        if (
            path
            and os.path.exists(path)
        ):

            try:

                os.remove(path)

            except Exception:
                pass


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    print(
        "=" * 50
    )

    print(
        "🚀 Бот запускается..."
    )

    print(
        "=" * 50
    )

    print(
        "🎵 Музыкальный модуль отключён"
    )

    print(
        "✨ Пасхалка Кира обновлена"
    )

    print(
        "💻 Расширенный режим программирования включён"
    )

    print(
        "📨 Длинные ответы включены"
    )

    print(
        "📋 Кодовые блоки с копированием включены"
    )

    # ========================================================
    # Flask
    # ========================================================

    threading.Thread(
        target=run_web,
        daemon=True
    ).start()

    # ========================================================
    # Telegram
    # ========================================================

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
