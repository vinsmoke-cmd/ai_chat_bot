import os
import re
import io
import asyncio
import threading
import tempfile
import requests
import telebot
import static_ffmpeg
import edge_tts

from flask import Flask
from pypdf import PdfReader
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from duckduckgo_search import DDGS
from groq import Groq

# ============================================================
# FFMPEG
# ============================================================

static_ffmpeg.add_paths()

# ============================================================
# ENV
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
AUDIUS_API_KEY = os.getenv("AUDIUS_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

# ============================================================
# CLIENTS
# ============================================================

bot = telebot.TeleBot(BOT_TOKEN)

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# ============================================================
# GEMINI
# ============================================================

genai = None
Image = None

if GEMINI_API_KEY:
    try:
        import google.generativeai as genai
        from PIL import Image

        genai.configure(api_key=GEMINI_API_KEY)
    except Exception as e:
        print("[GEMINI INIT ERROR]", repr(e))

# ============================================================
# MEMORY
# ============================================================

user_histories = {}
user_modes = {}

MAX_HISTORY = 12

# ============================================================
# MUSIC
# ============================================================

music_cache = {}

MUSIC_RESULTS_PER_PAGE = 10
MUSIC_MAX_RESULTS = 30

# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "Бот работает!"


@app.route("/health")
def health():
    return "OK"


def run_web():
    port = int(os.environ.get("PORT", 8080))

    app.run(
        host="0.0.0.0",
        port=port
    )


# ============================================================
# HELPERS
# ============================================================

def clean_markdown(text):
    if not text:
        return ""

    text = re.sub(r"[*_#`]", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    return text.strip()


def safe_text(text, limit=4000):
    text = clean_markdown(text)

    if len(text) <= limit:
        return text

    return text[:limit - 3] + "..."


# ============================================================
# AI
# ============================================================

def get_system_prompt(user_id):
    mode = user_modes.get(user_id, "normal")

    if mode == "neuroham":
        return (
            "Ты Нейрохам — умный, язвительный и саркастичный "
            "ИИ-ассистент. Отвечай остроумно и дерзко, "
            "можешь подшучивать над пользователем, но не "
            "переходи в угрозы или жестокость. "
            "Не используй Markdown. "
            "Отвечай на языке пользователя."
        )

    return (
        "Ты полезный, дружелюбный и умный ИИ-ассистент. "
        "Отвечай на языке пользователя. "
        "Пиши естественно, без лишней воды. "
        "Не используй Markdown."
    )


def ask_ai_with_history(user_id, prompt):
    if not groq_client:
        return "GROQ_API_KEY не задан."

    if user_id not in user_histories:
        user_histories[user_id] = [
            {
                "role": "system",
                "content": get_system_prompt(user_id)
            }
        ]

    history = user_histories[user_id]

    history.append(
        {
            "role": "user",
            "content": prompt
        }
    )

    if len(history) > MAX_HISTORY:
        user_histories[user_id] = (
            [history[0]]
            + history[-(MAX_HISTORY - 1):]
        )

    try:
        response = groq_client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=user_histories[user_id],
            temperature=0.7
        )

        answer = response.choices[0].message.content or ""
        answer = clean_markdown(answer)

        user_histories[user_id].append(
            {
                "role": "assistant",
                "content": answer
            }
        )

        return answer

    except Exception as e:
        print("[GROQ ERROR]", repr(e))

        if user_histories[user_id]:
            if user_histories[user_id][-1]["role"] == "user":
                user_histories[user_id].pop()

        return "ИИ временно недоступен. Попробуй ещё раз."


# ============================================================
# WEB SEARCH
# ============================================================

def perform_web_search(query):
    try:
        with DDGS() as ddgs:
            results = list(
                ddgs.text(
                    query,
                    max_results=5
                )
            )

        if not results:
            return "Ничего не найдено."

        output = []

        for result in results:
            title = result.get("title", "")
            body = result.get("body", "")
            href = result.get("href", "")

            output.append(
                f"{title}\n"
                f"{body[:500]}\n"
                f"{href}"
            )

        return "\n\n".join(output)

    except Exception as e:
        print("[SEARCH ERROR]", repr(e))
        return "Поиск временно недоступен."


# ============================================================
# IMAGE
# ============================================================

def generate_image(prompt):
    url = (
        "https://image.pollinations.ai/prompt/"
        + requests.utils.quote(prompt)
    )

    try:
        response = requests.get(
            url,
            timeout=60,
            headers={
                "User-Agent": "Mozilla/5.0"
            }
        )

        if response.status_code == 200:
            return response.content

    except Exception as e:
        print("[IMAGE ERROR]", repr(e))

    return None


# ============================================================
# GEMINI PHOTO
# ============================================================

def analyze_image_gemini(image_bytes):
    if not GEMINI_API_KEY or genai is None or Image is None:
        return "Для анализа изображений не задан GEMINI_API_KEY."

    try:
        model = genai.GenerativeModel(
            "gemini-2.5-flash"
        )

        image = Image.open(
            io.BytesIO(image_bytes)
        )

        response = model.generate_content(
            [
                "Проанализируй изображение. "
                "Опиши, что на нём изображено, "
                "на русском языке.",
                image
            ]
        )

        if response and response.text:
            return clean_markdown(response.text)

    except Exception as e:
        print("[GEMINI ERROR]", repr(e))

    return "Не удалось проанализировать изображение."


# ============================================================
# TTS
# ============================================================

async def generate_audio(text, output_file):
    communicate = edge_tts.Communicate(
        text,
        "ru-RU-SvetlanaNeural"
    )

    await communicate.save(output_file)


# ============================================================
# HELP
# ============================================================

@bot.message_handler(
    commands=["start", "help"]
)
def help_cmd(message):
    bot.reply_to(
        message,
        "Привет! Я ИИ-ассистент.\n\n"
        "Команды:\n\n"
        "/search <запрос> — поиск\n"
        "/weather <город> — погода\n"
        "/image <описание> — генерация изображения\n"
        "/music <название> — поиск музыки 🎵\n"
        "/gemini <запрос> — Gemini\n"
        "/fact — интересный факт\n"
        "/code <задача> — программирование\n"
        "/sum <текст> — выжимка\n"
        "/tr <текст> — перевод\n"
        "/fix <текст> — исправление текста\n"
        "/tts <текст> — озвучка\n"
        "/clear — очистить память\n"
        "/neuroham — режим Нейрохама"
    )


# ============================================================
# NEUROHAM
# ============================================================

@bot.message_handler(
    commands=["neuroham", "rude"]
)
def toggle_neuroham(message):
    user_id = message.chat.id

    if user_modes.get(user_id, "normal") == "normal":
        user_modes[user_id] = "neuroham"

        answer = (
            "Режим Нейрохам активирован. "
            "Ну всё, теперь я официально имею право "
            "закатывать глаза на твои вопросы 💀"
        )

    else:
        user_modes[user_id] = "normal"

        answer = (
            "Режим Нейрохам выключен. "
            "Возвращаюсь к цивилизованному общению."
        )

    user_histories.pop(user_id, None)

    bot.reply_to(
        message,
        answer
    )


# ============================================================
# CLEAR
# ============================================================

@bot.message_handler(
    commands=["clear"]
)
def clear_cmd(message):
    user_histories.pop(
        message.chat.id,
        None
    )

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
        parts[1].strip()
        if len(parts) > 1
        else ""
    )

    if topic:
        prompt = (
            f"Расскажи один действительно интересный "
            f"факт на тему: {topic}"
        )
    else:
        prompt = (
            "Расскажи один удивительный случайный факт. "
            "Факт должен быть реальным."
        )

    msg = bot.reply_to(
        message,
        "Ищу интересный факт..."
    )

    answer = ask_ai_with_history(
        message.chat.id,
        prompt
    )

    bot.edit_message_text(
        safe_text(answer),
        chat_id=message.chat.id,
        message_id=msg.message_id
    )


