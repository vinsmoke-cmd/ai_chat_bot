import os
import threading
import asyncio
import edge_tts
from flask import Flask
from openai import OpenAI
import telebot
from telebot.types import BotCommand
import requests
from bs4 import BeautifulSoup
import random
import time
import re

# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

OPENROUTER_KEY = (
    os.getenv("OPENROUTER_API_KEY")
    or os.getenv("GROQ_KEY")
    or os.getenv("GROQ_API_KEY")
)

GEMINI_KEY = os.getenv("GEMINI_API_KEY")

HF_TOKEN = os.getenv("HF_TOKEN")

MAX_HISTORY_LENGTH = 100
TELEGRAM_MAX_LENGTH = 4000

# ============================================================
# ПРОВЕРКА GOOGLE GENAI
# ============================================================

try:
    from google import genai
    from google.genai import types

    if GEMINI_KEY:
        gemini_client = genai.Client(
            api_key=GEMINI_KEY
        )
        print("Gemini Client initialized successfully.")
    else:
        gemini_client = None
        print("WARNING: GEMINI_API_KEY is not set.")

except Exception as e:
    gemini_client = None
    print("Gemini initialization error:", repr(e))

# ============================================================
# TELEGRAM
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в Environment Variables.")

bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode=None
)

# ============================================================
# OPENROUTER
# ============================================================

client = None

if OPENROUTER_KEY:
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENROUTER_KEY
    )

# ============================================================
# SYSTEM INSTRUCTION
# ============================================================

SYSTEM_INSTRUCTION = """
Ты умный, естественный и дружелюбный собеседник в Telegram.

Общайся естественно и без лишней официальности.
Можно иногда использовать лёгкий юмор.

Отвечай на языке пользователя.
Если пользователь просит перейти на другой язык, используй этот язык.

Не выдумывай факты.
Если не уверен, честно скажи об этом.

Не используй Markdown без необходимости.
Не используй звездочки для оформления.
Не используй решетки для оформления.
Не используй подчёркивания для оформления.

Если пользователь просит код:
- давай полный рабочий код;
- сохраняй правильные отступы;
- не пропускай важные части;
- не выдумывай несуществующие библиотеки.
"""

# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is active and running!"


@app.route("/health")
def health():
    return "OK"


def run_web():
    port = int(
        os.getenv(
            "PORT",
            "8080"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True
    )


# ============================================================
# ПАМЯТЬ
# ============================================================

dialog_history = {}
history_lock = threading.Lock()


def get_history(chat_id):
    with history_lock:
        return list(
            dialog_history.get(
                chat_id,
                []
            )
        )


def add_history(
    chat_id,
    role,
    content
):
    if not content:
        return

    with history_lock:

        if chat_id not in dialog_history:
            dialog_history[chat_id] = []

        dialog_history[chat_id].append(
            {
                "role": role,
                "content": content
            }
        )

        if len(dialog_history[chat_id]) > MAX_HISTORY_LENGTH * 2:
            dialog_history[chat_id] = (
                dialog_history[chat_id]
                [-(MAX_HISTORY_LENGTH * 2):]
            )


def clear_history(chat_id):
    with history_lock:
        dialog_history[chat_id] = []


# ============================================================
# ОЧИСТКА ТЕКСТА
# ============================================================

def clean_text(text):

    if not text:
        return ""

    text = str(text)

    text = re.sub(
        r"<think>.*?</think>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE
    )

    text = text.replace(
        "```",
        ""
    )

    text = text.replace(
        "**",
        ""
    )

    text = text.replace(
        "__",
        ""
    )

    text = text.replace(
        "~~",
        ""
    )

    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text
    )

    return text.strip()


# ============================================================
# TELEGRAM SAFE SEND
# ============================================================

def split_message(text):

    if not text:
        return [
            "Пустой ответ."
        ]

    if len(text) <= TELEGRAM_MAX_LENGTH:
        return [text]

    parts = []

    while len(text) > TELEGRAM_MAX_LENGTH:

        cut = text.rfind(
            "\n",
            0,
            TELEGRAM_MAX_LENGTH
        )

        if cut < 1000:
            cut = text.rfind(
                " ",
                0,
                TELEGRAM_MAX_LENGTH
            )

        if cut < 1000:
            cut = TELEGRAM_MAX_LENGTH

        parts.append(
            text[:cut]
        )

        text = text[cut:].lstrip()

    if text:
        parts.append(text)

    return parts


