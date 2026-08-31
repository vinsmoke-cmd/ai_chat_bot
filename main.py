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
from bs4 import BeautifulSoup
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

groq_client = (
    Groq(api_key=GROQ_API_KEY)
    if GROQ_API_KEY
    else None
)

# ============================================================
# GEMINI
# ============================================================

if GEMINI_API_KEY:
    import google.generativeai as genai
    from PIL import Image

    genai.configure(
        api_key=GEMINI_API_KEY
    )

# ============================================================
# MEMORY
# ============================================================

user_histories = {}
user_modes = {}

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
# CLEAN
# ============================================================

def clean_markdown(text):

    if not text:
        return ""

    return re.sub(
        r'[*_#]',
        '',
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

    if user_id not in user_histories:

        if mode == "neuroham":

            sys_prompt = (
                "Ты — Нейрохам, гениальный, "
                "ворчливый и саркастичный ИИ. "
                "Отвечай едко и с иронией, "
                "но без нецензурной лексики. "
                "Не используй Markdown."
            )

        else:

            sys_prompt = (
                "Ты полезный, дружелюбный "
                "и веселый ИИ-ассистент. "
                "Отвечай на языке пользователя. "
                "Не используй Markdown."
            )

        user_histories[user_id] = [
            {
                "role": "system",
                "content": sys_prompt
            }
        ]

    user_histories[user_id].append(
        {
            "role": "user",
            "content": prompt
        }
    )

    if len(user_histories[user_id]) > 11:

        user_histories[user_id] = (
            [user_histories[user_id][0]]
            + user_histories[user_id][-10:]
        )

    messages = [
        x.copy()
        for x in user_histories[user_id]
    ]

    if mode == "neuroham":

        messages[-1]["content"] = (
            "Ответь в стиле максимально "
            "саркастичного Нейрохама, "
            "но без мата.\n\n"
            + prompt
        )

    if groq_client:

        try:

            response = groq_client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=messages
            )

            answer = clean_markdown(
                response.choices[0].message.content
            )

            user_histories[user_id].append(
                {
                    "role": "assistant",
                    "content": answer
                }
            )

            return answer

        except Exception as e:

            print(
                "[GROQ ERROR]",
                repr(e)
            )

    user_histories[user_id].pop()

    return (
        "Все провайдеры ИИ сейчас недоступны. "
        "Попробуй ещё раз через минуту."
    )


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

        text = ""

        for result in results:

            text += (
                f"- {result.get('title', '')}: "
                f"{result.get('body', '')[:400]}\n"
            )

        return text

    except Exception as e:

        return f"Ошибка поиска: {e}"


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
            timeout=60
        )

        if response.status_code == 200:
            return response.content

    except Exception as e:

        print(
            "[IMAGE ERROR]",
            repr(e)
        )

    return None


# ============================================================
# GEMINI PHOTO
# ============================================================