# ============================================================
# WEATHER
# ============================================================

CITY_ALIASES = {
    "моксква": "Москва",
    "москв": "Москва",
    "москво": "Москва",
    "мск": "Москва",

    "ташкентт": "Ташкент",
    "ташкен": "Ташкент",

    "самаркандд": "Самарканд",
    "самаркан": "Самарканд",

    "питeр": "Санкт-Петербург",
    "питер": "Санкт-Петербург",
    "спб": "Санкт-Петербург",

    "лондонн": "Лондон",
    "парижж": "Париж",
    "берлинн": "Берлин",
    "дубайй": "Дубай",
    "римм": "Рим"
}


def normalize_city(city):
    city = city.strip()
    key = city.lower()

    if key in CITY_ALIASES:
        return CITY_ALIASES[key]

    return city


def get_weather_code(code):
    codes = {
        0: "Ясно",
        1: "Преимущественно ясно",
        2: "Переменная облачность",
        3: "Пасмурно",
        45: "Туман",
        48: "Изморозь",
        51: "Лёгкая морось",
        53: "Морось",
        55: "Сильная морось",
        61: "Небольшой дождь",
        63: "Дождь",
        65: "Сильный дождь",
        71: "Небольшой снег",
        73: "Снег",
        75: "Сильный снег",
        80: "Ливень",
        81: "Сильный ливень",
        82: "Очень сильный ливень",
        95: "Гроза",
        96: "Гроза с градом",
        99: "Сильная гроза с градом"
    }

    return codes.get(
        int(code),
        "Неизвестно"
    )