def safe_reply(
    message,
    text
):

    text = clean_text(text)

    if not text:
        text = "Не удалось получить ответ."

    for part in split_message(text):

        try:

            bot.reply_to(
                message,
                part
            )

        except Exception as e:

            print(
                "Telegram send error:",
                repr(e)
            )


# ============================================================
# OPENROUTER AI
# ============================================================

def query_ai(
    messages,
    temperature=0.8
):

    if not client:
        raise RuntimeError(
            "OPENROUTER_API_KEY/GROQ_KEY не задан."
        )

    model_name = (
        "deepseek/deepseek-r1:free"
    )

    response = client.chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=temperature
    )

    answer = (
        response
        .choices[0]
        .message
        .content
    )

    return clean_text(
        answer
    )


# ============================================================
# GEMINI
# ============================================================

GEMINI_MODEL = "gemini-2.5-flash"


def ask_gemini(
    prompt
):

    if not gemini_client:
        raise RuntimeError(
            "Gemini Client не инициализирован. "
            "Проверь GEMINI_API_KEY."
        )

    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.7,
            max_output_tokens=4096
        )
    )

    answer = response.text

    if not answer:
        raise RuntimeError(
            "Gemini вернул пустой ответ."
        )

    return clean_text(
        answer
    )


# ============================================================
# COMMANDS
# ============================================================

def setup_commands():

    try:

        bot.set_my_commands(
            [
                BotCommand(
                    "help",
                    "Список команд"
                ),
                BotCommand(
                    "image",
                    "Создать изображение"
                ),
                BotCommand(
                    "gemini",
                    "Спросить Gemini"
                ),
                BotCommand(
                    "search",
                    "Поиск в интернете"
                ),
                BotCommand(
                    "weather",
                    "Погода"
                ),
                BotCommand(
                    "fact",
                    "Случайный факт"
                ),
                BotCommand(
                    "code",
                    "Работа с кодом"
                ),
                BotCommand(
                    "sum",
                    "Краткая выжимка"
                ),
                BotCommand(
                    "tr",
                    "Перевод"
                ),
                BotCommand(
                    "fix",
                    "Исправить текст"
                ),
                BotCommand(
                    "tts",
                    "Озвучить текст"
                ),
                BotCommand(
                    "clear",
                    "Очистить память"
                )
            ]
        )

    except Exception as e:

        print(
            "Command setup error:",
            repr(e)
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
def send_welcome(message):

    bot.reply_to(
        message,
        "Привет! Я ИИ-помощник.\n\n"
        "Команды:\n"
        "/weather город — погода\n"
        "/fact — случайный факт\n"
        "/image описание — изображение\n"
        "/gemini запрос — Gemini\n"
        "/search запрос — поиск\n"
        "/tts текст — озвучка\n"
        "/code задача — код\n"
        "/sum текст — выжимка\n"
        "/tr текст — перевод\n"
        "/fix текст — исправление\n"
        "/clear — очистить память"
    )


# ============================================================
# CLEAR
# ============================================================

@bot.message_handler(
    commands=["clear"]
)
def handle_clear(message):

    clear_history(
        message.chat.id
    )

    bot.reply_to(
        message,
        "Память диалога очищена."
    )


# ============================================================
# WEATHER
# ============================================================

@bot.message_handler(
    commands=["weather"]
)
def handle_weather(message):

    text = (
        message.text
        or ""
    )

    parts = text.split(
        maxsplit=1
    )

    if len(parts) < 2:

        bot.reply_to(
            message,
            "Укажи город.\n"
            "Например: /weather Москва"
        )

        return

    city = parts[1].strip()

    try:

        geo_url = (
            "https://geocoding-api.open-meteo.com/"
            "v1/search"
        )

        geo_response = requests.get(
            geo_url,
            params={
                "name": city,
                "count": 1,
                "language": "ru",
                "format": "json"
            },
            timeout=10
        )

        geo_response.raise_for_status()

        geo_data = geo_response.json()

        results = geo_data.get(
            "results",
            []
        )

        if not results:

            bot.reply_to(
                message,
                "Город не найден."
            )

            return

        location = results[0]

        latitude = location["latitude"]
        longitude = location["longitude"]

        name = location.get(
            "name",
            city
        )

        weather_url = (
            "https://api.open-meteo.com/"
            "v1/forecast"
        )

        weather_response = requests.get(
            weather_url,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "current": (
                    "temperature_2m,"
                    "apparent_temperature,"
                    "relative_humidity_2m,"
                    "weather_code,"
                    "wind_speed_10m"
                ),
                "timezone": "auto"
            },
            timeout=10
        )

        weather_response.raise_for_status()

        data = weather_response.json()

        current = data.get(
            "current",
            {}
        )

        bot.reply_to(
            message,
            f"Погода в {name}:\n\n"
            f"Температура: "
            f"{current.get('temperature_2m')}°C\n"
            f"Ощущается как: "
            f"{current.get('apparent_temperature')}°C\n"
            f"Влажность: "
            f"{current.get('relative_humidity_2m')}%\n"
            f"Ветер: "
            f"{current.get('wind_speed_10m')} км/ч"
        )

    except Exception as e:

        print(
            "Weather error:",
            repr(e)
        )

        bot.reply_to(
            message,
            "Не удалось получить погоду."
        )


