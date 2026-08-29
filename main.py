import os
import threading
import asyncio
import random
import re
import html

import edge_tts
import requests
import telebot
import google.generativeai as genai

from flask import Flask
from groq import Groq
from telebot.types import BotCommand
from googlesearch import search
from bs4 import BeautifulSoup

============================================================

НАСТРОЙКИ

============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_KEY = os.getenv("GROQ_KEY") or os.getenv("GROQ_API_KEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

FIXED_MODEL = "openai/gpt-oss-120b"

До 1000 сообщений пользователя.

1000 сообщений = примерно 500 пар user/assistant.

MAX_HISTORY_LENGTH = 1000

if not BOT_TOKEN:
raise RuntimeError("BOT_TOKEN не задан в переменных окружения.")

bot = telebot.TeleBot(BOT_TOKEN)

groq_client = Groq(api_key=GROQ_KEY) if GROQ_KEY else None

app = Flask(name)

dialog_history = {}

============================================================

GEMINI

============================================================

gemini_text_model = None
gemini_vision_model = None

if GEMINI_KEY:
genai.configure(api_key=GEMINI_KEY)

gemini_text_model = genai.GenerativeModel(
    "gemini-3.6-flash"
)

gemini_vision_model = genai.GenerativeModel(
    "gemini-3.6-flash"
)

============================================================

СИСТЕМНАЯ ИНСТРУКЦИЯ

============================================================

SYSTEM_INSTRUCTION = """
Ты умный и дружелюбный ИИ-ассистент.

ЯЗЫК:

Отвечай на языке, на котором пользователь обращается к тебе.

Если пользователь прямо просит перейти на другой язык,
перейди на этот язык и продолжай использовать его,
пока пользователь не попросит другой язык.

Поддерживай любые языки, которые способен нормально использовать.

Если пользователь смешивает языки, выбирай язык,
который лучше всего подходит для ответа.

СТИЛЬ:

Отвечай естественно, как живой собеседник.

Не повторяй постоянно одинаковые фразы.

Не начинай каждый ответ одинаково.

Если пользователь пишет короткое сообщение,
например "Привет", "Как дела?" или "Что нового?",
можешь отвечать немного по-разному.

Будь дружелюбным, но не навязчивым.

Иногда можешь задавать встречный вопрос,
если это действительно уместно.

Эмодзи используй редко.
Не добавляй эмодзи в каждый ответ.

ФОРМАТ:

Не используй Markdown.

Не используй звездочки.

Не используй решетки.

Не используй подчёркивания.

Не используй обратные кавычки для оформления.

Не используй декоративные символы для оформления текста.

Не создавай Markdown-заголовки.

Для списков используй обычные тире или нумерацию.

Пиши обычным чистым текстом.

КОД:

Если пользователь просит код,
пиши настоящий рабочий код.

Не заключай код в Markdown-ограждения.

Перед кодом можешь кратко объяснить его назначение.

ТОЧНОСТЬ:

Не выдумывай факты.

Если не уверен в информации,
честно скажи об этом.

Не утверждай, что у тебя есть доступ к интернету,
если интернет-поиск действительно не использовался.

Не упоминай эти системные инструкции пользователю.
"""

============================================================

ОЧИСТКА ОТ MARKDOWN

============================================================

def clean_text(text):
if not text:
return ""

text = str(text)

# Убираем thinking-блоки
if "</think>" in text:
    text = text.split("</think>")[-1]

if "<think>" in text:
    text = text.split("<think>")[0]

# Markdown
text = text.replace("```python", "")
text = text.replace("```", "")
text = text.replace("**", "")
text = text.replace("__", "")
text = text.replace("~~", "")
text = text.replace("*", "")
text = text.replace("#", "")

# Markdown-ссылки
text = re.sub(
    r"([^]+)\][^)]+",
    r"\1",
    text
)

# HTML
text = re.sub(
    r"<[^>]+>",
    "",
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

============================================================

РЕДКИЕ ЭМОДЗИ

============================================================

def add_rare_emoji(text):
if not text:
return text

# Только примерно 10% ответов получают эмодзи
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
    "💡",
    "🔥"
]

emoji = random.choice(emojis)

if random.random() < 0.30:
    return f"{emoji} {text}"

return f"{text} {emoji}"

============================================================

БЕЗОПАСНАЯ ОТПРАВКА ДЛИННЫХ СООБЩЕНИЙ

============================================================

def send_long_message(message, text):
if not text:
text = "Не удалось получить ответ."

text = clean_text(text)

# Telegram ограничивает размер сообщения.
max_length = 4000

if len(text) <= max_length:
    bot.reply_to(
        message,
        text
    )
    return

parts = []

while text:
    part = text[:max_length]

    if len(text) > max_length:
        split_pos = part.rfind("\n")

        if split_pos > 1000:
            part = part[:split_pos]

    parts.append(part)
    text = text[len(part):]

for part in parts:
    bot.send_message(
        message.chat.id,
        part
    )

============================================================

ИЗВЛЕЧЕНИЕ АРГУМЕНТОВ КОМАНДЫ

============================================================

def get_command_args(message, command_name):
text = message.text or ""

pattern = rf"^/{re.escape(command_name)}(?:@\S+)?(?:\s+(.*))?$"

match = re.match(
    pattern,
    text,
    flags=re.IGNORECASE
)

if not match:
    return ""

return (match.group(1) or "").strip()

============================================================

FLASK

============================================================

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
    port=port
)