def geocode_city(city):
    city = normalize_city(city)

    try:
        response = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={
                "name": city,
                "count": 5,
                "language": "ru",
                "format": "json"
            },
            timeout=10
        )

        data = response.json()

        results = data.get(
            "results",
            []
        )

        if results:
            return results[0]

    except Exception as e:
        print("[WEATHER GEO ERROR]", repr(e))

    return None


@bot.message_handler(
    commands=["weather"]
)
def weather_cmd(message):
    parts = message.text.split(
        maxsplit=1
    )

    city = (
        parts[1].strip()
        if len(parts) > 1
        else ""
    )

    if not city:
        bot.reply_to(
            message,
            "Пример: /weather Москва"
        )
        return

    city = normalize_city(city)

    try:
        place = geocode_city(city)

        if not place:
            bot.reply_to(
                message,
                "Город не найден. Попробуй написать название города ещё раз."
            )
            return

        latitude = place["latitude"]
        longitude = place["longitude"]

        weather_response = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": latitude,
                "longitude": longitude,
                "current": (
                    "temperature_2m,"
                    "relative_humidity_2m,"
                    "apparent_temperature,"
                    "wind_speed_10m,"
                    "weather_code"
                ),
                "timezone": "auto"
            },
            timeout=10
        )

        weather_response.raise_for_status()

        data = weather_response.json()
        current = data["current"]

        country = place.get(
            "country",
            ""
        )

        weather_name = get_weather_code(
            current["weather_code"]
        )

        answer = (
            f"🌤 {place['name']}"
            + (
                f", {country}"
                if country
                else ""
            )
            + "\n\n"
            f"☁️ Состояние: {weather_name}\n"
            f"🌡 Температура: "
            f"{current['temperature_2m']}°C\n"
            f"🤒 Ощущается как: "
            f"{current['apparent_temperature']}°C\n"
            f"💧 Влажность: "
            f"{current['relative_humidity_2m']}%\n"
            f"💨 Ветер: "
            f"{current['wind_speed_10m']} км/ч"
        )

        bot.reply_to(
            message,
            answer
        )

    except Exception as e:
        print("[WEATHER ERROR]", repr(e))

        bot.reply_to(
            message,
            "Не удалось получить погоду. Попробуй ещё раз."
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
        parts[1].strip()
        if len(parts) > 1
        else ""
    )

    if not query:
        bot.reply_to(
            message,
            "Пример: /search новости науки"
        )
        return

    msg = bot.reply_to(
        message,
        "🔎 Ищу..."
    )

    data = perform_web_search(query)

    answer = ask_ai_with_history(
        message.chat.id,
        "Ответь на запрос пользователя "
        "на основе результатов поиска.\n\n"
        f"Запрос: {query}\n\n"
        f"Результаты:\n{data[:6000]}"
    )

    bot.edit_message_text(
        safe_text(answer),
        chat_id=message.chat.id,
        message_id=msg.message_id
    )