# ============================================================
# FACT
# ============================================================

@bot.message_handler(
    commands=["fact"]
)
def handle_fact(message):

    facts = [
        "У осьминога три сердца.",
        "Банан ботанически считается ягодой.",
        "На Венере сутки длиннее года.",
        "У жирафа и человека одинаковое количество шейных позвонков.",
        "Молния действительно может ударять в одно место несколько раз."
    ]

    bot.reply_to(
        message,
        random.choice(facts)
    )


# ============================================================
# IMAGE
# ============================================================

@bot.message_handler(
    commands=["image"]
)
def handle_image(message):

    parts = (
        message.text
        or ""
    ).split(
        maxsplit=1
    )

    if len(parts) < 2:

        bot.reply_to(
            message,
            "Напиши описание.\n"
            "Например:\n"
            "/image космический кот"
        )

        return

    prompt = parts[1].strip()

    try:

        english_prompt = prompt

        if client:

            try:

                english_prompt = query_ai(
                    [
                        {
                            "role": "system",
                            "content":
                                "Translate the image request "
                                "into a detailed English prompt "
                                "for an image generator. "
                                "Return only the prompt."
                        },
                        {
                            "role": "user",
                            "content": prompt
                        }
                    ],
                    temperature=0.7
                )

            except Exception as e:

                print(
                    "Image prompt translation error:",
                    repr(e)
                )

        # ----------------------------------------------------
        # HUGGING FACE
        # ----------------------------------------------------

        if HF_TOKEN:

            try:

                hf_url = (
                    "https://api-inference.huggingface.co/"
                    "models/"
                    "black-forest-labs/FLUX.1-schnell"
                )

                response = requests.post(
                    hf_url,
                    headers={
                        "Authorization":
                            f"Bearer {HF_TOKEN}"
                    },
                    json={
                        "inputs":
                            english_prompt
                    },
                    timeout=60
                )

                if (
                    response.status_code == 200
                    and len(response.content) > 1000
                ):

                    bot.send_photo(
                        message.chat.id,
                        response.content,
                        caption=(
                            "Готово.\n"
                            + prompt
                        )
                    )

                    return

            except Exception as e:

                print(
                    "Hugging Face error:",
                    repr(e)
                )

        # ----------------------------------------------------
        # POLLINATIONS FALLBACK
        # ----------------------------------------------------

        encoded = requests.utils.quote(
            english_prompt
        )

        seed = random.randint(
            1,
            9999999
        )

        image_url = (
            "https://image.pollinations.ai/"
            "prompt/"
            + encoded
            + f"?model=flux&seed={seed}"
            "&width=1024"
            "&height=1024"
            "&nologo=true"
        )

        bot.send_photo(
            message.chat.id,
            image_url,
            caption=(
                "Готово.\n"
                + prompt
            )
        )

    except Exception as e:

        print(
            "Image error:",
            repr(e)
        )

        bot.reply_to(
            message,
            "Не удалось создать изображение."
        )


# ============================================================
# GEMINI COMMAND
# ============================================================

@bot.message_handler(
    commands=["gemini"]
)
def handle_gemini(message):

    if not gemini_client:

        bot.reply_to(
            message,
            "Gemini сейчас недоступен.\n\n"
            "Причина: Gemini Client не был "
            "инициализирован.\n\n"
            "Проверь GEMINI_API_KEY в Render."
        )

        return

    text = (
        message.text
        or ""
    )

    parts = text.split(
        maxsplit=1
    )

    if len(parts) < 2:

        bot.reply_to(
            message,
            "Напиши запрос после /gemini."
        )

        return

    query = parts[1].strip()

    if not query:
        bot.reply_to(
            message,
            "Напиши запрос после /gemini."
        )
        return

    bot.send_chat_action(
        message.chat.id,
        "typing"
    )

    try:

        answer = ask_gemini(
            query
        )

        safe_reply(
            message,
            answer
        )

    except Exception as e:

        print(
            "Gemini error:",
            repr(e)
        )

        bot.reply_to(
            message,
            "Ошибка Gemini:\n"
            + str(e)
        )


