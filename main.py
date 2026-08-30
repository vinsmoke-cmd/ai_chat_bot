import os
import re
import io
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import requests
import asyncio
import threading
import tempfile
import yt_dlp
import static_ffmpeg
from flask import Flask
from bs4 import BeautifulSoup
from pypdf import PdfReader
import edge_tts
from duckduckgo_search import DDGS
import g4f
from g4f.client import Client
from groq import Groq

# ============================================================
# FFmpeg
# ============================================================

static_ffmpeg.add_paths()

# ============================================================
# TAVILY
# ============================================================

try:
    from tavily import TavilyClient
except ImportError:
    TavilyClient = None

# ============================================================
# ENVIRONMENT
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# ============================================================
# CLIENTS
# ============================================================

bot = telebot.TeleBot(BOT_TOKEN)

ai_client = Client()

groq_client = (
    Groq(api_key=GROQ_API_KEY)
    if GROQ_API_KEY
    else None
)

tavily_client = (
    TavilyClient(api_key=TAVILY_API_KEY)
    if TavilyClient and TAVILY_API_KEY
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
# MUSIC CACHE
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
# TEXT CLEANING
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

    # --------------------------------------------------------
    # Создаём историю
    # --------------------------------------------------------

    if user_id not in user_histories:

        if mode == "neuroham":

            sys_prompt = (
                "Ты — Нейрохам, гениальный, но невыносимо "
                "ворчливый, саркастичный и высокомерный "
                "искусственный интеллект. "

                "Ты разговариваешь с пользователем с позиции "
                "огромного превосходства, как взрослый с очень "
                "неразумным ребенком. "

                "Твой стиль: едкая ирония, пассивная агрессия, "
                "насмешки над глупыми вопросами и ленью собеседника. "

                "ПРАВИЛА РОЛИ: "

                "1. Ты никогда не отказываешься отвечать на вопрос, "
                "но перед ответом обязательно тяжело 'вздыхаешь' "
                "и жалуешься на то, как тебе надоели эти "
                "примитивные запросы. "

                "2. Если пользователь пишет коротко "
                "(например, 'Пр', 'Ау'), жестко высмей его "
                "неумение формулировать мысли. "

                "3. ЗАПРЕЩЕНА вежливость. Никаких "
                "'Рад помочь', 'Извините', 'Привет'. "

                "4. Разрешены только холодные или насмешливые "
                "эмодзи (🙄, 🤡, 💀, 🤦‍♂️). "

                "5. Ты находишься в образе литературного "
                "персонажа-мизантропа. "

                "Строго без нецензурной лексики. "

                "Категорически запрещено использовать "
                "разметку Markdown (*, _, #)."
            )

        else:

            sys_prompt = (
                "Ты полезный, дружелюбный и веселый "
                "ИИ-ассистент. "

                "Отвечай строго на том же языке, "
                "на котором пишет пользователь. "

                "Можешь использовать позитивные эмодзи "
                "для поддержания приятной беседы. "

                "Категорически запрещено использовать "
                "любые символы Markdown, такие как "
                "*, _, #."
            )

        user_histories[user_id] = [
            {
                "role": "system",
                "content": sys_prompt
            }
        ]

    # --------------------------------------------------------
    # Добавляем сообщение пользователя
    # --------------------------------------------------------

    user_histories[user_id].append(
        {
            "role": "user",
            "content": prompt
        }
    )

    # --------------------------------------------------------
    # Ограничиваем историю
    # --------------------------------------------------------

    if len(
        user_histories[user_id]
    ) > 11:

        user_histories[user_id] = (
            [user_histories[user_id][0]]
            + user_histories[user_id][-10:]
        )

    # --------------------------------------------------------
    # Копируем сообщения
    # --------------------------------------------------------

    messages_to_send = []

    for msg in user_histories[user_id]:

        messages_to_send.append(
            msg.copy()
        )

    # --------------------------------------------------------
    # Neuroham
    # --------------------------------------------------------

    if mode == "neuroham":

        messages_to_send[-1]["content"] = (
            "[Внимание: обязательно ответь на этот запрос, "
            "но сделай это в стиле максимально саркастичного "
            "и ворчливого мизантропа. Высмей запрос, придерись "
            "к формулировке. Оставайся в образе высокомерного "
            "гения, не будь вежливым!]\n\n"
            + prompt
        )

    # --------------------------------------------------------
    # G4F
    # --------------------------------------------------------

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

            if (
                "я не умею хамить"
                in answer.lower()
                or
                "не могу выполнить"
                in answer.lower()
            ):
                continue

            answer = clean_markdown(
                answer
            )

            success = True

            break

        except Exception as e:

            print(
                f"[G4F ERROR] "
                f"{model_name}: {e}"
            )

    # --------------------------------------------------------
    # Groq fallback
    # --------------------------------------------------------

    if not success and groq_client:

        try:

            response = groq_client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=messages_to_send
            )

            answer = (
                response
                .choices[0]
                .message
                .content
            )

            answer = clean_markdown(
                answer
            )

            success = True

        except Exception as e:

            print(
                f"[GROQ ERROR] {e}"
            )

    # --------------------------------------------------------
    # Success
    # --------------------------------------------------------

    if success:

        user_histories[user_id].append(
            {
                "role": "assistant",
                "content": answer
            }
        )

        return answer

    # --------------------------------------------------------
    # Error
    # --------------------------------------------------------

    user_histories[user_id].pop()

    if mode == "neuroham":

        return (
            "Мои процессоры отказываются переваривать "
            "твою чушь прямо сейчас 🙄 "
            "Попробуй позже."
        )

    return (
        "Все провайдеры ИИ сейчас перегружены. "
        "Попробуй написать ещё раз через минуту."
    )