# ============================================================
# MUSIC
# ============================================================

def format_duration(seconds):
    try:
        seconds = int(
            float(seconds or 0)
        )

        return (
            f"{seconds // 60}:"
            f"{seconds % 60:02d}"
        )

    except Exception:
        return "0:00"


def normalize_ftu(track):
    if not isinstance(track, dict):
        return None

    track_id = (
        track.get("id")
        or track.get("uuid")
    )

    if not track_id:
        return None

    artist = (
        track.get("artist")
        or track.get("artist_name")
        or ""
    )

    if isinstance(artist, dict):
        artist = artist.get("name", "")

    return {
        "source": "freetouse",
        "id": str(track_id),
        "title": str(
            track.get(
                "title",
                "Без названия"
            )
        ),
        "artist": str(artist),
        "duration": (
            track.get("duration")
            or track.get(
                "duration_seconds",
                0
            )
        ),
        "raw": track
    }


def search_free_to_use(query):
    results = []
    used = set()

    try:
        response = requests.get(
            "https://api.freetouse.com/v3/music/tracks/search",
            params={
                "query": query,
                "limit": MUSIC_MAX_RESULTS
            },
            timeout=15
        )

        print(
            "[FTU]",
            response.status_code
        )

        if response.status_code != 200:
            return []

        data = response.json()

        if isinstance(data, dict):
            entries = data.get(
                "data",
                []
            )
        else:
            entries = data

        for item in entries:
            track = normalize_ftu(item)

            if not track:
                continue

            if track["id"] in used:
                continue

            used.add(track["id"])
            results.append(track)

    except Exception as e:
        print(
            "[FTU SEARCH ERROR]",
            repr(e)
        )

    return results[:MUSIC_MAX_RESULTS]


# ============================================================
# AUDIUS
# ============================================================

def search_audius(query):
    if not AUDIUS_API_KEY:
        print("[AUDIUS] API key отсутствует")
        return []

    results = []

    try:
        response = requests.get(
            "https://discoveryprovider.audius.co/v1/tracks/search",
            params={
                "query": query,
                "limit": MUSIC_MAX_RESULTS,
                "app_name": "telegram_music_bot"
            },
            headers={
                "Authorization":
                    f"Bearer {AUDIUS_API_KEY}"
            },
            timeout=15
        )

        print(
            "[AUDIUS]",
            response.status_code
        )

        if response.status_code != 200:
            return []

        data = response.json()

        for item in data.get("data", []):
            if not item:
                continue

            track_id = item.get("id")

            if not track_id:
                continue

            user = item.get("user") or {}

            artist = (
                user.get("name")
                or user.get("handle")
                or ""
            )

            results.append(
                {
                    "source": "audius",
                    "id": str(track_id),
                    "title": item.get(
                        "title",
                        "Без названия"
                    ),
                    "artist": artist,
                    "duration": item.get(
                        "duration",
                        0
                    ),
                    "raw": item
                }
            )

    except Exception as e:
        print(
            "[AUDIUS SEARCH ERROR]",
            repr(e)
        )

    return results[:MUSIC_MAX_RESULTS]


def search_music(query):
    results = search_free_to_use(query)

    if results:
        return results, "freetouse"

    print(
        "[MUSIC] Free To Use ничего не нашёл."
    )

    results = search_audius(query)

    if results:
        return results, "audius"

    return [], None


# ============================================================
# MUSIC DOWNLOAD
# ============================================================