# ============================================================
# PHOTO / GEMINI VISION
# ============================================================

@bot.message_handler(
    content_types=["photo"]
)
def handle_photo(message):

    if not gemini_client:

        bot.reply_to(
            message,
            "Анализ фото недоступен.\n"
            "Проверь GEMINI_API_KEY."
        )

        return

    bot.send_chat_action(
        message.chat.id,
        "typing"
    )

    try:

        file_info = bot.get_file(
            message.photo[-1].file_id
        )

        image_bytes = bot.download_file(
            file_info.file_path
        )

        caption = (
            message.caption
            or
            "Опиши это изображение."
        )

        image_part = types.Part.from_bytes(
            data=image_bytes,
            mime_type="image/jpeg"
        )

        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                image_part,
                caption
            ],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0.5,
                max_output_tokens=4096
            )
        )

        answer = response.text

        if not answer:
            answer = "Gemini не смог вернуть описание."

        safe_reply(
            message,
            answer
        )

    except Exception as e:

        print(
            "Gemini vision error:",
            repr(e)
        )

        bot.reply_to(
            message,
            "Ошибка анализа изображения:\n"
            + str(e)
        )


# ============================================================
# TTS
# ============================================================

@bot.message_handler(
    commands=["tts"]
)
def handle_tts(message):

    parts = (
        message.text
        or ""
    ).split(
        maxsplit=1
    )

    if len(parts) < 2:

        bot.reply_to(
            message,
            "Напиши текст после /tts."
        )

        return

    text = parts[1].strip()

    filename = (
        f"voice_"
        f"{message.chat.id}_"
        f"{message.message_id}.mp3"
    )

    try:

        async def generate():

            communicate = edge_tts.Communicate(
                text,
                "ru-RU-SvetlanaNeural"
            )

            await communicate.save(
                filename
            )

        asyncio.run(
            generate()
        )

        with open(
            filename,
            "rb"
        ) as voice:

            bot.send_voice(
                message.chat.id,
                voice
            )

    except Exception as e:

        print(
            "TTS error:",
            repr(e)
        )

        bot.reply_to(
            message,
            "Ошибка озвучки."
        )

    finally:

        try:

            if os.path.exists(
                filename
            ):
                os.remove(
                    filename
                )

        except Exception:
            pass


# ============================================================
# SEARCH
# ============================================================

@bot.message_handler(
    commands=["search"]
)
def handle_search(message):

    if not client:

        bot.reply_to(
            message,
            "Поиск недоступен: "
            "OPENROUTER_API_KEY не задан."
        )

        return

    parts = (
        message.text
        or ""
    ).split(
        maxsplit=1
    )

    if len(parts) < 2:

        bot.reply_to(
            message,
            "Напиши запрос после /search."
        )

        return

    query = parts[1].strip()

    try:

        from googlesearch import search

        urls = list(
            search(
                query,
                num_results=5
            )
        )

        if not urls:

            bot.reply_to(
                message,
                "Ничего не найдено."
            )

            return

        sources = []

        headers = {
            "User-Agent":
                "Mozilla/5.0"
        }

        for url in urls:

            try:

                response = requests.get(
                    url,
                    headers=headers,
                    timeout=8
                )

                if response.status_code != 200:
                    continue

                soup = BeautifulSoup(
                    response.text,
                    "html.parser"
                )

                for element in soup(
                    [
                        "script",
                        "style",
                        "noscript"
                    ]
                ):
                    element.decompose()

                text = soup.get_text(
                    separator=" ",
                    strip=True
                )

                text = re.sub(
                    r"\s+",
                    " ",
                    text
                )

                sources.append(
                    f"Источник: {url}\n"
                    f"{text[:2000]}"
                )

            except Exception:
                continue

        if not sources:

            bot.reply_to(
                message,
                "Не удалось прочитать найденные сайты."
            )

            return

        search_text = (
            "\n\n".join(
                sources
            )
        )

        answer = query_ai(
            [
                {
                    "role":
                        "system",
                    "content":
                        SYSTEM_INSTRUCTION
                },
                {
                    "role":
                        "user",
                    "content":
                        (
                            f"Запрос: {query}\n\n"
                            f"Данные из интернета:\n"
                            f"{search_text}\n\n"
                            "Сформируй точный и понятный ответ."
                        )
                }
            ],
            temperature=0.4
        )

        safe_reply(
            message,
            answer
        )

    except Exception as e:

        print(
            "Search error:",
            repr(e)
        )

        bot.reply_to(
            message,
            "Ошибка поиска."
        )


