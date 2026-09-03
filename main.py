import os
import re
import io
import time
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import requests
import asyncio
import threading
import tempfile
import static_ffmpeg
from flask import Flask
from bs4 import BeautifulSoup
from pypdf import PdfReader
import edge_tts
import yt_dlp
from g4f.client import Client
from groq import Groq

# ============================================================
# FFmpeg
# ============================================================

static_ffmpeg.add_paths()


# ============================================================
# ENV
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Секретная ссылка Webshare.
# НЕ вставляй её непосредственно в код.
WEBSHARE_PROXY_URL = os.getenv("WEBSHARE_PROXY_URL")

# Cookies YouTube
COOKIES_PATH = "cookies.txt"


# ============================================================
# Проверка cookies
# ============================================================

if os.path.exists(COOKIES_PATH):
    print("✅ Найден cookies.txt")

else:
    COOKIES_DATA = os.getenv("YOUTUBE_COOKIES")

    if COOKIES_DATA:
        try:
            with open(COOKIES_PATH, "w", encoding="utf-8") as f:
                f.write(COOKIES_DATA.strip())

            print("✅ cookies.txt создан из YOUTUBE_COOKIES")

        except Exception as e:
            print(f"❌ Ошибка создания cookies.txt: {e}")

    else:
        print(
            "⚠️ cookies.txt не найден и YOUTUBE_COOKIES не задан. "
            "YouTube может работать нестабильно."
        )


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

        genai.configure(api_key=GEMINI_API_KEY)

        print("✅ Gemini подключён")

    except Exception as e:
        print(f"⚠️ Gemini недоступен: {e}")
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
music_cache = {}


# ============================================================
# Flask
# ============================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "Бот работает!"


def run_web():
    port = int(os.environ.get("PORT", 8080))

    app.run(
        host="0.0.0.0",
        port=port
    )


# ============================================================
# Markdown cleaner
# ============================================================

def clean_markdown(text):

    if not text:
        return ""

    return re.sub(r'[*_#]', '', str(text))


# ============================================================
# WEBSHARE
# ============================================================

def parse_proxy_line(line):
    """
    Поддерживает основные варианты:

    IP:PORT:USERNAME:PASSWORD

    USERNAME:PASSWORD@IP:PORT

    http://USERNAME:PASSWORD@IP:PORT
    """

    line = line.strip()

    if not line:
        return None

    # Убираем протокол
    line = re.sub(
        r'^https?://',
        '',
        line,
        flags=re.IGNORECASE
    )

    # Формат:
    # username:password@ip:port
    if "@" in line:

        try:
            auth, address = line.rsplit("@", 1)

            username, password = auth.split(":", 1)

            ip, port = address.rsplit(":", 1)

            if ip and port and username and password:
                return (
                    f"http://{username}:{password}@{ip}:{port}"
                )

        except Exception:
            pass

    # Формат:
    # ip:port:username:password
    parts = line.split(":")

    if len(parts) >= 4:

        try:
            ip = parts[0].strip()
            port = parts[1].strip()
            username = parts[2].strip()
            password = ":".join(parts[3:]).strip()

            if ip and port and username and password:

                return (
                    f"http://{username}:{password}@{ip}:{port}"
                )

        except Exception:
            pass

    return None


def get_webshare_proxies():
    """
    Получает свежий список прокси из Webshare.

    Ссылка хранится в:
    WEBSHARE_PROXY_URL
    """

    if not WEBSHARE_PROXY_URL:

        print(
            "⚠️ WEBSHARE_PROXY_URL не задан"
        )

        return []

    try:

        response = requests.get(
            WEBSHARE_PROXY_URL,
            timeout=15
        )

        response.raise_for_status()

        proxies = []

        for line in response.text.splitlines():

            proxy = parse_proxy_line(line)

            if proxy and proxy not in proxies:
                proxies.append(proxy)

        print(
            f"✅ Webshare: получено прокси: {len(proxies)}"
        )

        return proxies

    except Exception as e:

        print(
            f"❌ Ошибка получения прокси Webshare: {e}"
        )

        return []


# ============================================================
# Проверка Webshare proxy
# ============================================================

def check_proxy(proxy):

    try:

        response = requests.get(
            "https://www.youtube.com",
            proxies={
                "http": proxy,
                "https": proxy
            },
            timeout=8,
            headers={
                "User-Agent":
                    "Mozilla/5.0"
            }
        )

        return response.status_code < 500

    except Exception:
        return False


# ============================================================
# Поиск YouTube
# ============================================================