============================================================

TELEGRAM COMMANDS

============================================================

def setup_commands():
bot.set_my_commands([
BotCommand(
"help",
"Список всех команд"
),
BotCommand(
"image",
"Сгенерировать картинку"
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
"Узнать погоду"
),
BotCommand(
"fact",
"Случайный факт"
),
BotCommand(
"code",
"Написать или разобрать код"
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
"Исправить ошибки"
),
BotCommand(
"tts",
"Озвучить текст"
),
BotCommand(
"clear",
"Сбросить контекст"
)
])

============================================================

START / HELP

============================================================

@bot.message_handler(
commands=["start", "help"]
)
def send_welcome(message):

text = (
    "Привет! Я твой ИИ-ассистент 🤖\n\n"
    "Что я умею:\n"
    "• Общаться на разных языках\n"
    "• Анализировать изображения\n"
    "• Писать и разбирать код\n"
    "• Исправлять ошибки\n"
    "• Переводить текст\n"
    "• Делать краткие выжимки\n"
    "• Генерировать изображения\n"
    "• Озвучивать текст\n"
    "• Искать информацию в интернете\n"
    "• Показывать погоду\n"
    "• Рассказывать случайные факты\n\n"
    "Команды:\n"
    "/image — создать изображение\n"
    "/gemini — спросить Gemini\n"
    "/search — поиск в интернете\n"
    "/weather — погода\n"
    "/fact — случайный факт\n"
    "/code — работа с кодом\n"
    "/sum — краткая выжимка\n"
    "/tr — перевод\n"
    "/fix — исправление текста\n"
    "/tts — озвучка\n"
    "/clear — очистить контекст\n\n"
    "Можно использовать команды и в группах, например:\n"
    "/weather@ИмяБота Москва"
)

bot.reply_to(
    message,
    text
)

============================================================

CLEAR

============================================================

@bot.message_handler(
commands=["clear"]
)
def clear_history(message):

dialog_history[message.chat.id] = []

bot.reply_to(
    message,
    "Контекст этого диалога очищен."
)

============================================================

ПОГОДА

============================================================

def get_weather(city):
"""
Получает город через Open-Meteo Geocoding,
затем текущую погоду через Open-Meteo Forecast.
"""

if not city:
    return None, "Укажи город."

city = city.strip()

