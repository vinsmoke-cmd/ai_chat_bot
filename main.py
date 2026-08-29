import os
import threading
import asyncio
import random
import re
import time
from urllib.parse import quote_plus

import requests
import edge_tts
import telebot

from flask import Flask
from groq import Groq
from telebot.types import BotCommand
from bs4 import BeautifulSoup
from pypdf import PdfReader

# Gemini через НОВУЮ библиотеку google-genai
from google import genai


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

GROQ_KEY = (
    os.getenv("GROQ_KEY")
    or os.getenv("GROQ_API_KEY")
)

GEMINI_API_KEY = (
    os.getenv("GEMINI_API_KEY")
    or os.getenv("GOOGLE_API_KEY")
)

# Основная модель Groq
FIXED_MODEL = "openai/gpt-oss-120b"

# Gemini
GEMINI_MODEL = "gemini-2.5-flash"

# Максимум сообщений в памяти одного чата
# 1000 сообщений = примерно 500 пар вопрос/ответ
MAX_HISTORY_LENGTH = 1000

# Максимальная длина одного сообщения Telegram
TELEGRAM_MAX_LENGTH = 4000

# HTTP timeout
HTTP_TIMEOUT = 12


# ============================================================
# ПРОВЕРКА НАСТРОЕК
# ============================================================

print("======================================")
print("Checking environment variables...")

if BOT_TOKEN:
    print("BOT_TOKEN: OK")
else:
    print("BOT_TOKEN: NO")

if GROQ_KEY:
    print("GROQ_KEY: OK")
else:
    print("GROQ_KEY: NO")

if GEMINI_API_KEY:
    print("GEMINI_API_KEY: OK")
else:
    print("GEMINI_API_KEY: NO")

print("======================================")


# ============================================================
# TELEGRAM
# ============================================================

bot = None

if BOT_TOKEN:
    bot = telebot.TeleBot(
        BOT_TOKEN,
        parse_mode=None
    )


# ============================================================
# GROQ
# ============================================================

groq_client = None

if GROQ_KEY:
    try:
        groq_client = Groq(
            api_key=GROQ_KEY
        )

        print("Groq initialized.")

    except Exception as e:
        print(
            "Groq initialization error:",
            e
        )


# ============================================================
# GEMINI
# ============================================================

gemini_client = None

if GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(
            api_key=GEMINI_API_KEY
        )

        print(
            "Gemini initialized using google-genai."
        )
        print(
            "Gemini model:",
            GEMINI_MODEL
        )

    except Exception as e:
        print(
            "Gemini initialization error:",
            e
        )


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "AI Chat Bot is running!"


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
# ПАМЯТЬ ДИАЛОГОВ
# ============================================================

dialog_history = {}

history_lock = threading.Lock()


def get_history(chat_id):
    with history_lock:

        if chat_id not in dialog_history:
            dialog_history[chat_id] = []

        return list(
            dialog_history[chat_id]
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

        if (
            len(dialog_history[chat_id])
            > MAX_HISTORY_LENGTH
        ):
            dialog_history[chat_id] = (
                dialog_history[chat_id]
                [-MAX_HISTORY_LENGTH:]
            )


def clear_chat_history(chat_id):
    with history_lock:
        dialog_history[chat_id] = []


# ============================================================
# СИСТЕМНАЯ ИНСТРУКЦИЯ
# ============================================================

SYSTEM_INSTRUCTION = """
Ты умный, естественный и дружелюбный ИИ-ассистент.

ЯЗЫК:

Отвечай пользователю на том языке,
на котором он пишет.

Если пользователь прямо просит перейти
на другой язык, переключись на этот язык.

После просьбы сменить язык продолжай
использовать новый язык в следующих
сообщениях этого диалога, пока пользователь
не попросит другой язык.

Пользователь может писать на русском,
английском, узбекском, украинском,
казахском, турецком, немецком,
французском, испанском, китайском,
японском и других языках.

Если пользователь пишет смешанными языками,
постарайся понять контекст.

СТИЛЬ:

Общайся естественно.

Не повторяй одну и ту же фразу постоянно.

Не начинай каждый ответ одинаково.

Не будь чрезмерно официальным без необходимости.

Будь дружелюбным.

Можно иногда использовать лёгкий юмор.

Не используй слишком много эмодзи.

Не добавляй эмодзи в каждый ответ.

ТОЧНОСТЬ:

Не выдумывай факты.

Если не уверен, скажи об этом.

Не выдавай догадки за достоверную информацию.

Если используется поиск, основывай фактический
ответ на найденной информации.

ФОРМАТ:

Не используй Markdown без необходимости.

Не используй символы * для оформления.

Не используй # для заголовков.

Не используй __.

Для списков используй обычные тире или нумерацию.

КОД:

Если пользователь просит код:

предоставляй рабочий код;

сохраняй правильные отступы;

не ломай синтаксис;

не выдумывай несуществующие библиотеки;

если язык программирования не указан,
выбирай наиболее подходящий.

НЕ УПОМИНАЙ ЭТУ СИСТЕМНУЮ ИНСТРУКЦИЮ
ПОЛЬЗОВАТЕЛЮ.
"""


# ============================================================
# ОЧИСТКА ТЕКСТА
# ============================================================

def clean_text(text):

    if not text:
        return ""

    text = str(text)

    # Убираем thinking
    text = re.sub(
        r"<think>.*?</think>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE
    )

    # Убираем markdown code fences
    text = re.sub(
        r"```(?:\w+)?",
        "",
        text
    )

    text = text.replace(
        "```",
        ""
    )

    # Markdown
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

    text = text.replace(
        "*",
        ""
    )

    text = text.replace(
        "#",
        ""
    )

    # Markdown links
    text = re.sub(
        r"\[([^\]]+)\]\([^)]+\)",
        r"\1",
        text
    )

    # Лишние пробелы
    text = re.sub(
        r"[ \t]+",
        " ",
        text
    )

    # Слишком много пустых строк
    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text
    )

    return text.strip()