def analyze_image_gemini(
    image_bytes
):

    if not GEMINI_API_KEY:

        return (
            "Для анализа изображений "
            "не задан GEMINI_API_KEY."
        )

    try:

        model = genai.GenerativeModel(
            "gemini-2.5-flash"
        )

        image = Image.open(
            io.BytesIO(image_bytes)
        )

        response = model.generate_content(
            [
                "Подробно опиши изображение "
                "на русском языке.",
                image
            ]
        )

        if response and response.text:

            return clean_markdown(
                response.text
            )

    except Exception as e:

        print(
            "[GEMINI ERROR]",
            repr(e)
        )

    return (
        "Не удалось проанализировать изображение."
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
# HELP
# ============================================================

@bot.message_handler(
    commands=[
        "start",
        "help"
    ]
)
def help_cmd(message):

    text = (
        "Привет! Я ИИ-ассистент.\n\n"
        "Команды:\n\n"
        "/search <запрос> — поиск\n"
        "/weather <город> — погода\n"
        "/image <описание> — картинка\n"
        "/music <название> — музыка 🎵\n"
        "/gemini <запрос> — Gemini\n"
        "/fact — интересный факт\n"
        "/code <задача> — программирование\n"
        "/sum <текст> — выжимка\n"
        "/tr <текст> — перевод\n"
        "/fix <текст> — исправление\n"
        "/tts <текст> — озвучка\n"
        "/clear — очистить память\n"
        "/neuroham — режим Нейрохама"
    )

    bot.reply_to(
        message,
        text
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
def toggle_neuroham(message):

    user_id = message.chat.id

    if user_modes.get(
        user_id,
        "normal"
    ) == "normal":

        user_modes[user_id] = "neuroham"

        answer = (
            "Режим Нейрохам активирован. "
            "Готовься к критике 💀"
        )

    else:

        user_modes[user_id] = "normal"

        answer = (
            "Режим Нейрохам выключен. "
            "Возвращаюсь к нормальному общению."
        )

    user_histories.pop(
        user_id,
        None
    )

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
        parts[1]
        if len(parts) > 1
        else ""
    )

    prompt = (
        f"Расскажи один интересный факт "
        f"на тему {topic}."
        if topic
        else
        "Расскажи один очень интересный "
        "случайный факт."
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
        answer,
        chat_id=message.chat.id,
        message_id=msg.message_id
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

    try:

        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={
                "name": city,
                "count": 1,
                "language": "ru",
                "format": "json"
            },
            timeout=10
        ).json()

        if not geo.get("results"):

            bot.reply_to(
                message,
                "Город не найден."
            )

            return

        place = geo["results"][0]

        latitude = place["latitude"]
        longitude = place["longitude"]

        weather = requests.get(
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
        ).json()

        current = weather["current"]

        answer = (
            f"🌤 {place['name']}\n\n"
            f"🌡 Температура: "
            f"{current['temperature_2m']}°C\n"
            f"🤒 Ощущается: "
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

    data = perform_web_search(
        query
    )

    answer = ask_ai_with_history(
        message.chat.id,
        f"Ответь на запрос пользователя "
        f"на основе результатов поиска:\n\n"
        f"Запрос: {query}\n\n"
        f"{data[:5000]}"
    )

    bot.edit_message_text(
        answer,
        chat_id=message.chat.id,
        message_id=msg.message_id
    )


# ============================================================
# MUSIC HELPERS
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

    if not isinstance(
        track,
        dict
    ):
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

    if isinstance(
        artist,
        dict
    ):

        artist = (
            artist.get("name")
            or ""
        )

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


def search_free_to_use(
    query
):

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

        entries = (
            data.get("data", [])
            if isinstance(
                data,
                dict
            )
            else data
        )

        for item in entries:

            track = normalize_ftu(
                item
            )

            if not track:
                continue

            if track["id"] in used:
                continue

            used.add(
                track["id"]
            )

            results.append(
                track
            )

    except Exception as e:

        print(
            "[FTU SEARCH ERROR]",
            repr(e)
        )

    return results[:MUSIC_MAX_RESULTS]


# ============================================================
# AUDIUS SEARCH
# ============================================================

def search_audius(
    query
):

    if not AUDIUS_API_KEY:

        print(
            "[AUDIUS] API key отсутствует"
        )

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

        entries = data.get(
            "data",
            []
        )

        for item in entries:

            if not item:
                continue

            track_id = item.get(
                "id"
            )

            if not track_id:
                continue

            user = item.get(
                "user"
            ) or {}

            artist = (
                user.get(
                    "name"
                )
                or user.get(
                    "handle"
                )
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


# ============================================================
# AUDIUS DOWNLOAD
# ============================================================

def download_audius(
    track,
    temp_dir
):

    track_id = track["id"]

    urls = [
        (
            "https://discoveryprovider.audius.co/v1/"
            f"tracks/{track_id}/stream"
        ),
        (
            "https://discoveryprovider.audius.co/v1/"
            f"tracks/{track_id}/stream?app_name=telegram_music_bot"
        )
    ]

    response = None

    for url in urls:

        try:

            response = requests.get(
                url,
                params={
                    "app_name":
                        "telegram_music_bot"
                },
                headers={
                    "Authorization":
                        f"Bearer {AUDIUS_API_KEY}"
                },
                stream=True,
                timeout=30
            )

            if response.status_code == 200:
                break

        except Exception as e:

            print(
                "[AUDIUS DOWNLOAD ERROR]",
                repr(e)
            )

    if not response or response.status_code != 200:

        raise Exception(
            "Audius не смог отдать аудио."
        )

    title = re.sub(
        r'[\\/:*?"<>|]+',
        "_",
        track.get(
            "title",
            "track"
        )
    )[:100]

    path = os.path.join(
        temp_dir,
        title + ".mp3"
    )

    with open(
        path,
        "wb"
    ) as f:

        for chunk in response.iter_content(
            1024 * 1024
        ):

            if chunk:
                f.write(chunk)

    if not os.path.isfile(path):

        raise Exception(
            "Файл Audius не сохранён."
        )

    if os.path.getsize(path) <= 0:

        raise Exception(
            "Audius вернул пустой файл."
        )

    return path


# ============================================================
# MUSIC SEARCH
# ============================================================

def search_music(
    query
):

    results = search_free_to_use(
        query
    )

    if results:

        return results, "freetouse"

    print(
        "[MUSIC] Free To Use ничего не нашёл."
    )

    results = search_audius(
        query
    )

    if results:

        return results, "audius"

    return [], None


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
            "🎵 Пример:\n"
            "/music chill lofi"
        )

        return

    loading = bot.reply_to(
        message,
        "🔎 Ищу музыку..."
    )

    try:

        results, source = search_music(
            query
        )

        if not results:

            bot.edit_message_text(
                "❌ Ничего не найдено.",
                chat_id=message.chat.id,
                message_id=loading.message_id
            )

            return

        music_cache[
            message.chat.id
        ] = {
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

        bot.edit_message_text(
            f"❌ Ошибка поиска:\n{e}",
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

    cache = music_cache.get(
        user_id
    )

    if not cache:
        return

    results = cache["results"]

    total_pages = (
        len(results)
        + MUSIC_RESULTS_PER_PAGE
        - 1
    ) // MUSIC_RESULTS_PER_PAGE

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

    for i, track in enumerate(
        page_results
    ):

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

        keyboard.row(
            *navigation
        )

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
        call.data.startswith(
            "music_"
        )
)
def music_callback(call):

    user_id = call.message.chat.id

    try:

        # ----------------------------------------------------
        # PAGE
        # ----------------------------------------------------

        if call.data.startswith(
            "music_page:"
        ):

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

        index = int(
            call.data.split(
                ":",
                1
            )[1]
        )

        cache = music_cache.get(
            user_id
        )

        if not cache:

            bot.answer_callback_query(
                call.id,
                "Результаты устарели.",
                show_alert=True
            )

            return

        results = cache["results"]

        if (
            index < 0
            or index >= len(results)
        ):

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

                size = os.path.getsize(
                    path
                )

                if size > 50 * 1024 * 1024:

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
# FREE TO USE DOWNLOAD
# ============================================================

def download_free_to_use(
    track,
    temp_dir
):

    raw = track.get(
        "raw",
        {}
    )

    possible = [
        "download_url",
        "downloadUrl",
        "audio_url",
        "audioUrl",
        "file_url",
        "fileUrl",
        "url"
    ]

    url = None

    for key in possible:

        value = raw.get(
            key
        )

        if (
            isinstance(value, str)
            and value.startswith("http")
        ):

            url = value
            break

    if not url:

        raise Exception(
            "Free To Use не предоставил "
            "ссылку на аудио."
        )

    title = re.sub(
        r'[\\/:*?"<>|]+',
        "_",
        track.get(
            "title",
            "track"
        )
    )[:100]

    path = os.path.join(
        temp_dir,
        title + ".mp3"
    )

    response = requests.get(
        url,
        stream=True,
        timeout=30,
        headers={
            "User-Agent":
                "Mozilla/5.0"
        }
    )

    response.raise_for_status()

    with open(
        path,
        "wb"
    ) as f:

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

    cmd = parts[0].split("@")[0]
    content = parts[1]

    if cmd == "/gemini":

        prompt = content

    elif cmd == "/code":

        prompt = (
            "Реши задачу программирования:\n"
            + content
        )

    elif cmd == "/sum":

        prompt = (
            "Сделай краткую выжимку:\n"
            + content
        )

    elif cmd == "/tr":

        prompt = (
            "Переведи на английский:\n"
            + content
        )

    else:

        prompt = (
            "Исправь ошибки в тексте:\n"
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
        answer,
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
        parts[1]
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

    image = generate_image(
        prompt
    )

    if image:

        bot.send_photo(
            message.chat.id,
            image,
            caption=prompt
        )

        bot.delete_message(
            message.chat.id,
            msg.message_id
        )

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
        parts[1]
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

    path = tempfile.mktemp(
        suffix=".mp3"
    )

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

        bot.delete_message(
            message.chat.id,
            msg.message_id
        )

    except Exception as e:

        bot.edit_message_text(
            f"Ошибка TTS: {e}",
            chat_id=message.chat.id,
            message_id=msg.message_id
        )

    finally:

        if os.path.exists(path):

            os.remove(path)


# ============================================================
# TEXT
# ============================================================

@bot.message_handler(
    content_types=["text"]
)
def handle_text(message):

    text = message.text
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
            "самая понимающая, самая веселая, "
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
        answer,
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


# ============================================================
# PDF
# ============================================================

@bot.message_handler(
    content_types=["document"]
)
def handle_document(message):

    if (
        message.document.mime_type
        != "application/pdf"
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

        reader = PdfReader(
            path
        )

        text = ""

        for page in reader.pages[:5]:

            text += (
                page.extract_text()
                or ""
            )

        answer = ask_ai_with_history(
            message.chat.id,
            "Сделай краткую выжимку PDF:\n\n"
            + text[:5000]
        )

        bot.edit_message_text(
            answer,
            chat_id=message.chat.id,
            message_id=msg.message_id
        )

    except Exception as e:

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

    print(
        "===================================="
    )

    print(
        "🤖 Бот запускается..."
    )

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

    print(
        "===================================="
    )

    threading.Thread(
        target=run_web,
        daemon=True
    ).start()

    bot.infinity_polling(
        allowed_updates=[
            "message",
            "callback_query"
        ]
    )