def safe_filename(name):
    name = re.sub(
        r'[\\/:*?"<>|]+',
        "_",
        name
    )

    return name.strip()[:100] or "track"


def download_free_to_use(track, temp_dir):
    raw = track.get(
        "raw",
        {}
    )

    possible_keys = [
        "download_url",
        "downloadUrl",
        "audio_url",
        "audioUrl",
        "file_url",
        "fileUrl",
        "url"
    ]

    url = None

    for key in possible_keys:
        value = raw.get(key)

        if (
            isinstance(value, str)
            and value.startswith("http")
        ):
            url = value
            break

    if not url:
        raise Exception(
            "Free To Use не предоставил ссылку на аудио."
        )

    path = os.path.join(
        temp_dir,
        safe_filename(
            track.get(
                "title",
                "track"
            )
        ) + ".mp3"
    )

    response = requests.get(
        url,
        stream=True,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0"
        }
    )

    response.raise_for_status()

    with open(path, "wb") as f:
        for chunk in response.iter_content(
            1024 * 1024
        ):
            if chunk:
                f.write(chunk)

    if (
        not os.path.isfile(path)
        or os.path.getsize(path) <= 0
    ):
        raise Exception(
            "Free To Use вернул пустой файл."
        )

    return path


def download_audius(track, temp_dir):
    track_id = track["id"]

    url = (
        "https://discoveryprovider.audius.co/v1/"
        f"tracks/{track_id}/stream"
    )

    response = requests.get(
        url,
        params={
            "app_name": "telegram_music_bot"
        },
        headers={
            "Authorization":
                f"Bearer {AUDIUS_API_KEY}"
        },
        stream=True,
        timeout=30
    )

    if response.status_code != 200:
        raise Exception(
            f"Audius вернул HTTP {response.status_code}"
        )

    path = os.path.join(
        temp_dir,
        safe_filename(
            track.get(
                "title",
                "track"
            )
        ) + ".mp3"
    )

    with open(path, "wb") as f:
        for chunk in response.iter_content(
            1024 * 1024
        ):
            if chunk:
                f.write(chunk)

    if (
        not os.path.isfile(path)
        or os.path.getsize(path) <= 0
    ):
        raise Exception(
            "Audius вернул пустой файл."
        )

    return path


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

    query = (
        parts[1].strip()
        if len(parts) > 1
        else ""
    )

    if not query:
        bot.reply_to(
            message,
            "🎵 Пример:\n/music chill lofi"
        )
        return

    loading = bot.reply_to(
        message,
        "🔎 Ищу музыку..."
    )

    try:
        results, source = search_music(query)

        if not results:
            bot.edit_message_text(
                "❌ Ничего не найдено.",
                chat_id=message.chat.id,
                message_id=loading.message_id
            )
            return

        music_cache[message.chat.id] = {
            "query": query,
            "results": results,
            "source": source,
            "page": 0
        }

        show_music_page(
            message.chat.id,
            loading.message_id,
            0
        )

    except Exception as e:
        print(
            "[MUSIC ERROR]",
            repr(e)
        )

        bot.edit_message_text(
            "❌ Ошибка поиска музыки.",
            chat_id=message.chat.id,
            message_id=loading.message_id
        )


# ============================================================
# MUSIC PAGE
# ============================================================

