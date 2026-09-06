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

        print(
            f"⚠️ Gemini недоступен: {e}"
        )

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
# ОЧИСТКА ОБЫЧНОГО MARKDOWN
# ============================================================

def clean_markdown(text):

    if not text:
        return ""

    text = str(text)

    # Защищаем кодовые блоки,
    # чтобы символы _ и # внутри кода
    # случайно не удалялись.
    code_blocks = []

    def protect_code(match):

        code_blocks.append(
            match.group(0)
        )

        return f"§CODEBLOCK{len(code_blocks) - 1}§"

    protected = re.sub(
        r"```.*?```",
        protect_code,
        text,
        flags=re.DOTALL
    )

    protected = re.sub(
        r"[*_#]",
        "",
        protected
    )

    for index, code_block in enumerate(
        code_blocks
    ):

        protected = protected.replace(
            f"§CODEBLOCK{index}§",
            code_block
        )

    return protected


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
        )

        # Убираем только лишние переносы
        # по краям. Сам код внутри сохраняем.
        code = code.strip("\n")

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
# ОТПРАВКА КОДА В TELEGRAM
# ============================================================

def send_code_block(
    chat_id,
    code,
    language=""
):

    if not code:
        return None

    safe_code = html.escape(
        code,
        quote=False
    )

    # Telegram понимает <pre><code>
    # как отдельный форматированный
    # блок кода.
    #
    # В клиентах Telegram такой блок
    # имеет стандартное копирование.

    formatted = (
        "<pre><code>"
        + safe_code
        + "</code></pre>"
    )

    try:

        return bot.send_message(
            chat_id,
            formatted,
            parse_mode="HTML"
        )

    except Exception as e:

        print(
            f"⚠️ Ошибка отправки "
            f"кодового блока: {e}"
        )

        try:

            return bot.send_message(
                chat_id,
                code
            )

        except Exception as e2:

            print(
                f"❌ Ошибка fallback "
                f"кодового блока: {e2}"
            )

    return None


# ============================================================
# ОТПРАВКА AI-ОТВЕТА
# ============================================================