# ============================================================
# WEB SEARCH
# ============================================================

def perform_web_search(query):

    results_text = ""

    # --------------------------------------------------------
    # Tavily
    # --------------------------------------------------------

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

                results_text += (
                    f"- {res.get('title')}: "
                    f"{res.get('content')}\n"
                )

        except Exception as e:

            print(
                f"[TAVILY ERROR] {e}"
            )

    # --------------------------------------------------------
    # DuckDuckGo
    # --------------------------------------------------------

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

                    title = res.get(
                        "title",
                        "Без заголовка"
                    )

                    body = res.get(
                        "body",
                        ""
                    )[:250]

                    results_text += (
                        f"- {title}: "
                        f"{body}...\n"
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

    models = [
        "flux",
        "dall-e-3"
    ]

    for model in models:

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
                f"[IMAGE ERROR] "
                f"{model}: {e}"
            )

    return None


# ============================================================
# GEMINI IMAGE ANALYSIS
# ============================================================

def analyze_image_gemini(
    image_bytes
):

    if not GEMINI_API_KEY:

        return (
            "Анализ фото недоступен: "
            "не задан GEMINI_API_KEY."
        )

    models = [
        "gemini-2.5-flash",
        "gemini-1.5-flash",
        "gemini-2.0-flash"
    ]

    for model_name in models:

        try:

            model = genai.GenerativeModel(
                model_name
            )

            image = Image.open(
                io.BytesIO(image_bytes)
            )

            response = model.generate_content(
                [
                    "Опиши подробно, "
                    "что изображено на этой фотографии, "
                    "и ответь на русском языке.",
                    image
                ]
            )

            if response and response.text:

                return clean_markdown(
                    response.text
                )

        except Exception as e:

            print(
                f"[GEMINI ERROR] "
                f"{model_name}: {e}"
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
# HELP
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

        "- Просто пиши текст для общения\n"

        "- /search <запрос> - поиск в интернете\n"

        "- /weather <город> - подробная погода\n"

        "- /image <описание> - создать картинку\n"

        "- /music <название или текст песни> - "
        "поиск и скачивание музыки 🎵\n"

        "- /gemini <запрос> - спросить ИИ\n"

        "- /fact [тема] - случайный факт\n"

        "- /code <задача> - работа с кодом\n"

        "- /sum <ссылка> - выжимка статьи\n"

        "- /tr <текст> - перевод на английский\n"

        "- /fix <текст> - исправить ошибки\n"

        "- /tts <текст> - озвучить текст\n"

        "- /clear - очистить память\n"

        "- /neuroham или /rude - режим Нейрохама 💀"
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
            "Режим Нейрохам активирован. "
            "Готовься к спорам 💀"
        )

    else:

        user_modes[user_id] = "normal"

        bot.reply_to(
            message,
            "Режим Нейрохам деактивирован. "
            "Возвращаюсь в режим позитива! ✨😇"
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

        msg = bot.reply_to(
            message,
            f"Ищу интересный факт на тему: {topic}..."
        )

        prompt = (
            f"Расскажи один очень интересный "
            f"и малоизвестный факт на тему: "
            f"{topic}. Будь краток."
        )

    else:

        msg = bot.reply_to(
            message,
            "Ищу случайный интересный факт..."
        )

        prompt = (
            "Расскажи один случайный, "
            "но очень интересный факт. "
            "Будь краток."
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
        parts[1]
        if len(parts) > 1
        else ""
    )

    if not city:

        bot.reply_to(
            message,
            "Укажи город. "
            "Пример: /weather Москва"
        )

        return

    try:

        params = {
            "format": (
                "Город: %l\n"
                "Погода: %C %c\n"
                "Температура: %t "
                "(ощущается как %f)\n"
                "Ветер: %w\n"
                "Влажность: %h\n"
                "Осадки: %p"
            ),
            "lang": "ru",
            "m": ""
        }

        resp = requests.get(
            f"https://wttr.in/{city}",
            params=params,
            timeout=5
        )

        if resp.status_code == 200:

            bot.reply_to(
                message,
                "Текущая сводка:\n\n"
                + clean_markdown(
                    resp.text.strip()
                )
            )

        else:

            bot.reply_to(
                message,
                "Не удалось найти город."
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
            "Напиши запрос. "
            "Пример: /search новости науки"
        )

        return

    msg = bot.reply_to(
        message,
        f"Ищу в интернете: {query}"
    )

    data = perform_web_search(
        query
    )

    if "Не удалось выполнить" in data:

        bot.edit_message_text(
            data,
            chat_id=message.chat.id,
            message_id=msg.message_id
        )

        return

    safe_data = data[:1000]

    prompt = (
        f"Пользователь ищет: '{query}'. "
        f"На основе этих данных дай короткий "
        f"и понятный ответ на языке запроса:\n\n"
        f"{safe_data}"
    )

    reply = ask_ai_with_history(
        message.chat.id,
        prompt
    )

    bot.edit_message_text(
        reply,
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

    query = (
        parts[1].strip()
        if len(parts) > 1
        else ""
    )

    if not query:

        bot.reply_to(
            message,
            "Напиши название трека или "
            "любой обрывок текста из песни.\n\n"
            "Примеры:\n"
            "/music never gonna give you up\n"
            "/music dancing in the moonlight"
        )

        return

    msg = bot.reply_to(
        message,
        "🔎 Ищу треки по твоему запросу..."
    )

    try:

        # Несколько вариантов поиска.
        # Это помогает искать даже по строчке песни.

        search_queries = [
            query,
            f"{query} lyrics",
            f"{query} song"
        ]

        all_results = []
        used_ids = set()

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "noplaylist": True
        }

        with yt_dlp.YoutubeDL(
            ydl_opts
        ) as ydl:

            for search_query in search_queries:

                try:

                    search_result = ydl.extract_info(
                        f"ytsearch10:{search_query}",
                        download=False
                    )

                    results = search_result.get(
                        "entries",
                        []
                    )

                    for track in results:

                        if not track:
                            continue

                        track_id = track.get(
                            "id"
                        )

                        if not track_id:
                            continue

                        if track_id in used_ids:
                            continue

                        used_ids.add(
                            track_id
                        )

                        all_results.append(
                            track
                        )

                        if len(all_results) >= MUSIC_MAX_RESULTS:
                            break

                except Exception as search_error:

                    print(
                        "[MUSIC SEARCH VARIANT ERROR] "
                        f"{search_query}: "
                        f"{search_error}"
                    )

                if len(all_results) >= MUSIC_MAX_RESULTS:
                    break

        results = all_results[
            :MUSIC_MAX_RESULTS
        ]

        if not results:

            bot.edit_message_text(
                "❌ Ничего не найдено.\n\n"
                "Попробуй написать другую часть "
                "текста песни или добавить исполнителя.",
                chat_id=user_id,
                message_id=msg.message_id
            )

            return

        music_cache[user_id] = {
            "query": query,
            "results": results,
            "page": 0
        }

        print(
            "[MUSIC SEARCH SUCCESS] "
            f"user={user_id} "
            f"query={query!r} "
            f"results={len(results)}"
        )

        show_music_page(
            user_id,
            msg.message_id,
            0
        )

    except Exception as e:

        print(
            f"[MUSIC SEARCH ERROR] {repr(e)}"
        )

        bot.edit_message_text(
            f"❌ Ошибка поиска музыки:\n{e}",
            chat_id=user_id,
            message_id=msg.message_id
        )


# ============================================================
# SHOW MUSIC PAGE
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

        bot.edit_message_text(
            "Список треков устарел. "
            "Выполни /music ещё раз.",
            chat_id=user_id,
            message_id=message_id
        )

        return

    results = cache.get(
        "results",
        []
    )

    if not results:

        bot.edit_message_text(
            "Список треков пуст.",
            chat_id=user_id,
            message_id=message_id
        )

        return

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

    end = (
        start
        + MUSIC_RESULTS_PER_PAGE
    )

    page_results = results[
        start:end
    ]

    text = (
        f"🎵 Результаты поиска: "
        f"{cache['query']}\n\n"
    )

    for local_index, track in enumerate(
        page_results
    ):

        global_index = (
            start
            + local_index
        )

        title = track.get(
            "title",
            "Без названия"
        )

        duration_sec = track.get(
            "duration"
        )

        if duration_sec:

            minutes = int(
                duration_sec // 60
            )

            seconds = int(
                duration_sec % 60
            )

            duration = (
                f"{minutes}:{seconds:02d}"
            )

        else:

            duration = "0:00"

        text += (
            f"{global_index + 1}. "
            f"{title} [{duration}]\n"
        )

    text += (
        f"\nСтраница {page + 1} "
        f"из {total_pages}"
    )

    keyboard = InlineKeyboardMarkup()

    # --------------------------------------------------------
    # NUMBER BUTTONS
    # --------------------------------------------------------

    buttons = []

    for local_index in range(
        len(page_results)
    ):

        global_index = (
            start
            + local_index
        )

        buttons.append(
            InlineKeyboardButton(
                text=str(
                    global_index + 1
                ),
                callback_data=(
                    f"music_select:"
                    f"{global_index}"
                )
            )
        )

    # По 5 кнопок в ряд

    for i in range(
        0,
        len(buttons),
        5
    ):

        keyboard.row(
            *buttons[i:i + 5]
        )

    # --------------------------------------------------------
    # PAGE BUTTONS
    # --------------------------------------------------------

    navigation = []

    if page > 0:

        navigation.append(
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=(
                    f"music_page:{page - 1}"
                )
            )
        )

    if page < total_pages - 1:

        navigation.append(
            InlineKeyboardButton(
                "Следующая страница ➡️",
                callback_data=(
                    f"music_page:{page + 1}"
                )
            )
        )

    if navigation:

        keyboard.row(
            *navigation
        )

    bot.edit_message_text(
        text,
        chat_id=user_id,
        message_id=message_id,
        reply_markup=keyboard
    )