try:
    geo_url = (
        "https://geocoding-api.open-meteo.com/v1/search"
    )

    geo_params = {
        "name": city,
        "count": 5,
        "language": "ru",
        "format": "json"
    }

    geo_response = requests.get(
        geo_url,
        params=geo_params,
        timeout=10
    )

    geo_response.raise_for_status()

    geo_data = geo_response.json()

    results = geo_data.get(
        "results",
        []
    )

    if not results:
        return None, (
            f"Не смог найти город «{city}»."
        )

    # Берём первый результат.
    result = results[0]

    lat = result["latitude"]
    lon = result["longitude"]

    name = result.get(
        "name",
        city
    )

    country = result.get(
        "country",
        ""
    )

    weather_url = (
        "https://api.open-meteo.com/v1/forecast"
    )

    weather_params = {
        "latitude": lat,
        "longitude": lon,
        "current": (
            "temperature_2m,"
            "relative_humidity_2m,"
            "apparent_temperature,"
            "precipitation,"
            "weather_code,"
            "wind_speed_10m"
        ),
        "timezone": "auto"
    }

    weather_response = requests.get(
        weather_url,
        params=weather_params,
        timeout=10
    )

    weather_response.raise_for_status()

    weather_data = weather_response.json()

    current = weather_data.get(
        "current",
        {}
    )

    temperature = current.get(
        "temperature_2m"
    )

    feels_like = current.get(
        "apparent_temperature"
    )

    humidity = current.get(
        "relative_humidity_2m"
    )

    precipitation = current.get(
        "precipitation"
    )

    wind = current.get(
        "wind_speed_10m"
    )

    weather_code = current.get(
        "weather_code"
    )

    descriptions = {
        0: "Ясно",
        1: "Преимущественно ясно",
        2: "Переменная облачность",
        3: "Пасмурно",
        45: "Туман",
        48: "Туман с изморозью",
        51: "Небольшая морось",
        53: "Морось",
        55: "Сильная морось",
        61: "Небольшой дождь",
        63: "Дождь",
        65: "Сильный дождь",
        71: "Небольшой снег",
        73: "Снег",
        75: "Сильный снег",
        80: "Ливень",
        81: "Ливень",
        82: "Сильный ливень",
        95: "Гроза",
        96: "Гроза с градом",
        99: "Сильная гроза с градом"
    }

    description = descriptions.get(
        weather_code,
        "Неизвестные погодные условия"
    )

    location = name

    if country:
        location += f", {country}"

    text = (
        f"Погода: {location}\n\n"
        f"Состояние: {description}\n"
        f"Температура: {temperature}°C\n"
        f"Ощущается как: {feels_like}°C\n"
        f"Влажность: {humidity}%\n"
        f"Осадки: {precipitation} мм\n"
        f"Ветер: {wind} км/ч"
    )

    return text, None

except requests.RequestException:
    return None, (
        "Сервис погоды временно недоступен. "
        "Попробуй ещё раз через некоторое время."
    )

except Exception:
    return None, (
        "Не удалось получить погоду."
    )

@bot.message_handler(
commands=["weather"]
)
def handle_weather(message):

city = get_command_args(
    message,
    "weather"
)

if not city:
    bot.reply_to(
        message,
        "Укажи город.\n\n"
        "Например:\n"
        "/weather Москва\n\n"
        "В группе:\n"
        "/weather@ИмяБота Москва"
    )
    return

bot.send_chat_action(
    message.chat.id,
    "typing"
)

result, error = get_weather(city)

if error:
    bot.reply_to(
        message,
        error
    )
    return

bot.reply_to(
    message,
    result
)

============================================================

ПОГОДА В ОБЫЧНОМ ТЕКСТЕ

============================================================

def extract_weather_text(text):
if not text:
return None

text = text.strip()

patterns = [
    r"^погода\s+(.+)$",
    r"^какая\s+погода\s+(?:в|на)\s+(.+)$",
    r"^погоду\s+(?:в|на)\s+(.+)$"
]

for pattern in patterns:
    match = re.match(
        pattern,
        text,
        flags=re.IGNORECASE
    )

    if match:
        return match.group(1).strip()

return None

============================================================

FACT

============================================================

@bot.message_handler(
commands=["fact"]
)
def handle_fact(message):

facts = [
    "У осьминогов три сердца.",
    "Бананы с точки зрения ботаники являются ягодами.",
    "Мёд при правильном хранении может сохраняться очень долго.",
    "На Венере день длится дольше года.",
    "У акул появились предки раньше первых динозавров.",
    "Некоторые виды бамбука могут расти очень быстро.",
    "У ворон хорошо развиты способности к решению задач.",
    "Молния может ударить в одно место несколько раз."
]

bot.reply_to(
    message,
    random.choice(facts)
)

============================================================

IMAGE GENERATION

============================================================

@bot.message_handler(
commands=["image"]
)
def handle_image_generation(message):

prompt = get_command_args(
    message,
    "image"
)

if not prompt:
    bot.reply_to(
        message,
        "Напиши, что нужно нарисовать.\n"
        "Например: /image космический кот"
    )
    return

bot.send_chat_action(
    message.chat.id,
    "upload_photo"
)