def show_music_page(
    user_id,
    message_id,
    page
):
    cache = music_cache.get(user_id)

    if not cache:
        return

    results = cache["results"]

    total_pages = (
        len(results)
        + MUSIC_RESULTS_PER_PAGE
        - 1
    ) // MUSIC_RESULTS_PER_PAGE

    if total_pages <= 0:
        return

    page = max(
        0,
        min(
            page,
            total_pages - 1
        )
    )

    cache["page"] = page

    start = (
        page
        * MUSIC_RESULTS_PER_PAGE
    )

    page_results = results[
        start:
        start + MUSIC_RESULTS_PER_PAGE
    ]

    source_name = (
        "Free To Use"
        if cache["source"] == "freetouse"
        else "Audius"
    )

    text = (
        "🎵 Результаты поиска\n\n"
        f"🔎 {cache['query']}\n"
        f"📡 Источник: {source_name}\n\n"
    )

    keyboard = InlineKeyboardMarkup()

    for i, track in enumerate(page_results):
        index = start + i

        title = track.get(
            "title",
            "Без названия"
        )

        artist = track.get(
            "artist",
            ""
        )

        duration = format_duration(
            track.get(
                "duration",
                0
            )
        )

        text += (
            f"{index + 1}. {title}\n"
            f"   👤 {artist}\n"
            f"   ⏱ {duration}\n\n"
        )

        keyboard.add(
            InlineKeyboardButton(
                f"⬇️ {index + 1}. Скачать",
                callback_data=(
                    f"music_download:{index}"
                )
            )
        )

    navigation = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "⬅️",
                callback_data=(
                    f"music_page:{page - 1}"
                )
            )
        )

    if page < total_pages - 1:
        navigation.append(
            InlineKeyboardButton(
                "➡️",
                callback_data=(
                    f"music_page:{page + 1}"
                )
            )
        )

    if navigation:
        keyboard.row(*navigation)

    text += (
        f"Страница {page + 1} "
        f"из {total_pages}"
    )

    try:
        bot.edit_message_text(
            text,
            chat_id=user_id,
            message_id=message_id,
            reply_markup=keyboard
        )

    except Exception as e:
        print(
            "[MUSIC PAGE ERROR]",
            repr(e)
        )


# ============================================================
# MUSIC CALLBACK
# ============================================================