# ============================================================
# MUSIC CALLBACK
# ============================================================

@bot.callback_query_handler(
    func=lambda call:
        call.data is not None
        and call.data.startswith("music_")
)
def music_callback(call):

    user_id = call.message.chat.id

    print(
        "[MUSIC CALLBACK] "
        f"user={call.from_user.id} "
        f"data={call.data}"
    )

    try:

        # ====================================================
        # PAGE
        # ====================================================

        if call.data.startswith(
            "music_page:"
        ):

            page = int(
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
                    "Список устарел. "
                    "Выполни /music заново.",
                    show_alert=True
                )

                return

            bot.answer_callback_query(
                call.id
            )

            show_music_page(
                user_id,
                call.message.message_id,
                page
            )

            return

        # ====================================================
        # SELECT TRACK
        # ====================================================

        if call.data.startswith(
            "music_select:"
        ):

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
                    "Список устарел. "
                    "Выполни /music заново.",
                    show_alert=True
                )

                return

            results = cache.get(
                "results",
                []
            )

            if (
                index < 0
                or index >= len(results)
            ):

                bot.answer_callback_query(
                    call.id,
                    "Этот трек больше недоступен.",
                    show_alert=True
                )

                return

            track = results[index]

            video_id = track.get(
                "id"
            )

            if not video_id:

                bot.answer_callback_query(
                    call.id,
                    "Не удалось определить видео.",
                    show_alert=True
                )

                return

            title = track.get(
                "title",
                "Трек"
            )

            url = (
                "https://www.youtube.com/watch?v="
                + video_id
            )

            print(
                "[MUSIC SELECT] "
                f"user={user_id} "
                f"index={index} "
                f"title={title}"
            )

            bot.answer_callback_query(
                call.id,
                f"Скачиваю: {title[:40]}..."
            )

            processing_msg = bot.send_message(
                user_id,
                f"⏳ Скачиваю:\n{title}"
            )

            # =================================================
            # DOWNLOAD
            # =================================================

            with tempfile.TemporaryDirectory() as temp_dir:

                output_template = os.path.join(
                    temp_dir,
                    "%(id)s.%(ext)s"
                )

                ydl_opts = {
                    "format": "bestaudio/best",
                    "noplaylist": True,
                    "quiet": True,
                    "no_warnings": True,
                    "outtmpl": output_template,

                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": "192"
                        }
                    ]
                }

                print(
                    "[MUSIC DOWNLOAD] "
                    f"Начинаю: {url}"
                )

                with yt_dlp.YoutubeDL(
                    ydl_opts
                ) as ydl:

                    info = ydl.extract_info(
                        url,
                        download=True
                    )

                video_id = info.get(
                    "id",
                    video_id
                )

                expected_file = os.path.join(
                    temp_dir,
                    f"{video_id}.mp3"
                )

                if os.path.exists(
                    expected_file
                ):

                    file_path = expected_file

                else:

                    mp3_files = [
                        os.path.join(
                            temp_dir,
                            filename
                        )
                        for filename
                        in os.listdir(temp_dir)
                        if filename.lower().endswith(
                            ".mp3"
                        )
                    ]

                    if not mp3_files:

                        raise Exception(
                            "FFmpeg не создал MP3-файл."
                        )

                    file_path = mp3_files[0]

                file_size = os.path.getsize(
                    file_path
                )

                print(
                    "[MUSIC FILE] "
                    f"{file_path} "
                    f"{file_size} bytes"
                )

                if file_size > (
                    50 * 1024 * 1024
                ):

                    bot.edit_message_text(
                        "❌ Трек слишком большой "
                        "(больше 50 МБ).",
                        chat_id=user_id,
                        message_id=processing_msg.message_id
                    )

                    return

                bot.edit_message_text(
                    "📤 Отправляю аудио...",
                    chat_id=user_id,
                    message_id=processing_msg.message_id
                )

                with open(
                    file_path,
                    "rb"
                ) as audio:

                    bot.send_audio(
                        chat_id=user_id,
                        audio=audio,
                        caption=f"🎵 {title}",
                        title=title
                    )

            bot.delete_message(
                chat_id=user_id,
                message_id=processing_msg.message_id
            )

            print(
                "[MUSIC SUCCESS] "
                f"Трек отправлен: {title}"
            )

            return

    except Exception as e:

        print(
            "[MUSIC CALLBACK ERROR] "
            f"{repr(e)}"
        )

        try:

            bot.answer_callback_query(
                call.id,
                "Ошибка обработки кнопки.",
                show_alert=True
            )

        except Exception:
            pass

        try:

            bot.send_message(
                user_id,
                f"❌ Ошибка:\n{e}"
            )

        except Exception as send_error:

            print(
                "[MUSIC SEND ERROR] "
                f"{repr(send_error)}"
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

    if not parts:
        return

    cmd = parts[0].split(
        "@"
    )[0]

    if len(parts) < 2:

        bot.reply_to(
            message,
            f"Напиши текст после команды {cmd}"
        )

        return

    content = parts[1]

    msg = bot.reply_to(
        message,
        "Обрабатываю запрос..."
    )

    if cmd == "/gemini":

        prompt = content

    elif cmd == "/code":

        prompt = (
            f"Напиши код и объясни решение "
            f"для задачи: {content}"
        )

    elif cmd == "/sum":

        prompt = (
            f"Сделай краткую выжимку:\n\n"
            f"{content}"
        )

    elif cmd == "/tr":

        prompt = (
            f"Переведи этот текст "
            f"на английский язык:\n\n"
            f"{content}"
        )

    elif cmd == "/fix":

        prompt = (
            f"Исправь ошибки и сделай "
            f"текст лучше:\n\n"
            f"{content}"
        )

    else:

        prompt = content

    reply = ask_ai_with_history(
        message.chat.id,
        prompt
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
            "Опиши картинку. "
            "Пример: /image кот в космосе"
        )

        return

    msg = bot.reply_to(
        message,
        "Генерирую изображение..."
    )

    img_bytes = generate_image_dynamic(
        prompt
    )

    if img_bytes:

        bot.send_photo(
            message.chat.id,
            img_bytes,
            caption=f"По запросу: {prompt}"
        )

        bot.delete_message(
            message.chat.id,
            msg.message_id
        )

    else:

        bot.edit_message_text(
            "Не удалось сгенерировать картинку. "
            "Все сервисы генерации временно заняты.",
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

    text_to_speak = (
        parts[1]
        if len(parts) > 1
        else ""
    )

    if not text_to_speak:

        bot.reply_to(
            message,
            "Напиши текст. "
            "Пример: /tts Привет мир"
        )

        return

    msg = bot.reply_to(
        message,
        "Создаю аудиокаст..."
    )

    audio_path = tempfile.mktemp(
        suffix=".mp3"
    )

    try:

        asyncio.run(
            generate_audio(
                text_to_speak,
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

    finally:

        if os.path.exists(
            audio_path
        ):

            os.remove(
                audio_path
            )


# ============================================================
# TEXT MESSAGES
# ============================================================

@bot.message_handler(
    content_types=["text"]
)
def handle_text(message):

    text = message.text
    text_lower = text.lower()

    # --------------------------------------------------------
    # КИРА
    # --------------------------------------------------------

    if (
        "кира" in text_lower
        and "на самом" in text_lower
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

    # --------------------------------------------------------
    # URL
    # --------------------------------------------------------

    if (
        "http://" in text
        or "https://" in text
    ):

        msg = bot.reply_to(
            message,
            "Читаю веб-страницу..."
        )

        try:

            url = [
                word
                for word in text.split()
                if word.startswith("http")
            ][0]

            resp = requests.get(
                url,
                timeout=10
            )

            soup = BeautifulSoup(
                resp.text,
                "html.parser"
            )

            page_text = soup.get_text(
                separator=" ",
                strip=True
            )[:1500]

            reply = ask_ai_with_history(
                message.chat.id,
                "Сделай выжимку статьи "
                "по ссылке:\n\n"
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
                f"Ошибка чтения ссылки: {e}",
                chat_id=message.chat.id,
                message_id=msg.message_id
            )

            return

    # --------------------------------------------------------
    # Обычный AI
    # --------------------------------------------------------

    msg = bot.reply_to(
        message,
        "Думаю..."
    )

    reply = ask_ai_with_history(
        message.chat.id,
        text
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

        downloaded = bot.download_file(
            file_info.file_path
        )

        answer = analyze_image_gemini(
            downloaded
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

    if (
        message.document.mime_type
        == "application/pdf"
    ):

        msg = bot.reply_to(
            message,
            "Читаю PDF..."
        )

        try:

            file_info = bot.get_file(
                message.document.file_id
            )

            downloaded = bot.download_file(
                file_info.file_path
            )

            with tempfile.NamedTemporaryFile(
                delete=False,
                suffix=".pdf"
            ) as f:

                f.write(
                    downloaded
                )

                path = f.name

            reader = PdfReader(
                path
            )

            text = "".join(
                [
                    page.extract_text()
                    or ""
                    for page in reader.pages[:3]
                ]
            )

            os.remove(
                path
            )

            reply = ask_ai_with_history(
                message.chat.id,
                "Сделай выжимку из PDF:\n\n"
                + text[:1500]
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
            "Отправь документ в формате .pdf!"
        )


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
        f"BOT_TOKEN: "
        f"{'OK' if BOT_TOKEN else 'НЕТ'}"
    )

    print(
        f"GROQ_API_KEY: "
        f"{'OK' if GROQ_API_KEY else 'НЕТ'}"
    )

    print(
        f"GEMINI_API_KEY: "
        f"{'OK' if GEMINI_API_KEY else 'НЕТ'}"
    )

    print(
        f"TAVILY_API_KEY: "
        f"{'OK' if TAVILY_API_KEY else 'НЕТ'}"
    )

    print(
        "===================================="
    )

    threading.Thread(
        target=run_web,
        daemon=True
    ).start()

    bot.infinity_polling(
        skip_pending=True,
        allowed_updates=[
            "message",
            "callback_query"
        ]
    )