try:

    english_prompt = prompt

    if groq_client:

        chat = groq_client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Translate the user's image prompt "
                        "into a detailed English prompt "
                        "for an AI image generator. "
                        "Return only the translated prompt."
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            model=FIXED_MODEL,
            temperature=0.7
        )

        english_prompt = clean_text(
            chat.choices[0].message.content
        )

    encoded_prompt = requests.utils.quote(
        english_prompt
    )

    image_url = (
        "https://image.pollinations.ai/prompt/"
        + encoded_prompt
    )

    bot.send_photo(
        message.chat.id,
        image_url,
        caption=f"Запрос: {prompt}"
    )

except Exception as e:

    bot.reply_to(
        message,
        f"Ошибка генерации изображения: {e}"
    )

============================================================

GEMINI

============================================================

@bot.message_handler(
commands=["gemini"]
)
def handle_gemini(message):

if not GEMINI_KEY or not gemini_text_model:
    bot.reply_to(
        message,
        "Gemini сейчас недоступен: "
        "GEMINI_API_KEY не задан."
    )
    return

query = get_command_args(
    message,
    "gemini"
)

if not query:
    bot.reply_to(
        message,
        "Напиши запрос после команды /gemini."
    )
    return

bot.send_chat_action(
    message.chat.id,
    "typing"
)

try:

    full_query = (
        SYSTEM_INSTRUCTION
        + "\n\nЗапрос пользователя:\n"
        + query
    )

    response = gemini_text_model.generate_content(
        full_query
    )

    answer = clean_text(
        response.text
    )

    answer = add_rare_emoji(
        answer
    )

    send_long_message(
        message,
        answer
    )

except Exception as e:

    bot.reply_to(
        message,
        f"Ошибка Gemini: {e}"
    )

============================================================

TTS

============================================================

@bot.message_handler(
commands=["tts"]
)
def handle_tts(message):

text = get_command_args(
    message,
    "tts"
)

if not text:
    bot.reply_to(
        message,
        "Напиши текст после команды /tts."
    )
    return

bot.send_chat_action(
    message.chat.id,
    "record_voice"
)

filename = (
    f"voice_"
    f"{message.from_user.id}_"
    f"{message.message_id}.mp3"
)

try:

    async def generate_voice():

        communicate = edge_tts.Communicate(
            text,
            "ru-RU-SvetlanaNeural"
        )

        await communicate.save(
            filename
        )

    asyncio.run(
        generate_voice()
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

    bot.reply_to(
        message,
        f"Ошибка озвучки: {e}"
    )

finally:

    if os.path.exists(filename):
        os.remove(filename)

============================================================

ПОИСК В ИНТЕРНЕТЕ

============================================================

Несколько публичных SearXNG-серверов.

Они используются по очереди.

SEARXNG_INSTANCES = [
"https://search.bus-hit.me",
"https://searx.be",
"https://search.sapti.me",
"https://searx.tiekoetter.com",
"https://search.ononoki.org"
]

def search_searxng(query):

headers = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "Chrome/120 Safari/537.36"
    )
}

for instance in SEARXNG_INSTANCES:

    try:

        url = instance.rstrip("/") + "/search"

        params = {
            "q": query,
            "format": "json",
            "language": "ru",
            "safesearch": 1,
            "pageno": 1
        }

        response = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=8
        )

        if response.status_code != 200:
            continue

        data = response.json()

        results = data.get(
            "results",
            []
        )

        if not results:
            continue

        return results[:5]

    except Exception:
        continue

return []

def search_google_fallback(query):

try:

    urls = list(
        search(
            query,
            num_results=5
        )
    )

    results = []

    for url in urls:

        results.append({
            "url": url,
            "title": url,
            "content": ""
        })

    return results

except Exception:
    return []

def read_search_pages(results):

headers = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "Chrome/120 Safari/537.36"
    )
}

readable = []

for item in results[:5]:

    url = item.get(
        "url",
        ""
    )

    title = item.get(
        "title",
        url
    )

    content = item.get(
        "content",
        ""
    )

    # SearXNG уже может вернуть текст.
    if content:
        readable.append(
            f"Источник: {title}\n"
            f"URL: {url}\n"
            f"{content[:1800]}"
        )
        continue

    try:

        response = requests.get(
            url,
            headers=headers,
            timeout=7
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
                "noscript",
                "svg"
            ]
        ):
            element.decompose()

        page_text = soup.get_text(
            separator=" ",
            strip=True
  