@bot.callback_query_handler(
    func=lambda call:
        call.data.startswith("music_")
)
def music_callback(call):
    user_id = call.message.chat.id

    try:
        # ----------------------------------------------------
        # PAGE
        # ----------------------------------------------------

        if call.data.startswith("music_page:"):
            page = int(
                call.data.split(
                    ":",
                    1
                )[1]
            )

            bot.answer_callback_query(
                call.id
            )

            show_music_page(
                user_id,
                call.message.message_id,
                page
            )

            return

        # ----------------------------------------------------
        # DOWNLOAD
        # ----------------------------------------------------

        if not call.data.startswith(
            "music_download:"
        ):
            return

        index = int(
            call.data.split(
                ":",
                1
            )[1]
        )

        cache = music_cache.get(user_id)

        if not cache:
            bot.answer_callback_query(
                call.id,
                "Результаты устарели.",
                show_alert=True
            )
            return

        results = cache["results"]

        if index < 0 or index >= len(results):
            bot.answer_callback_query(
                call.id,
                "Трек недоступен.",
                show_alert=True
            )
            return

        track = results[index]

        bot.answer_callback_query(
            call.id,
            "⏳ Скачиваю..."
        )

        title = track.get(
            "title",
            "Трек"
        )

        artist = track.get(
            "artist",
            ""
        )

        processing = bot.send_message(
            user_id,
            f"⏳ Скачиваю:\n{title}"
        )

        try:
            with tempfile.TemporaryDirectory() as temp_dir:

                if track["source"] == "freetouse":
                    path = download_free_to_use(
                        track,
                        temp_dir
                    )

                elif track["source"] == "audius":
                    path = download_audius(
                        track,
                        temp_dir
                    )

                else:
                    raise Exception(
                        "Неизвестный источник."
                    )

                if os.path.getsize(path) > 50 * 1024 * 1024:
                    raise Exception(
                        "Файл больше 50 МБ."
                    )

                bot.edit_message_text(
                    "📤 Отправляю аудио...",
                    chat_id=user_id,
                    message_id=processing.message_id
                )

                with open(
                    path,
                    "rb"
                ) as audio:

                    bot.send_audio(
                        user_id,
                        audio,
                        caption=(
                            f"🎵 {title}"
                            + (
                                f"\n👤 {artist}"
                                if artist
                                else ""
                            )
                        ),
                        title=title,
                        performer=(
                            artist
                            if artist
                            else None
                        )
                    )

            try:
                bot.delete_message(
                    user_id,
                    processing.message_id
                )
            except Exception:
                pass

        except Exception as e:
            print(
                "[MUSIC DOWNLOAD ERROR]",
                repr(e)
            )

            bot.edit_message_text(
                f"❌ Не удалось скачать трек:\n{e}",
                chat_id=user_id,
                message_id=processing.message_id
            )

    except Exception as e:
        print(
            "[MUSIC CALLBACK ERROR]",
            repr(e)
        )

        try:
            bot.answer_callback_query(
                call.id,
                "Произошла ошибка.",
                show_alert=True
            )
        except Exception:
            pass


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
def ai_cmd(message):
    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) < 2:
        bot.reply_to(
            message,
            "Напиши текст после команды."
        )
        return

    cmd = parts[0].split("@")[0].lower()
    content = parts[1].strip()

    if cmd == "/gemini":
        if not GEMINI_API_KEY or genai is None:
            bot.reply_to(
                message,
                "GEMINI_API_KEY не задан."
            )
            return

        try:
            model = genai.GenerativeModel(
                "gemini-2.5-flash"
            )

            response = model.generate_content(
                content
            )

            answer = (
                response.text
                if response and response.text
                else "Gemini не дал ответа."
            )

            bot.reply_to(
                message,
                safe_text(answer)
            )

        except Exception as e:
            print(
                "[GEMINI COMMAND ERROR]",
                repr(e)
            )

            bot.reply_to(
                message,
                "Gemini временно недоступен."
            )

        return

    if cmd == "/code":
        prompt = (
            "Реши задачу программирования. "
            "Если нужен код — дай полностью рабочий код.\n\n"
            + content
        )

    elif cmd == "/sum":
        prompt = (
            "Сделай краткую и понятную выжимку текста:\n\n"
            + content
        )

    elif cmd == "/tr":
        prompt = (
            "Переведи следующий текст на английский. "
            "Сохрани смысл и естественность:\n\n"
            + content
        )

    else:
        prompt = (
            "Исправь орфографические, пунктуационные "
            "и грамматические ошибки. "
            "Верни исправленный текст:\n\n"
            + content
        )

    msg = bot.reply_to(
        message,
        "Обрабатываю..."
    )

    answer = ask_ai_with_history(
        message.chat.id,
        prompt
    )

    bot.edit_message_text(
        safe_text(answer),
        chat_id=message.chat.id,
        message_id=msg.message_id
    )


# ============================================================
# IMAGE COMMAND
# ============================================================