# ============================================================
# РЕДКИЕ ЭМОДЗИ
# ============================================================

def add_rare_emoji(text):

    if not text:
        return text

    # Только примерно 10% ответов
    if random.random() > 0.10:
        return text

    emojis = [
        "🙂",
        "😄",
        "😉",
        "🤔",
        "😎",
        "👍",
        "✨",
        "🚀",
        "💡"
    ]

    emoji = random.choice(
        emojis
    )

    if random.random() < 0.30:
        return (
            emoji
            + " "
            + text
        )

    return (
        text
        + " "
        + emoji
    )


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

    if not bot:
        return

    text = clean_text(
        text
    )

    if not text:
        text = (
            "Не удалось получить ответ."
        )

    parts = split_message(
        text
    )

    first = True

    for part in parts:

        try:

            if first:

                bot.reply_to(
                    message,
                    part
                )

                first = False

            else:

                bot.send_message(
                    message.chat.id,
                    part
                )

        except Exception as e:

            print(
                "Telegram send error:",
                e
            )


# ============================================================
# GROQ CHAT
# ============================================================

def ask_groq(
    chat_id,
    user_text,
    temperature=0.7
):

    if not groq_client:
        raise RuntimeError(
            "GROQ_KEY не задан."
        )

    history = get_history(
        chat_id
    )

    messages = [
        {
            "role": "system",
            "content": SYSTEM_INSTRUCTION
        }
    ]

    for item in history:

        role = item.get(
            "role"
        )

        content = item.get(
            "content"
        )

        if (
            role in (
                "user",
                "assistant"
            )
            and content
        ):

            messages.append(
                {
                    "role": role,
                    "content": content
                }
            )

    messages.append(
        {
            "role": "user",
            "content": user_text
        }
    )

    response = (
        groq_client
        .chat
        .completions
        .create(
            model=FIXED_MODEL,
            messages=messages,
            temperature=temperature,
            max_tokens=4096
        )
    )

    answer = (
        response
        .choices[0]
        .message
        .content
    )

    answer = clean_text(
        answer
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

    return answer


# ============================================================
# COMMANDS
# ============================================================

def setup_commands():

    if not bot:
        return

    commands = [
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

    try:

        bot.set_my_commands(
            commands
        )

        print(
            "Telegram commands configured."
        )

    except Exception as e:

        print(
            "Command setup error:",
            e
        )


# ============================================================
# START / HELP
# ============================================================

if bot:

    @bot.message_handler(
        commands=[
            "start",
            "help"
        ]
    )
    def send_welcome(message):

        text = (
            "Привет! Я ИИ-ассистент 🤖\n\n"

            "Я умею:\n"
            "- Общаться на разных языках\n"
            "- Запоминать контекст диалога\n"
            "- Анализировать фотографии\n"
            "- Работать с кодом\n"
            "- Переводить тексты\n"
            "- Искать информацию в интернете\n"
            "- Показывать погоду\n"
            "- Генерировать изображения\n"
            "- Озвучивать текст\n"
            "- Рассказывать факты\n\n"

            "Команды:\n"
            "/search запрос — поиск\n"
            "/weather город — погода\n"
            "/image описание — изображение\n"
            "/gemini запрос — Gemini\n"
            "/code задача — код\n"
            "/sum текст — выжимка\n"
            "/tr текст — перевод\n"
            "/fix текст — исправление\n"
            "/tts текст — озвучка\n"
            "/fact — случайный факт\n"
            "/clear — очистить память\n\n"

            "В группах:\n"
            "/weather@имя_бота Москва\n\n"

            "Обычное сообщение в группе "
            "обрабатывается при упоминании "
            "бота или ответе на сообщение бота."
        )

        bot.reply_to(
            message,
            text
        )


# ============================================================
# CLEAR
# ============================================================

if bot:

    @bot.message_handler(
        commands=["clear"]
    )
    def handle_clear(message):

        clear_chat_history(
            message.chat.id
        )

        bot.reply_to(
            message,
            "Память этого диалога очищена."
        )


# ============================================================
# COMMAND ARGUMENT
# ============================================================

def extract_command_argument(message):

    text = (
        message.text
        or ""
    ).strip()

    if not text:
        return ""

    parts = text.split(
        maxsplit=1
    )

    if len(parts) == 1:
        return ""

    return parts[1].strip()


# ============================================================
# WEATHER
# ============================================================

def get_coordinates(city):

    encoded = quote_plus(
        city
    )

    urls = [
        (
            "https://geocoding-api.open-meteo.com/"
            "v1/search"
            f"?name={encoded}"
            "&count=5"
            "&language=en"
            "&format=json"
        ),
        (
            "https://nominatim.openstreetmap.org/"
            "search"
            f"?q={encoded}"
            "&format=json"
            "&limit=5"
        )
    ]

    headers = {
        "User-Agent":
            "AIChatBot/1.0"
    }

    for url in urls:

        try:

            response = requests.get(
                url,
                headers=headers,
                timeout=10
            )

            if response.status_code != 200:
                continue

            data = response.json()

            # Open-Meteo
            if "results" in data:

                results = data.get(
                    "results",
                    []
                )

                if results:

                    result = results[0]

                    return {
                        "name":
                            result.get(
                                "name",
                                city
                            ),
                        "latitude":
                            result.get(
                                "latitude"
                            ),
                        "longitude":
                            result.get(
                                "longitude"
                            ),
                        "country":
                            result.get(
                                "country",
                                ""
                            )
                    }

            # Nominatim
            if (
                isinstance(
                    data,
                    list
                )
                and data
            ):

                result = data[0]

                return {
                    "name":
                        result.get(
                            "display_name",
                            city
                        ).split(",")[0],

                    "latitude":
                        float(
                            result["lat"]
                        ),

                    "longitude":
                        float(
                            result["lon"]
                        ),

                    "country":
                        ""
                }

        except Exception as e:

            print(
                "Geocoding error:",
                e
            )

    return None


def weather_code_text(code):

    codes = {
        0: "Ясно",
        1: "Преимущественно ясно",
        2: "Переменная облачность",
        3: "Пасмурно",
        45: "Туман",
        48: "Изморозь и туман",
        51: "Небольшая морось",
        53: "Морось",
        55: "Сильная морось",
        61: "Небольшой дождь",
        63: "Дождь",
        65: "Сильный дождь",
        71: "Небольшой снег",
        73: "Снег",
        75: "Сильный снег",
        80: "Ливни",
        81: "Ливни",
        82: "Сильные ливни",
        95: "Гроза",
        96: "Гроза с градом",
        99: "Сильная гроза с градом"
    }

    return codes.get(
        code,
        "Неизвестные условия"
    )


if bot:

    @bot.message_handler(
        commands=["weather"]
    )
    def handle_weather(message):

        city = extract_command_argument(
            message
        )

        if not city:

            bot.reply_to(
                message,
                "Укажи город.\n\n"
                "Например:\n"
                "/weather Москва\n"
                "/weather Ташкент\n"
                "/weather London"
            )

            return

        bot.send_chat_action(
            message.chat.id,
            "typing"
        )

        try:

            location = get_coordinates(
                city
            )

            if not location:

                bot.reply_to(
                    message,
                    "Не смог найти этот город. "
                    "Попробуй указать страну."
                )

                return

            lat = location["latitude"]
            lon = location["longitude"]

            weather_url = (
                "https://api.open-meteo.com/"
                "v1/forecast"
                f"?latitude={lat}"
                f"&longitude={lon}"
                "&current=temperature_2m,"
                "relative_humidity_2m,"
                "apparent_temperature,"
                "weather_code,"
                "wind_speed_10m"
                "&timezone=auto"
            )

            response = requests.get(
                weather_url,
                timeout=10
            )

            response.raise_for_status()

            data = response.json()

            current = data.get(
                "current",
                {}
            )

            temperature = current.get(
                "temperature_2m"
            )

            apparent = current.get(
                "apparent_temperature"
            )

            humidity = current.get(
                "relative_humidity_2m"
            )

            wind = current.get(
                "wind_speed_10m"
            )

            code = current.get(
                "weather_code"
            )

            description = weather_code_text(
                code
            )

            name = location.get(
                "name",
                city
            )

            country = location.get(
                "country",
                ""
            )

            title = name

            if country:
                title += (
                    ", "
                    + country
                )

            weather_text = (
                f"Погода: {title}\n\n"
                f"Состояние: {description}\n"
                f"Температура: {temperature}°C\n"
                f"Ощущается как: {apparent}°C\n"
                f"Влажность: {humidity}%\n"
                f"Ветер: {wind} км/ч"
            )

            bot.reply_to(
                message,
                weather_text
            )

        except Exception as e:

            print(
                "Weather error:",
                e
            )

            bot.reply_to(
                message,
                "Не удалось получить погоду."
            )


# ============================================================
# FACT
# ============================================================

if bot:

    @bot.message_handler(
        commands=["fact"]
    )
    def handle_fact(message):

        facts = [
            "У осьминогов три сердца.",
            "Банан с точки зрения ботаники является ягодой.",
            "На Венере день длится дольше её года.",
            "Молния может несколько раз ударить в одно и то же место.",
            "У акул появились предки раньше первых динозавров.",
            "Некоторые виды бамбука растут очень быстро.",
            "У ворон хорошо развиты способности к решению задач.",
            "У человека и жирафа одинаковое количество шейных позвонков."
        ]

        bot.reply_to(
            message,
            random.choice(facts)
        )


# ============================================================
# IMAGE GENERATION
# ============================================================

if bot:

    @bot.message_handler(
        commands=["image"]
    )
    def handle_image(message):

        prompt = extract_command_argument(
            message
        )

        if not prompt:

            bot.reply_to(
                message,
                "Напиши, что нужно нарисовать.\n\n"
                "Например:\n"
                "/image космический кот на Марсе"
            )

            return

        bot.send_chat_action(
            message.chat.id,
            "upload_photo"
        )

        try:

            english_prompt = prompt

            if groq_client:

                try:

                    response = (
                        groq_client
                        .chat
                        .completions
                        .create(
                            model=FIXED_MODEL,
                            messages=[
                                {
                                    "role": "system",
                                    "content":
                                        "Translate the image "
                                        "request into a detailed "
                                        "English prompt for an "
                                        "image generator. "
                                        "Return only the prompt."
                                },
                                {
                                    "role": "user",
                                    "content": prompt
                                }
                            ],
                            temperature=0.5,
                            max_tokens=1000
                        )
                    )

                    english_prompt = clean_text(
                        response
                        .choices[0]
                        .message
                        .content
                    )

                except Exception as e:

                    print(
                        "Image prompt translation error:",
                        e
                    )

            encoded = quote_plus(
                english_prompt
            )

            image_url = (
                "https://image.pollinations.ai/"
                "prompt/"
                + encoded
            )

            bot.send_photo(
                message.chat.id,
                image_url,
                caption=(
                    "Изображение по запросу:\n"
                    + prompt
                )
            )

        except Exception as e:

            print(
                "Image error:",
                e
            )

            bot.reply_to(
                message,
                "Не удалось создать изображение."
            )


# ============================================================
# GEMINI
# ============================================================

def ask_gemini(
    prompt
):

    if not gemini_client:

        raise RuntimeError(
            "GEMINI_API_KEY не задан "
            "или Gemini не был инициализирован."
        )

    response = (
        gemini_client
        .models
        .generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )
    )

    if not response:
        raise RuntimeError(
            "Gemini вернул пустой ответ."
        )

    answer = getattr(
        response,
        "text",
        None
    )

    if not answer:

        raise RuntimeError(
            "Gemini не вернул текстовый ответ."
        )

    return clean_text(
        answer
    )


if bot:

    @bot.message_handler(
        commands=["gemini"]
    )
    def handle_gemini(message):

        query = extract_command_argument(
            message
        )

        if not query:

            bot.reply_to(
                message,
                "Напиши вопрос после /gemini.\n\n"
                "Например:\n"
                "/gemini Объясни теорию относительности"
            )

            return

        if not gemini_client:

            bot.reply_to(
                message,
                "Gemini сейчас недоступен.\n\n"
                "Причина: GEMINI_API_KEY не был "
                "инициализирован в Render."
            )

            print(
                "Gemini command rejected: "
                "gemini_client is None."
            )

            return

        bot.send_chat_action(
            message.chat.id,
            "typing"
        )

        try:

            prompt = (
                SYSTEM_INSTRUCTION
                + "\n\n"
                "Запрос пользователя:\n"
                + query
            )

            answer = ask_gemini(
                prompt
            )

            answer = add_rare_emoji(
                answer
            )

            safe_reply(
                message,
                answer
            )

        except Exception as e:

            print(
                "Gemini API error:",
                repr(e)
            )

            bot.reply_to(
                message,
                "Ошибка Gemini.\n\n"
                "Подробная причина записана "
                "в логах Render."
            )


# ============================================================
# TTS
# ============================================================

def detect_tts_voice(text):

    # Русский
    if re.search(
        r"[а-яА-ЯёЁ]",
        text
    ):
        return "ru-RU-DmitryNeural"

    # Узбекский
    if re.search(
        r"[ўқғҳЎҚҒҲ]",
        text
    ):
        return "uz-UZ-SardorNeural"

    # Казахский
    if re.search(
        r"[әіңғүұқөһӘІҢҒҮҰҚӨҺ]",
        text
    ):
        return "kk-KZ-DauletNeural"

    return "en-US-GuyNeural"


if bot:

    @bot.message_handler(
        commands=["tts"]
    )
    def handle_tts(message):

        text = extract_command_argument(
            message
        )

        if not text:

            bot.reply_to(
                message,
                "Напиши текст после /tts."
            )

            return

        filename = (
            "voice_"
            + str(message.chat.id)
            + "_"
            + str(message.message_id)
            + ".mp3"
        )

        bot.send_chat_action(
            message.chat.id,
            "record_voice"
        )

        try:

            voice = detect_tts_voice(
                text
            )

            async def generate():

                communicator = (
                    edge_tts.Communicate(
                        text,
                        voice
                    )
                )

                await communicator.save(
                    filename
                )

            asyncio.run(
                generate()
            )

            with open(
                filename,
                "rb"
            ) as audio:

                bot.send_voice(
                    message.chat.id,
                    audio
                )

        except Exception as e:

            print(
                "TTS error:",
                e
            )

            bot.reply_to(
                message,
                "Не удалось озвучить текст."
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
# SEARCH HELPERS
# ============================================================

SEARCH_HEADERS = {
    "User-Agent":
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/128.0 Safari/537.36"
}


def normalize_url(url):

    if not url:
        return ""

    url = url.strip()

    if not url.startswith(
        (
            "http://",
            "https://"
        )
    ):
        return ""

    return url


def search_duckduckgo(query):

    url = (
        "https://html.duckduckgo.com/html/"
        "?q="
        + quote_plus(query)
    )

    response = requests.get(
        url,
        headers=SEARCH_HEADERS,
        timeout=HTTP_TIMEOUT
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    results = []

    for result in soup.select(
        ".result"
    ):

        link = result.select_one(
            ".result__a"
        )

        if not link:
            continue

        href = normalize_url(
            link.get("href")
        )

        title = link.get_text(
            " ",
            strip=True
        )

        snippet_element = (
            result.select_one(
                ".result__snippet"
            )
        )

        snippet = ""

        if snippet_element:

            snippet = (
                snippet_element
                .get_text(
                    " ",
                    strip=True
                )
            )

        if href:

            results.append(
                {
                    "title": title,
                    "url": href,
                    "snippet": snippet
                }
            )

        if len(results) >= 5:
            break

    return results


def search_bing(query):

    url = (
        "https://www.bing.com/search?"
        "q="
        + quote_plus(query)
    )

    response = requests.get(
        url,
        headers=SEARCH_HEADERS,
        timeout=HTTP_TIMEOUT
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    results = []

    for item in soup.select(
        "li.b_algo"
    ):

        link = item.select_one(
            "h2 a"
        )

        if not link:
            continue

        href = normalize_url(
            link.get("href")
        )

        title = link.get_text(
            " ",
            strip=True
        )

        paragraph = item.select_one(
            ".b_caption p"
        )

        snippet = ""

        if paragraph:

            snippet = (
                paragraph
                .get_text(
                    " ",
                    strip=True
                )
            )

        if href:

            results.append(
                {
                    "title": title,
                    "url": href,
                    "snippet": snippet
                }
            )

        if len(results) >= 5:
            break

    return results


def search_searxng(query):

    custom_url = os.getenv(
        "SEARXNG_URL"
    )

    if not custom_url:
        return []

    custom_url = custom_url.rstrip(
        "/"
    )

    if not custom_url.endswith(
        "/search"
    ):
        custom_url += "/search"

    response = requests.get(
        custom_url,
        params={
            "q": query,
            "format": "json",
            "language": "auto",
            "safesearch": 1
        },
        headers=SEARCH_HEADERS,
        timeout=HTTP_TIMEOUT
    )

    response.raise_for_status()

    data = response.json()

    results = []

    for item in data.get(
        "results",
        []
    )[:5]:

        url = normalize_url(
            item.get(
                "url",
                ""
            )
        )

        if not url:
            continue

        results.append(
            {
                "title":
                    item.get(
                        "title",
                        ""
                    ),

                "url":
                    url,

                "snippet":
                    item.get(
                        "content",
                        ""
                    )
            }
        )

    return results


def perform_search(query):

    methods = [
        (
            "DuckDuckGo",
            search_duckduckgo
        ),
        (
            "Bing",
            search_bing
        ),
        (
            "SearXNG",
            search_searxng
        )
    ]

    for name, function in methods:

        try:

            results = function(
                query
            )

            if results:

                print(
                    "Search provider:",
                    name
                )

                return results

        except Exception as e:

            print(
                name,
                "search error:",
                repr(e)
            )

    return []


# ============================================================
# READ WEB PAGE
# ============================================================

def read_web_page(url):

    try:

        response = requests.get(
            url,
            headers=SEARCH_HEADERS,
            timeout=10
        )

        if response.status_code != 200:
            return ""

        content_type = (
            response.headers
            .get(
                "Content-Type",
                ""
            )
            .lower()
        )

        if (
            "text/html"
            not in content_type
        ):
            return ""

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        for element in soup(
            [
                "script",
                "style",
                "noscript",
                "svg",
                "nav",
                "footer"
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

        return text[:5000]

    except Exception as e:

        print(
            "Page read error:",
            e
        )

        return ""


# ============================================================
# SEARCH COMMAND
# ============================================================

if bot:

    @bot.message_handler(
        commands=["search"]
    )
    def handle_search(message):

        query = extract_command_argument(
            message
        )

        if not query:

            bot.reply_to(
                message,
                "Напиши запрос после /search.\n\n"
                "Например:\n"
                "/search новости космоса"
            )

            return

        if not groq_client:

            bot.reply_to(
                message,
                "Поиск недоступен, потому что "
                "GROQ_KEY не задан."
            )

            return

        bot.send_chat_action(
            message.chat.id,
            "typing"
        )

        try:

            results = perform_search(
                query
            )

            if not results:

                bot.reply_to(
                    message,
                    "Поиск временно недоступен."
                )

                return

            collected = []

            for result in results:

                title = result.get(
                    "title",
                    ""
                )

                url = result.get(
                    "url",
                    ""
                )

                snippet = result.get(
                    "snippet",
                    ""
                )

                page_text = ""

                if url:

                    page_text = read_web_page(
                        url
                    )

                if page_text:

                    content = page_text[:3500]

                else:

                    content = snippet[:1500]

                collected.append(
                    "ЗАГОЛОВОК: "
                    + title
                    + "\nURL: "
                    + url
                    + "\nИНФОРМАЦИЯ:\n"
                    + content
                )

            source_text = (
                "\n\n--------------------\n\n"
                .join(
                    collected
                )
            )

            prompt = (
                "Пользователь задал запрос:\n"
                + query
                + "\n\n"
                "Результаты поиска:\n"
                + source_text
                + "\n\n"
                "Ответь пользователю на его языке. "
                "Используй найденную информацию. "
                "Не придумывай факты, которых нет "
                "в результатах. "
                "Если источники противоречат друг "
                "другу, скажи об этом. "
                "В конце укажи использованные сайты."
            )

            response = (
                groq_client
                .chat
                .completions
                .create(
                    model=FIXED_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content":
                                SYSTEM_INSTRUCTION
                        },
                        {
                            "role": "user",
                            "content": prompt
                        }
                    ],
                    temperature=0.3,
                    max_tokens=4000
                )
            )

            answer = (
                response
                .choices[0]
                .message
                .content
            )

            answer = clean_text(
                answer
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
# PHOTO / GEMINI VISION
# ============================================================

def analyze_image_with_gemini(
    image_bytes,
    mime_type,
    prompt
):

    if not gemini_client:

        raise RuntimeError(
            "Gemini client не инициализирован."
        )

    from google.genai import types

    image_part = types.Part.from_bytes(
        data=image_bytes,
        mime_type=mime_type
    )

    response = (
        gemini_client
        .models
        .generate_content(
            model=GEMINI_MODEL,
            contents=[
                prompt,
                image_part
            ]
        )
    )

    answer = getattr(
        response,
        "text",
        None
    )

    if not answer:

        raise RuntimeError(
            "Gemini не вернул ответ "
            "при анализе изображения."
        )

    return clean_text(
        answer
    )


if bot:

    @bot.message_handler(
        content_types=["photo"]
    )
    def handle_photo(message):

        if not gemini_client:

            bot.reply_to(
                message,
                "Анализ изображений сейчас "
                "недоступен.\n\n"
                "GEMINI_API_KEY не был "
                "инициализирован."
            )

            return

        bot.send_chat_action(
            message.chat.id,
            "typing"
        )

        try:

            file_info = bot.get_file(
                message
                .photo[-1]
                .file_id
            )

            downloaded = bot.download_file(
                file_info.file_path
            )

            caption = (
                message.caption
                or
                "Опиши подробно это изображение."
            )

            prompt = (
                SYSTEM_INSTRUCTION
                + "\n\n"
                "Пользователь отправил изображение.\n"
                "Запрос пользователя:\n"
                + caption
            )

            answer = analyze_image_with_gemini(
                downloaded,
                "image/jpeg",
                prompt
            )

            answer = add_rare_emoji(
                answer
            )

            safe_reply(
                message,
                answer
            )

        except Exception as e:

            print(
                "Vision error:",
                repr(e)
            )

            bot.reply_to(
                message,
                "Не удалось проанализировать "
                "изображение.\n\n"
                "Подробная ошибка записана "
                "в логах Render."
            )


# ============================================================
# SPECIAL COMMANDS
# ============================================================

if bot:

    @bot.message_handler(
        commands=[
            "code",
            "sum",
            "tr",
            "fix"
        ]
    )
    def handle_special(message):

        if not groq_client:

            bot.reply_to(
                message,
                "GROQ_KEY не задан."
            )

            return

        command = (
            message.text
            .split()[0]
            .lower()
        )

        if "@" in command:

            command = command.split(
                "@"
            )[0]

        user_text = extract_command_argument(
            message
        )

        if not user_text:

            bot.reply_to(
                message,
                "Напиши текст после "
                + command
                + "."
            )

            return

        instructions = {

            "/code":
                "Помоги написать, исправить "
                "или объяснить код. Если пользователь "
                "прислал код, внимательно "
                "проанализируй его.",

            "/sum":
                "Сделай краткую и понятную "
                "выжимку предоставленного текста.",

            "/tr":
                "Переведи предоставленный текст. "
                "Если пользователь указал язык "
                "перевода, используй именно его.",

            "/fix":
                "Исправь ошибки в предоставленном "
                "тексте, сохранив первоначальный смысл."
        }

        instruction = instructions.get(
            command,
            ""
        )

        bot.send_chat_action(
            message.chat.id,
            "typing"
        )

        try:

            response = (
                groq_client
                .chat
                .completions
                .create(
                    model=FIXED_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content":
                                SYSTEM_INSTRUCTION
                                + "\n\nЗадача:\n"
                                + instruction
                        },
                        {
                            "role": "user",
                            "content": user_text
                        }
                    ],
                    temperature=0.4,
                    max_tokens=4096
                )
            )

            answer = (
                response
                .choices[0]
                .message
                .content
            )

            answer = clean_text(
                answer
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
# PDF
# ============================================================

def extract_pdf_text(file_path):

    try:

        reader = PdfReader(
            file_path
        )

        pages = []

        for page in reader.pages:

            try:

                text = page.extract_text()

                if text:
                    pages.append(text)

            except Exception:
                continue

        return "\n".join(
            pages
        )[:20000]

    except Exception as e:

        print(
            "PDF error:",
            e
        )

        return ""


# ============================================================
# DOCX
# ============================================================

def extract_docx_text(file_path):

    try:

        from docx import Document

        document = Document(
            file_path
        )

        paragraphs = []

        for paragraph in document.paragraphs:

            if paragraph.text:

                paragraphs.append(
                    paragraph.text
                )

        return "\n".join(
            paragraphs
        )[:20000]

    except Exception as e:

        print(
            "DOCX error:",
            e
        )

        return ""


# ============================================================
# BOT MENTION
# ============================================================

def is_bot_mentioned(message):

    if message.chat.type == "private":
        return True

    text = (
        message.text
        or
        message.caption
        or
        ""
    )

    if not text:
        return False

    if not bot:
        return False

    try:

        me = bot.get_me()

        username = (
            me.username
            or ""
        ).lower()

        if not username:
            return False

        return (
            "@"
            + username
        ) in text.lower()

    except Exception as e:

        print(
            "Mention check error:",
            e
        )

        return False


# ============================================================
# ОБЫЧНЫЙ ДИАЛОГ
# ============================================================

if bot:

    @bot.message_handler(
        func=lambda message: True,
        content_types=["text"]
    )
    def handle_text(message):

        if not groq_client:

            bot.reply_to(
                message,
                "ИИ временно недоступен: "
                "GROQ_KEY не задан."
            )

            return

        # В группах отвечаем только если:
        # 1. бот упомянут
        # 2. сообщение является reply боту

        if message.chat.type in (
            "group",
            "supergroup"
        ):

            text = (
                message.text
                or
                ""
            )

            mentioned = is_bot_mentioned(
                message
            )

            replied_to_bot = False

            try:

                if (
                    message.reply_to_message
                    and
                    message.reply_to_message.from_user
                ):

                    replied_to_bot = (
                        message
                        .reply_to_message
                        .from_user
                        .id
                        ==
                        bot.get_me().id
                    )

            except Exception as e:

                print(
                    "Reply check error:",
                    e
                )

            if (
                not mentioned
                and
                not replied_to_bot
            ):
                return

        text = (
            message.text
            or
            ""
        ).strip()

        if not text:
            return

        # Убираем @username бота
        try:

            username = (
                bot
                .get_me()
                .username
            )

            if username:

                text = re.sub(
                    r"@"
                    + re.escape(username),
                    "",
                    text,
                    flags=re.IGNORECASE
                ).strip()

        except Exception:
            pass

        if not text:
            return

        bot.send_chat_action(
            message.chat.id,
            "typing"
        )

        try:

            answer = ask_groq(
                message.chat.id,
                text,
                temperature=0.7
            )

            answer = add_rare_emoji(
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
                "Произошла ошибка "
                "при обращении к ИИ."
            )


# ============================================================
# TELEGRAM POLLING
# ============================================================

def polling_loop():

    if not bot:

        print(
            "ERROR: BOT_TOKEN не задан."
        )

        return

    consecutive_409 = 0

    while True:

        try:

            print(
                "Starting Telegram polling..."
            )

            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                skip_pending=True,
                allowed_updates=[
                    "message"
                ]
            )

            consecutive_409 = 0

            print(
                "Telegram polling stopped."
            )

            print(
                "Restarting in 5 seconds..."
            )

            time.sleep(5)

        except Exception as e:

            error_text = str(e)

            print(
                "Telegram polling error:",
                repr(e)
            )

            # ==================================================
            # TELEGRAM 409
            # ==================================================

            if (
                "409" in error_text
                and
                "Conflict" in error_text
            ):

                consecutive_409 += 1

                print(
                    "Telegram 409 Conflict."
                )

                print(
                    "Another instance may be "
                    "using the same BOT_TOKEN."
                )

                print(
                    "Attempt:",
                    consecutive_409,
                    "/ 3"
                )

                if consecutive_409 >= 3:

                    print(
                        "Three consecutive 409 errors."
                    )

                    print(
                        "Waiting 60 seconds before "
                        "trying again."
                    )

                    time.sleep(60)

                    consecutive_409 = 0

                else:

                    time.sleep(15)

                continue

            # ==================================================
            # ДРУГИЕ ОШИБКИ
            # ==================================================

            consecutive_409 = 0

            print(
                "Non-409 Telegram error."
            )

            print(
                "Restarting polling in 10 seconds..."
            )

            time.sleep(10)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    print(
        "======================================"
    )

    print(
        "AI Chat Bot starting..."
    )

    print(
        "History limit:",
        MAX_HISTORY_LENGTH
    )

    print(
        "Groq:",
        "OK" if groq_client else "NO"
    )

    print(
        "Gemini:",
        "OK" if gemini_client else "NO"
    )

    print(
        "Gemini model:",
        GEMINI_MODEL
    )

    print(
        "======================================"
    )

    if not bot:

        print(
            "FATAL ERROR: BOT_TOKEN is missing."
        )

        raise SystemExit(1)

    # ========================================================
    # УДАЛЯЕМ WEBHOOK
    # ========================================================

    try:

        bot.delete_webhook(
            drop_pending_updates=True
        )

        print(
            "Telegram webhook deleted."
        )

    except Exception as e:

        print(
            "Webhook delete error:",
            repr(e)
        )

    # ========================================================
    # COMMANDS
    # ========================================================

    setup_commands()

    # ========================================================
    # FLASK
    # ========================================================

    web_thread = threading.Thread(
        target=run_web,
        daemon=True
    )

    web_thread.start()

    print(
        "Flask web server started."
    )

    # ========================================================
    # TELEGRAM
    # ========================================================

    polling_loop()