# ============================================================
# CODE / SUM / TR / FIX
# ============================================================

@bot.message_handler(
    commands=[
        "code",
        "sum",
        "tr",
        "fix"
    ]
)
def handle_special_commands(message):

    if not client:

        bot.reply_to(
            message,
            "OpenRouter/Groq API ключ не задан."
        )

        return

    text = (
        message.text
        or ""
    )

    parts = text.split(
        maxsplit=1
    )

    command = (
        parts[0]
        .split("@")[0]
        .lower()
    )

    if len(parts) < 2:

        bot.reply_to(
            message,
            f"Напиши текст после {command}."
        )

        return

    user_text = parts[1].strip()

    instructions = {

        "/code":
            "Напиши, исправь или объясни код.",

        "/sum":
            "Сделай краткую выжимку текста.",

        "/tr":
            "Переведи текст. Если язык указан пользователем, "
            "переводи именно на него.",

        "/fix":
            "Исправь ошибки текста, сохранив его смысл."
    }

    try:

        answer = query_ai(
            [
                {
                    "role":
                        "system",
                    "content":
                        (
                            SYSTEM_INSTRUCTION
                            + "\n\n"
                            + instructions.get(
                                command,
                                ""
                            )
                        )
                },
                {
                    "role":
                        "user",
                    "content":
                        user_text
                }
            ],
            temperature=0.4
        )

        safe_reply(
            message,
            answer
        )

    except Exception as e:

        print(
            "Special command error:",
            repr(e)
        )

        bot.reply_to(
            message,
            "Произошла ошибка."
        )


# ============================================================
# ОБЫЧНЫЙ ДИАЛОГ
# ============================================================

@bot.message_handler(
    func=lambda message: True,
    content_types=["text"]
)
def handle_text_message(message):

    if not client:

        bot.reply_to(
            message,
            "Основной ИИ временно недоступен: "
            "API ключ не задан."
        )

        return

    chat_id = message.chat.id

    user_text = (
        message.text
        or ""
    ).strip()

    if not user_text:
        return

    try:

        history = get_history(
            chat_id
        )

        messages = [
            {
                "role":
                    "system",
                "content":
                    SYSTEM_INSTRUCTION
            }
        ]

        messages.extend(
            history
        )

        messages.append(
            {
                "role":
                    "user",
                "content":
                    user_text
            }
        )

        answer = query_ai(
            messages,
            temperature=0.8
        )

        add_history(
            chat_id,
            "user",
            user_text
        )

        add_history(
            chat_id,
            "assistant",
            answer
        )

        safe_reply(
            message,
            answer
        )

    except Exception as e:

        print(
            "Chat error:",
            repr(e)
        )

        bot.reply_to(
            message,
            "Ошибка при обращении к ИИ."
        )


# ============================================================
# TELEGRAM POLLING
# ============================================================

def polling_loop():

    while True:

        try:

            print(
                "Starting Telegram polling..."
            )

            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                skip_pending=True
            )

        except Exception as e:

            error_text = str(e)

            print(
                "Polling error:",
                repr(e)
            )

            # Telegram 409:
            # другой экземпляр уже использует getUpdates.
            if (
                "409" in error_text
                or
                "Conflict" in error_text
                or
                "terminated by other getUpdates" in error_text
            ):

                print(
                    "Telegram 409 detected."
                )

                print(
                    "Waiting 15 seconds before retry..."
                )

                time.sleep(15)

            else:

                print(
                    "Waiting 5 seconds before retry..."
                )

                time.sleep(5)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    print(
        "======================================"
    )

    print(
        "AI Telegram Bot starting..."
    )

    print(
        "OpenRouter:",
        "OK" if client else "NO"
    )

    print(
        "Gemini:",
        "OK" if gemini_client else "NO"
    )

    print(
        "Hugging Face:",
        "OK" if HF_TOKEN else "NO"
    )

    print(
        "======================================"
    )

    setup_commands()

    web_thread = threading.Thread(
        target=run_web,
        daemon=True
    )

    web_thread.start()

    polling_loop()