def send_ai_response(
    chat_id,
    text
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

                sent = send_code_block(
                    chat_id,
                    code_part,
                    part.get(
                        "language",
                        ""
                    )
                )

                if sent:
                    sent_messages.append(
                        sent
                    )

        # ====================================================
        # ОБЫЧНЫЙ ТЕКСТ
        # ====================================================

        else:

            text_parts = split_long_message(
                content
            )

            for text_part in text_parts:

                try:

                    sent = bot.send_message(
                        chat_id,
                        text_part
                    )

                    sent_messages.append(
                        sent
                    )

                except Exception as e:

                    print(
                        f"⚠️ Ошибка отправки "
                        f"текста: {e}"
                    )

    return sent_messages


# ============================================================
# ИЗМЕНИТЬ ВРЕМЕННОЕ СООБЩЕНИЕ
# ИЛИ ОТПРАВИТЬ НОВЫЕ СООБЩЕНИЯ
# ============================================================

def edit_or_send_long(
    chat_id,
    message_id,
    text
):

    if not text:
        text = "Пустой ответ."

    text = str(text)

    # Если обычный короткий текст,
    # оставляем старое поведение —
    # просто меняем "Думаю..." на ответ.
    if (
        "```" not in text
        and len(text)
        <= TELEGRAM_MESSAGE_LIMIT - 100
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
                f"⚠️ Не удалось изменить "
                f"сообщение: {e}"
            )

    # Если ответ содержит код или длинный текст,
    # удаляем временное сообщение.
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

    # И отправляем нормальным способом.
    send_ai_response(
        chat_id,
        text
    )


# ============================================================
# AI
# ============================================================

def ask_ai_with_history(
    user_id,
    prompt
):

    mode = user_modes.get(
        user_id,
        "normal"
    )

    # ========================================================
    # СОЗДАНИЕ ИСТОРИИ
    # ========================================================

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

                "Не используй Markdown, "
                "кроме кодовых блоков."
            )

        else:

            sys_prompt = (
                "Ты полезный, дружелюбный и умный "
                "ИИ-ассистент. "

                "Отвечай строго на том же языке, "
                "на котором пишет пользователь. "

                "Если пользователь пишет на русском — "
                "отвечай на русском. "

                "Если пользователь пишет на английском — "
                "отвечай на английском. "

                "Если пользователь пишет на другом языке — "
                "отвечай на этом же языке. "

                "Можешь использовать позитивные эмодзи. "

                "Не используй Markdown в обычных ответах. "
                "Для программного кода используй "
                "Markdown-кодовые блоки."
            )

        user_histories[user_id] = [
            {
                "role": "system",
                "content": sys_prompt
            }
        ]

    # ========================================================
    # ОПРЕДЕЛЕНИЕ ЗАПРОСА ПРО КОД
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
    # ИНСТРУКЦИЯ ДЛЯ ПРОГРАММИРОВАНИЯ
    # ========================================================

    if is_coding_request:

        coding_instruction = (
            "ИНСТРУКЦИИ ДЛЯ ПРОГРАММИРОВАНИЯ:\n\n"

            "Пользователь работает с программным кодом.\n\n"

            "Отвечай максимально практически "
            "и подробно.\n\n"

            "Если пользователь просит написать код, "
            "предоставляй полноценный рабочий код.\n\n"

            "Если пользователь просит полный готовый "
            "код — предоставляй весь код целиком.\n\n"

            "Никогда не заменяй части кода фразами "
            "\"остальной код без изменений\", "
            "\"здесь остальной код\" или "
            "\"...\".\n\n"

            "Не используй многоточия вместо частей "
            "программного кода.\n\n"

            "Если пользователь прислал существующий "
            "проект и просит изменить конкретную часть, "
            "сохраняй остальные функции, если он "
            "не попросил их удалить.\n\n"

            "Учитывай импорты, зависимости, "
            "переменные окружения, функции, "
            "обработчики и связи между компонентами.\n\n"

            "Если исправляешь ошибку, исправляй "
            "причину проблемы, а не просто скрывай её.\n\n"

            "Большие фрагменты кода разрешено "
            "выдавать полностью.\n\n"

            "Каждый отдельный программный блок "
            "обязательно помещай в тройные обратные "
            "кавычки.\n\n"

            "Например:\n\n"

            "```python\n"
            "print('Hello')\n"
            "```\n\n"

            "Если ответ содержит объяснение и код, "
            "разделяй их: объяснение обычным текстом, "
            "код — внутри кодового блока.\n"
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

    # ========================================================
    # ДОБАВЛЯЕМ ЗАПРОС В ИСТОРИЮ
    # ========================================================

    user_histories[user_id].append(
        {
            "role": "user",
            "content": effective_prompt
        }
    )

    # ========================================================
    # ПАМЯТЬ
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
    # УРОВЕНЬ 1 — G4F
    # ========================================================

    models_to_try = [
        "gpt-3.5-turbo",
        "gpt-4o-mini",
        "gpt-4",
        "llama-3-70b"
    ]

    answer = ""
    success = False

    print(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    print(
        f"🤖 Новый запрос от {user_id}"
    )

    print(
        "🔄 Запускаю G4F..."
    )

    # ========================================================
    # ПРОБУЕМ ВСЕ G4F
    # ========================================================

    for model_name in models_to_try:

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

                print(
                    f"⚠️ G4F → {model_name}: "
                    "пустой ответ"
                )

                continue

            answer = str(
                answer
            ).strip()

            success = True

            print(
                f"✅ G4F → {model_name}: "
                "ответ получен"
            )

            break

        except Exception as e:

            print(
                f"❌ G4F → {model_name}: "
                f"{e}"
            )

    # ========================================================
    # УРОВЕНЬ 2 — GROQ GPT-OSS 120B
    # ========================================================

    if not success:

        print(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        print(
            "⚠️ Все G4F провайдеры "
            "не ответили."
        )

        if groq_client:

            print(
                "🔄 Переключаюсь на "
                "Groq GPT-OSS 120B..."
            )

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

                    print(
                        "✅ Groq GPT-OSS 120B: "
                        "ответ успешно получен!"
                    )

                else:

                    print(
                        "❌ Groq GPT-OSS 120B: "
                        "пустой ответ."
                    )

            except Exception as e:

                print(
                    "❌ Groq GPT-OSS 120B: "
                    f"{e}"
                )

        else:

            print(
                "❌ GROQ_API_KEY не найден."
            )

            print(
                "⚠️ Резервный Groq "
                "отключён."
            )

    # ========================================================
    # УСПЕШНЫЙ ОТВЕТ
    # ========================================================

    if success:

        user_histories[user_id].append(
            {
                "role": "assistant",
                "content": answer
            }
        )

        print(
            "🤖 Ответ отправлен."
        )

        print(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        return answer

    # ========================================================
    # ПОЛНОСТЬЮ НЕУДАЧНЫЙ ЗАПРОС
    # ========================================================

    user_histories[user_id].pop()

    print(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    print(
        "❌ Все AI-провайдеры "
        "не смогли обработать запрос."
    )

    print(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    if mode == "neuroham":

        return (
            "Даже мои процессоры решили "
            "сегодня саботировать работу 🙄"
        )

    return (
        "Не удалось получить ответ от ИИ.\n\n"
        "Я попробовал все доступные G4F-провайдеры "
        "и резервный Groq GPT-OSS 120B. "
        "Попробуй ещё раз немного позже."
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
                f"⚠️ Kira G4F "
                f"{model_name}: {e}"
            )

    # ========================================================
    # GROQ FALLBACK
    # ========================================================

    if groq_client:

        try:

            print(
                "🔄 Kira → Groq GPT-OSS 120B"
            )

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
                                "Пиши красивые, "
                                "тёплые и приятные тексты. "
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

                print(
                    "✅ Kira → Groq успешно"
                )

                return str(
                    answer
                ).strip()

        except Exception as e:

            print(
                f"❌ Groq Kira ошибка: {e}"
            )

    # ========================================================
    # FALLBACK TEXT
    # ========================================================

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
    # TAVILY
    # ========================================================

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

                title = result.get(
                    "title",
                    "Без заголовка"
                )

                content = result.get(
                    "content",
                    ""
                )

                results_text += (
                    f"- {title}: "
                    f"{content}\n"
                )

        except Exception as e:

            print(
                f"⚠️ Tavily ошибка: {e}"
            )

    # ========================================================
    # DUCKDUCKGO FALLBACK
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

                for result in results:

                    results_text += (
                        f"- "
                        f"{result.get('title', 'Без заголовка')}: "
                        f"{result.get('body', '')[:500]}\n"
                    )

        except Exception as e:

            results_text = (
                f"Не удалось выполнить поиск: {e}"
            )

    return results_text


# ============================================================
# ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЯ
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

                response_image = requests.get(
                    image_url,
                    timeout=25
                )

                if response_image.status_code == 200:

                    return response_image.content

        except Exception as e:

            print(
                f"⚠️ Генерация {model}: {e}"
            )

    return None


# ============================================================
# GEMINI IMAGE ANALYSIS
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
        "Привет! Я ИИ-ассистент 🤖\n\n"

        "Мои команды:\n\n"

        "/search <запрос> — поиск в интернете\n"
        "/weather <город> — погода\n"
        "/image <описание> — создать изображение\n"
        "/gemini <запрос> — спросить Gemini\n"
        "/fact [тема] — интересный факт\n"
        "/code <задача> — работа с кодом\n"
        "/sum <текст> — сделать выжимку\n"
        "/tr <текст> — перевод\n"
        "/fix <текст> — исправление текста\n"
        "/tts <текст> — озвучка\n"
        "/clear — очистить память\n"
        "/neuroham — режим Нейрохама\n\n"

        "Также можешь просто написать мне "
        "любой вопрос обычным сообщением."
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
        f"Вот результаты поиска из интернета "
        f"по запросу '{query}':\n\n"
        f"{raw_data}\n\n"

        "Сделай краткую и понятную выжимку "
        "на языке пользователя. "
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

            response = requests.get(
                url,
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
                f"Ошибка чтения ссылки: {e}"
            )

            return

    # ========================================================
    # ОБЫЧНЫЙ AI ЧАТ
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
            "Отправьте документ "
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

        extracted_pages = []

        # Первые 5 страниц,
        # как в предыдущей версии.
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

            "Сделай краткую и понятную "
            "выжимку из этого PDF:\n\n"
            +
            text[:6000]
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
# ЗАПУСК
# ============================================================

if __name__ == "__main__":

    print(
        "=" * 60
    )

    print(
        "🚀 Бот запускается..."
    )

    print(
        "=" * 60
    )

    print(
        "🎵 Музыкальный модуль: отключён"
    )

    print(
        "✨ Пасхалка Кира: включена"
    )

    print(
        "💻 Расширенный режим программирования: включён"
    )

    print(
        "📨 Длинные ответы: включены"
    )

    print(
        "📋 Кодовые блоки Telegram: включены"
    )

    print(
        "🔄 AI fallback: G4F → Groq GPT-OSS 120B"
    )

    if GROQ_API_KEY:

        print(
            "✅ GROQ_API_KEY найден"
        )

    else:

        print(
            "⚠️ GROQ_API_KEY НЕ найден — "
            "резервный Groq отключён"
        )

    print(
        "=" * 60
    )

    # ========================================================
    # FLASK
    # ========================================================

    threading.Thread(
        target=run_web,
        daemon=True
    ).start()

    # ========================================================
    # TELEGRAM POLLING
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