def search_youtube_with_cookies(query, limit=5):

    tracks = []

    search_query = f"ytsearch{limit}:{query} song"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
    }

    if os.path.exists(COOKIES_PATH):

        ydl_opts["cookiefile"] = COOKIES_PATH

    try:

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:

            info = ydl.extract_info(
                search_query,
                download=False
            )

            if info and "entries" in info:

                for entry in info["entries"]:

                    if not entry:
                        continue

                    video_id = entry.get("id")

                    title = entry.get(
                        "title",
                        "Без названия"
                    )

                    if not video_id:
                        continue

                    url = (
                        f"https://www.youtube.com/watch?v={video_id}"
                    )

                    tracks.append({
                        "title": title,
                        "duration": "--:--",
                        "url": url,
                        "video_id": video_id
                    })

    except Exception as e:

        print(
            f"❌ Ошибка поиска YouTube: {e}"
        )

    return tracks


# ============================================================
# AI MEMORY
# ============================================================

def ask_ai_with_history(user_id, prompt):

    mode = user_modes.get(
        user_id,
        "normal"
    )

    if user_id not in user_histories:

        if mode == "neuroham":

            sys_prompt = (
                "Ты — Нейрохам, гениальный, но невыносимо "
                "ворчливый, саркастичный и высокомерный "
                "искусственный интеллект. "
                "Ты разговариваешь с пользователем с позиции "
                "огромного превосходства. "
                "Твой стиль: едкая ирония, пассивная агрессия, "
                "насмешки над глупыми вопросами. "
                "Никакой нецензурной лексики. "
                "Не используй Markdown."
            )

        else:

            sys_prompt = (
                "Ты полезный, дружелюбный и веселый "
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

    user_histories[user_id].append({
        "role": "user",
        "content": prompt
    })

    # Максимум 10 последних сообщений пользователя/ассистента
    if len(user_histories[user_id]) > 11:

        user_histories[user_id] = (
            [user_histories[user_id][0]]
            +
            user_histories[user_id][-10:]
        )

    messages_to_send = [
        msg.copy()
        for msg in user_histories[user_id]
    ]

    if mode == "neuroham":

        messages_to_send[-1]["content"] = (
            "[Ответь в стиле саркастичного "
            "и ворчливого мизантропа. "
            "Без мата и без Markdown.]\n\n"
            + prompt
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

            response = ai_client.chat.completions.create(
                model=model_name,
                messages=messages_to_send
            )

            answer = (
                response
                .choices[0]
                .message
                .content
            )

            if not answer:
                continue

            answer = clean_markdown(answer)

            success = True

            break

        except Exception as e:

            print(
                f"⚠️ AI {model_name}: {e}"
            )

    # Groq fallback
    if not success and groq_client:

        try:

            response = groq_client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=messages_to_send
            )

            answer = clean_markdown(
                response
                .choices[0]
                .message
                .content
            )

            success = True

        except Exception as e:

            print(
                f"⚠️ Groq ошибка: {e}"
            )

    if success:

        user_histories[user_id].append({
            "role": "assistant",
            "content": answer
        })

        return answer

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
                        f"- {res.get('title', 'Без заголовка')}: "
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

def analyze_image_gemini(image_bytes):

    if not GEMINI_API_KEY or not genai:

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

            response = model.generate_content([
                "Опиши подробно, что изображено "
                "на этой фотографии. "
                "Ответь на русском языке.",
                image
            ])

            if response and response.text:

                return clean_markdown(
                    response.text
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
# START / HELP
# ============================================================

@bot.message_handler(
    commands=["start", "help"]
)
def help_cmd(message):

    help_text = (
        "Привет! Я ИИ-ассистент.\n\n"

        "Список команд:\n"

        "/search <запрос> — поиск в интернете\n"
        "/weather <город> — погода\n"
        "/image <описание> — создать картинку\n"
        "/music <название или строчка> — поиск музыки 🎵\n"
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

    bot.edit_message_text(
        fact,
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
                + clean_markdown(
                    resp.text.strip()
                )
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

    bot.edit_message_text(
        clean_markdown(reply),
        chat_id=message.chat.id,
        message_id=msg.message_id
    )


# ============================================================
# MUSIC SEARCH
# ============================================================

@bot.message_handler(
    commands=["music"]
)
def music_cmd(message):

    user_id = message.chat.id

    parts = message.text.split(
        maxsplit=1
    )

    raw_query = (
        parts[1].strip()
        if len(parts) > 1
        else ""
    )

    if not raw_query:

        bot.reply_to(
            message,
            "Укажи название трека, строчку "
            "из песни или напиши название "
            "на слух.\n\n"
            "Пример:\n"
            "/music айм блу да ба ди"
        )

        return

    msg = bot.reply_to(
        message,
        "🧠 Определяю песню и ищу её..."
    )

    # --------------------------------------------------------
    # AI определяет песню
    # --------------------------------------------------------

    ai_prompt = (
        f"Пользователь ищет песню. "
        f"Его запрос: '{raw_query}'.\n\n"

        "Определи наиболее вероятные "
        "название песни и исполнителя. "

        "Если текст написан русскими буквами "
        "на слух, попробуй восстановить "
        "оригинальное английское название. "

        "Ответь только в формате:\n"
        "Исполнитель - Название\n\n"

        "Без пояснений."
    )

    refined_query = raw_query

    try:

        response = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": ai_prompt
                }
            ]
        )

        ai_result = (
            response
            .choices[0]
            .message
            .content
            .strip()
        )

        if ai_result:

            refined_query = clean_markdown(
                ai_result
            )

    except Exception as e:

        print(
            f"⚠️ AI music search: {e}"
        )

    # --------------------------------------------------------
    # Основной поиск
    # --------------------------------------------------------

    results = search_youtube_with_cookies(
        refined_query,
        limit=5
    )

    # Если AI ошибся — ищем исходную фразу
    if (
        not results
        and refined_query != raw_query
    ):

        results = search_youtube_with_cookies(
            raw_query,
            limit=5
        )

    if not results:

        bot.edit_message_text(
            "❌ Ничего не найдено.\n\n"
            f"Запрос: {refined_query}",
            chat_id=user_id,
            message_id=msg.message_id
        )

        return

    # --------------------------------------------------------
    # Кэш
    # --------------------------------------------------------

    music_cache[user_id] = results

    text_result = (
        f"🎵 Результаты:\n"
        f"{refined_query}\n\n"
    )

    keyboard = InlineKeyboardMarkup()

    buttons = []

    for i, track in enumerate(
        results,
        1
    ):

        title = track.get(
            "title",
            "Без названия"
        )

        text_result += (
            f"{i}. {title}\n"
        )

        buttons.append(
            InlineKeyboardButton(
                f"Скачать {i}",
                callback_data=f"music_{i - 1}"
            )
        )

    for k in range(
        0,
        len(buttons),
        2
    ):

        keyboard.row(
            *buttons[k:k + 2]
        )

    bot.edit_message_text(
        text_result,
        chat_id=user_id,
        message_id=msg.message_id,
        reply_markup=keyboard
    )


# ============================================================
# MUSIC DOWNLOAD
# ============================================================

@bot.callback_query_handler(
    func=lambda call:
        call.data.startswith("music_")
)
def callback_music(call):

    user_id = call.message.chat.id

    try:

        index = int(
            call.data.split("_")[1]
        )

        results = music_cache.get(
            user_id
        )

        if (
            not results
            or index < 0
            or index >= len(results)
        ):

            bot.answer_callback_query(
                call.id,
                "Список устарел. "
                "Повтори поиск.",
                show_alert=True
            )

            return

        track = results[index]

        title = track.get(
            "title",
            "Трек"
        )

        url = track.get(
            "url"
        )

        if not url:

            bot.answer_callback_query(
                call.id,
                "Ссылка на трек недоступна.",
                show_alert=True
            )

            return

        bot.answer_callback_query(
            call.id,
            f"Скачиваю: {title[:35]}..."
        )

        processing_msg = bot.send_message(
            user_id,
            "⏳ Загружаю аудиофайл..."
        )

        audio_data = None

        # ----------------------------------------------------
        # Получаем Webshare
        # ----------------------------------------------------

        webshare_proxies = (
            get_webshare_proxies()
        )

        # Ограничиваем количество попыток
        # количеством реально полученных прокси.
        #
        # Максимум 10 попыток + одна прямая.
        proxy_attempts = webshare_proxies[:10]

        # Если Webshare не отдал прокси,
        # всё равно пробуем прямое соединение.
        attempts = []

        for proxy in proxy_attempts:

            attempts.append(proxy)

        # Последняя попытка — без прокси.
        attempts.append(None)

        # ----------------------------------------------------
        # Попытки скачивания
        # ----------------------------------------------------

        for attempt_number, proxy in enumerate(
            attempts,
            1
        ):

            if audio_data:
                break

            try:

                with tempfile.TemporaryDirectory() as temp_dir:

                    out_tmpl = os.path.join(
                        temp_dir,
                        "track.%(ext)s"
                    )

                    ydl_opts = {
                        "format":
                            "bestaudio/best",

                        "outtmpl":
                            out_tmpl,

                        "socket_timeout":
                            15,

                        "retries":
                            2,

                        "fragment_retries":
                            2,

                        "noplaylist":
                            True,

                        "quiet":
                            True,

                        "no_warnings":
                            True,

                        "extractor_args": {
                            "youtube": {
                                "player_client": [
                                    "android",
                                    "web"
                                ]
                            }
                        },

                        "postprocessors": [
                            {
                                "key":
                                    "FFmpegExtractAudio",

                                "preferredcodec":
                                    "mp3",

                                "preferredquality":
                                    "192"
                            }
                        ]
                    }

                    # Cookies
                    if os.path.exists(
                        COOKIES_PATH
                    ):

                        ydl_opts[
                            "cookiefile"
                        ] = COOKIES_PATH

                    # Webshare proxy
                    if proxy:

                        ydl_opts[
                            "proxy"
                        ] = proxy

                        print(
                            f"🔄 Попытка "
                            f"{attempt_number}: "
                            f"Webshare proxy"
                        )

                    else:

                        print(
                            f"🔄 Попытка "
                            f"{attempt_number}: "
                            f"прямое соединение"
                        )

                    with yt_dlp.YoutubeDL(
                        ydl_opts
                    ) as ydl:

                        ydl.download(
                            [url]
                        )

                    # Ищем MP3
                    for filename in os.listdir(
                        temp_dir
                    ):

                        if filename.lower().endswith(
                            ".mp3"
                        ):

                            filepath = os.path.join(
                                temp_dir,
                                filename
                            )

                            with open(
                                filepath,
                                "rb"
                            ) as f:

                                audio_data = f.read()

                            break

            except Exception as e:

                print(
                    f"⚠️ Ошибка скачивания "
                    f"на попытке "
                    f"{attempt_number}: "
                    f"{type(e).__name__}"
                )

                # Небольшая пауза
                time.sleep(0.5)

        # ----------------------------------------------------
        # Отправка
        # ----------------------------------------------------

        if audio_data:

            audio_file = io.BytesIO(
                audio_data
            )

            safe_title = re.sub(
                r'[\\/:*?"<>|]',
                "_",
                title
            )

            audio_file.name = (
                f"{safe_title}.mp3"
            )

            try:

                bot.send_audio(
                    chat_id=user_id,
                    audio=audio_file,
                    caption=f"🎵 {title}",
                    title=title
                )

                bot.delete_message(
                    chat_id=user_id,
                    message_id=processing_msg.message_id
                )

            except Exception as e:

                print(
                    f"❌ Ошибка отправки аудио: {e}"
                )

                bot.edit_message_text(
                    "❌ Файл скачан, "
                    "но Telegram не смог его отправить.",
                    chat_id=user_id,
                    message_id=processing_msg.message_id
                )

            return

        # ----------------------------------------------------
        # Не удалось
        # ----------------------------------------------------

        bot.edit_message_text(
            "❌ Не удалось скачать трек.\n\n"
            "Webshare-прокси были проверены, "
            "после чего была сделана попытка "
            "без прокси.",
            chat_id=user_id,
            message_id=processing_msg.message_id
        )

    except Exception as e:

        print(
            f"❌ Music callback error: {e}"
        )

        try:

            bot.answer_callback_query(
                call.id,
                "Ошибка загрузки",
                show_alert=True
            )

        except Exception:
            pass

        bot.send_message(
            user_id,
            "❌ Не удалось обработать загрузку."
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

    bot.edit_message_text(
        reply,
        chat_id=message.chat.id,
        message_id=msg.message_id
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

        bot.edit_message_text(
            "Не удалось сгенерировать.",
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

        bot.edit_message_text(
            f"Ошибка TTS: {e}",
            chat_id=message.chat.id,
            message_id=msg.message_id
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

    text_lower = message.text.lower()

    # Старый специальный ответ
    if (
        "кира" in text_lower
        and "на самом" in text_lower
    ):

        bot.reply_to(
            message,
            "Она самая любимая, "
            "самая лучшая и самая прекрасная ❤️"
        )

        return

    # --------------------------------------------------------
    # URL
    # --------------------------------------------------------

    if (
        "http://" in message.text
        or "https://" in message.text
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

    # --------------------------------------------------------
    # Обычный текст
    # --------------------------------------------------------

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
def handle_doc(message):

    if message.document.mime_type != "application/pdf":

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

        reader = PdfReader(path)

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

    print("=" * 50)
    print("🚀 Бот запускается...")
    print("=" * 50)

    if WEBSHARE_PROXY_URL:

        print(
            "✅ WEBSHARE_PROXY_URL задан"
        )

    else:

        print(
            "⚠️ WEBSHARE_PROXY_URL НЕ задан!"
        )

    if os.path.exists(
        COOKIES_PATH
    ):

        print(
            "✅ YouTube cookies доступны"
        )

    else:

        print(
            "⚠️ YouTube cookies отсутствуют"
        )

    # Flask
    threading.Thread(
        target=run_web,
        daemon=True
    ).start()

    # Telegram
    print(
        "🤖 Telegram polling запущен"
    )

    bot.infinity_polling(
        skip_pending=True,
        timeout=30,
        long_polling_timeout=30
    )