@bot.message_handler(
    commands=["image"]
)
def image_cmd(message):
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
            "Пример: /image кот в космосе"
        )
        return

    msg = bot.reply_to(
        message,
        "🎨 Генерирую..."
    )

    image = generate_image(prompt)

    if image:
        bot.send_photo(
            message.chat.id,
            image,
            caption=prompt[:1024]
        )

        try:
            bot.delete_message(
                message.chat.id,
                msg.message_id
            )
        except Exception:
            pass

    else:
        bot.edit_message_text(
            "Не удалось создать изображение.",
            chat_id=message.chat.id,
            message_id=msg.message_id
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

    text = (
        parts[1].strip()
        if len(parts) > 1
        else ""
    )

    if not text:
        bot.reply_to(
            message,
            "Пример: /tts Привет!"
        )
        return

    msg = bot.reply_to(
        message,
        "🔊 Создаю голос..."
    )

    fd, path = tempfile.mkstemp(
        suffix=".mp3"
    )

    os.close(fd)

    try:
        asyncio.run(
            generate_audio(
                text,
                path
            )
        )

        with open(
            path,
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
        print(
            "[TTS ERROR]",
            repr(e)
        )

        bot.edit_message_text(
            "Ошибка создания голоса.",
            chat_id=message.chat.id,
            message_id=msg.message_id
        )

    finally:
        if os.path.exists(path):
            os.remove(path)


# ============================================================
# KIRA EASTER EGG
# ============================================================

@bot.message_handler(
    content_types=["text"]
)
def handle_text(message):
    text = message.text or ""
    lower = text.lower()

    if (
        "кира" in lower
        and "на самом" in lower
    ):
        bot.reply_to(
            message,
            "Она самая любимая, самая лучшая, "
            "самая добрая, самая красивая, "
            "самая милая, самая нежная, "
            "самая заботливая, самая прекрасная, "
            "самая родная, самая дорогая, "
            "самая искренняя, самая душевная, "
            "самая очаровательная, самая замечательная, "
            "самая невероятная, самая особенная, "
            "самая чудесная, самая ласковая, "
            "самая понимающая, самая весёлая, "
            "самая позитивная, самая уютная, "
            "самая драгоценная, самая бесценная, "
            "самая неповторимая, самая удивительная "
            "и просто самая-самая ❤️"
        )
        return

    msg = bot.reply_to(
        message,
        "Думаю..."
    )

    answer = ask_ai_with_history(
        message.chat.id,
        text
    )

    bot.edit_message_text(
        safe_text(answer),
        chat_id=message.chat.id,
        message_id=msg.message_id
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

        image = bot.download_file(
            file_info.file_path
        )

        answer = analyze_image_gemini(
            image
        )

        bot.edit_message_text(
            safe_text(answer),
            chat_id=message.chat.id,
            message_id=msg.message_id
        )

    except Exception as e:
        print(
            "[PHOTO ERROR]",
            repr(e)
        )

        bot.edit_message_text(
            "Не удалось обработать изображение.",
            chat_id=message.chat.id,
            message_id=msg.message_id
        )


# ============================================================
# PDF
# ============================================================

@bot.message_handler(
    content_types=["document"]
)
def handle_document(message):
    mime = (
        message.document.mime_type
        or ""
    )

    filename = (
        message.document.file_name
        or ""
    ).lower()

    if (
        mime != "application/pdf"
        and not filename.endswith(".pdf")
    ):
        bot.reply_to(
            message,
            "Поддерживаются PDF-файлы."
        )
        return

    msg = bot.reply_to(
        message,
        "📄 Читаю PDF..."
    )

    path = None

    try:
        file_info = bot.get_file(
            message.document.file_id
        )

        data = bot.download_file(
            file_info.file_path
        )

        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=".pdf"
        ) as f:
            f.write(data)
            path = f.name

        reader = PdfReader(path)

        text = ""

        for page in reader.pages[:10]:
            text += (
                page.extract_text()
                or ""
            )

        if not text.strip():
            raise Exception(
                "В PDF не удалось найти текст."
            )

        answer = ask_ai_with_history(
            message.chat.id,
            "Сделай краткую и понятную выжимку "
            "следующего PDF:\n\n"
            + text[:10000]
        )

        bot.edit_message_text(
            safe_text(answer),
            chat_id=message.chat.id,
            message_id=msg.message_id
        )

    except Exception as e:
        print(
            "[PDF ERROR]",
            repr(e)
        )

        bot.edit_message_text(
            f"Ошибка PDF: {e}",
            chat_id=message.chat.id,
            message_id=msg.message_id
        )

    finally:
        if path and os.path.exists(path):
            os.remove(path)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    print("====================================")
    print("🤖 Бот запускается...")
    print(
        "BOT_TOKEN:",
        "OK" if BOT_TOKEN else "НЕТ"
    )
    print(
        "GROQ_API_KEY:",
        "OK" if GROQ_API_KEY else "НЕТ"
    )
    print(
        "GEMINI_API_KEY:",
        "OK" if GEMINI_API_KEY else "НЕТ"
    )
    print(
        "AUDIUS_API_KEY:",
        "OK" if AUDIUS_API_KEY else "НЕТ"
    )
    print("====================================")

    threading.Thread(
        target=run_web,
        daemon=True
    ).start()

    bot.infinity_polling(
        allowed_updates=[
            "message",
            "callback_query"
        ],
        skip_pending=True
    )
