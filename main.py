import os
import re
import io
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
from duckduckgo_search import DDGS
import yt_dlp
import g4f
from g4f.client import Client
from groq import Groq

# Автоматически внедряем FFmpeg в окружение
static_ffmpeg.add_paths()

try:
    from tavily import TavilyClient
except ImportError:
    TavilyClient = None

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

bot = telebot.TeleBot(BOT_TOKEN)
ai_client = Client()
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
tavily_client = TavilyClient(api_key=TAVILY_API_KEY) if (TavilyClient and TAVILY_API_KEY) else None

if GEMINI_API_KEY:
    import google.generativeai as genai
    from PIL import Image
    genai.configure(api_key=GEMINI_API_KEY)

user_histories = {}
user_modes = {}
music_cache = {}
app = Flask(__name__)

@app.route('/')
def home():
    return "Бот работает!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def clean_markdown(text):
    if not text:
        return ""
    return re.sub(r'[*_#]', '', text)


# ============================================================
# МУЗЫКА
# Интернет-поиск через DuckDuckGo
# 10 результатов + ◀️ ▶️
# Скачивание только аудио со страниц,
# где обнаружена информация о свободной лицензии
# ============================================================

MUSIC_PER_PAGE = 10
music_searches = {}

AUDIO_EXTENSIONS = (
    ".mp3",
    ".ogg",
    ".wav",
    ".m4a",
    ".flac",
    ".opus"
)

MAX_AUDIO_SIZE = 50 * 1024 * 1024


def search_music_web(query, page=0):
    """
    Ищет музыку в интернете через DuckDuckGo.

    В поисковый запрос добавляются признаки
    свободной лицензии.
    """

    search_query = (
        f'"{query}" '
        f'(music OR audio OR song OR track) '
        f'(mp3 OR ogg OR wav OR m4a) '
        f'("Creative Commons" OR CC0 OR "public domain")'
    )

    all_results = []

    try:
        with DDGS() as ddgs:

            results = ddgs.text(
                search_query,
                region="wt-wt",
                safesearch="moderate",
                max_results=50
            )

            for item in results:

                title = item.get("title")
                url = item.get("href")
                description = item.get("body", "")

                if not title or not url:
                    continue

                all_results.append({
                    "title": str(title),
                    "url": str(url),
                    "description": str(
                        description or ""
                    )
                })

    except Exception as e:

        print(
            "[MUSIC SEARCH ERROR]",
            repr(e)
        )

    # Убираем дубликаты
    unique_results = []
    seen_urls = set()

    for result in all_results:

        url = result["url"]

        if url in seen_urls:
            continue

        seen_urls.add(url)
        unique_results.append(result)

    start = page * MUSIC_PER_PAGE
    end = start + MUSIC_PER_PAGE

    return (
        unique_results[start:end],
        len(unique_results)
    )


def find_audio_on_page(url):
    """
    Открывает найденную страницу.

    Ищет:
    - <audio>
    - <source>
    - прямые MP3/OGG/WAV ссылки
    - og:audio

    Также проверяет наличие информации
    о свободной лицензии на странице.
    """

    try:

        response = requests.get(
            url,
            timeout=15,
            headers={
                "User-Agent":
                "Mozilla/5.0 "
                "(Linux; Android 15) "
                "AppleWebKit/537.36 "
                "Chrome/140 Mobile Safari/537.36"
            },
            allow_redirects=True
        )

        response.raise_for_status()

        final_url = response.url

        content_type = (
            response.headers
            .get("Content-Type", "")
            .lower()
        )

        # Сама страница оказалась аудиофайлом
        if (
            "audio/" in content_type
            or any(
                ext in final_url.lower()
                for ext in AUDIO_EXTENSIONS
            )
        ):
            return final_url, True

        # Не HTML — дальше страницу анализировать нельзя
        if (
            "text/html" not in content_type
            and "application/xhtml" not in content_type
        ):
            return None, False

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        page_text = soup.get_text(
            " ",
            strip=True
        ).lower()

        # Ищем признаки лицензии
        license_phrases = (
            "creative commons",
            "creativecommons.org",
            "cc by",
            "cc0",
            "public domain",
            "public-domain",
            "free to use",
            "free music",
            "royalty free",
            "royalty-free"
        )

        license_found = any(
            phrase in page_text
            for phrase in license_phrases
        )

        if not license_found:
            return None, False

        # ----------------------------------------------------
        # <audio src="">
        # ----------------------------------------------------

        for audio in soup.find_all("audio"):

            src = audio.get("src")

            if src:

                audio_url = requests.compat.urljoin(
                    response.url,
                    src
                )

                return audio_url, True

            source = audio.find("source")

            if source:

                src = source.get("src")

                if src:

                    audio_url = requests.compat.urljoin(
                        response.url,
                        src
                    )

                    return audio_url, True

        # ----------------------------------------------------
        # <source src="">
        # ----------------------------------------------------

        for source in soup.find_all(
            "source"
        ):

            src = source.get("src")

            if not src:
                continue

            audio_url = requests.compat.urljoin(
                response.url,
                src
            )

            if any(
                ext in audio_url.lower()
                for ext in AUDIO_EXTENSIONS
            ):
                return audio_url, True

        # ----------------------------------------------------
        # <a href="track.mp3">
        # ----------------------------------------------------

        for link in soup.find_all(
            "a",
            href=True
        ):

            href = link.get("href")

            audio_url = requests.compat.urljoin(
                response.url,
                href
            )

            if any(
                ext in audio_url.lower()
                for ext in AUDIO_EXTENSIONS
            ):
                return audio_url, True

        # ----------------------------------------------------
        # og:audio
        # ----------------------------------------------------

        meta_audio = soup.find(
            "meta",
            property="og:audio"
        )

        if meta_audio:

            content = meta_audio.get(
                "content"
            )

            if content:

                audio_url = requests.compat.urljoin(
                    response.url,
                    content
                )

                return audio_url, True

        # ----------------------------------------------------
        # meta audio
        # ----------------------------------------------------

        meta_audio = soup.find(
            "meta",
            attrs={
                "name": "audio"
            }
        )

        if meta_audio:

            content = meta_audio.get(
                "content"
            )

            if content:

                audio_url = requests.compat.urljoin(
                    response.url,
                    content
                )

                return audio_url, True

    except Exception as e:

        print(
            "[MUSIC PAGE ERROR]",
            repr(e)
        )

    return None, False


def download_music_audio(url):
    """
    Скачивает аудиофайл в память.

    Максимальный размер — 50 MB.
    """

    try:

        response = requests.get(
            url,
            stream=True,
            timeout=60,
            headers={
                "User-Agent":
                "Mozilla/5.0"
            },
            allow_redirects=True
        )

        response.raise_for_status()

        content_type = (
            response.headers
            .get("Content-Type", "")
            .lower()
        )

        final_url = response.url.lower()

        is_audio = (
            "audio/" in content_type
            or any(
                ext in final_url
                for ext in AUDIO_EXTENSIONS
            )
        )

        if not is_audio:
            return None

        content_length = response.headers.get(
            "Content-Length"
        )

        if content_length:

            try:

                if int(content_length) > MAX_AUDIO_SIZE:
                    return None

            except ValueError:
                pass

        data = bytearray()

        for chunk in response.iter_content(
            chunk_size=256 * 1024
        ):

            if not chunk:
                continue

            data.extend(chunk)

            if len(data) > MAX_AUDIO_SIZE:
                return None

        if not data:
            return None

        return bytes(data)

    except Exception as e:

        print(
            "[MUSIC DOWNLOAD ERROR]",
            repr(e)
        )

        return None


def music_results_text(
    tracks,
    page
):

    text = (
        f"🎵 Результаты поиска\n"
        f"Страница {page + 1}\n\n"
    )

    for i, track in enumerate(
        tracks,
        start=1
    ):

        title = track.get(
            "title",
            "Без названия"
        )

        text += (
            f"{i}. {title}\n"
        )

    text += (
        "\n👇 Выбери трек:"
    )

    return text


def music_keyboard(
    user_id,
    page,
    total
):

    keyboard = InlineKeyboardMarkup(
        row_width=1
    )

    search = music_searches.get(
        user_id
    )

    if not search:
        return keyboard

    tracks = search.get(
        "tracks",
        []
    )

    for i, track in enumerate(
        tracks
    ):

        title = track.get(
            "title",
            "Без названия"
        )

        button_text = (
            f"{i + 1}. {title}"
        )

        if len(button_text) > 50:

            button_text = (
                button_text[:47]
                + "..."
            )

        keyboard.add(
            InlineKeyboardButton(
                button_text,
                callback_data=(
                    f"music_get:"
                    f"{user_id}:"
                    f"{i}"
                )
            )
        )

    navigation = []

    # ◀️
    if page > 0:

        navigation.append(
            InlineKeyboardButton(
                "◀️",
                callback_data=(
                    f"music_page:"
                    f"{user_id}:"
                    f"{page - 1}"
                )
            )
        )

    # ▶️
    if total > (
        (page + 1) * MUSIC_PER_PAGE
    ):

        navigation.append(
            InlineKeyboardButton(
                "▶️",
                callback_data=(
                    f"music_page:"
                    f"{user_id}:"
                    f"{page + 1}"
                )
            )
        )

    if navigation:

        keyboard.row(
            *navigation
        )

    return keyboard


# ============================================================
# /music
# ============================================================

@bot.message_handler(
    commands=['music']
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
            "🎵 Укажи название трека.\n\n"
            "Пример:\n"
            "/music Alan Walker"
        )

        return

    msg = bot.reply_to(
        message,
        "🌐 Ищу музыку в интернете... 🎧"
    )

    try:

        # Сначала пробуем уточнить запрос через g4f
        refined_query = raw_query

        try:

            ai_prompt = (
                "Определи, какую песню ищет пользователь. "
                "Запрос может содержать ошибки или быть "
                "неполной строкой. "
                "Напиши только исполнителя и название "
                "песни без пояснений.\n\n"
                f"Запрос: {raw_query}"
            )

            response = ai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "user",
                        "content": ai_prompt
                    }
                ]
            )

            possible_query = (
                response.choices[0]
                .message
                .content
                .strip()
            )

            if possible_query:
                refined_query = possible_query

        except Exception as e:

            print(
                "[MUSIC AI RECOGNITION ERROR]",
                repr(e)
            )

        # Интернет-поиск
        tracks, total = search_music_web(
            refined_query,
            page=0
        )

        # Если по уточнённому запросу ничего нет —
        # пробуем оригинальный запрос
        if not tracks and refined_query != raw_query:

            tracks, total = search_music_web(
                raw_query,
                page=0
            )

            refined_query = raw_query

        if not tracks:

            bot.edit_message_text(
                "❌ Не удалось найти подходящие "
                "аудиозаписи в интернете.\n\n"
                "Попробуй другой запрос.",
                chat_id=user_id,
                message_id=msg.message_id
            )

            return

        # Сохраняем поиск
        music_searches[user_id] = {
            "query": refined_query,
            "page": 0,
            "tracks": tracks,
            "total": total
        }

        text = music_results_text(
            tracks,
            0
        )

        keyboard = music_keyboard(
            user_id,
            0,
            total
        )

        bot.edit_message_text(
            text,
            chat_id=user_id,
            message_id=msg.message_id,
            reply_markup=keyboard
        )

    except Exception as e:

        print(
            "[MUSIC COMMAND ERROR]",
            repr(e)
        )

        try:

            bot.edit_message_text(
                "❌ Ошибка поиска музыки.",
                chat_id=user_id,
                message_id=msg.message_id
            )

        except Exception:
            pass


# ============================================================
# Переключение страниц ◀️ ▶️
# ============================================================

@bot.callback_query_handler(
    func=lambda call:
        call.data.startswith(
            "music_page:"
        )
)
def callback_music_page(call):

    try:

        _, user_id, page = (
            call.data.split(":")
        )

        user_id = int(user_id)
        page = int(page)

        # Нельзя управлять чужим поиском
        if call.from_user.id != user_id:

            bot.answer_callback_query(
                call.id,
                "❌ Это не твой поиск."
            )

            return

        search = music_searches.get(
            user_id
        )

        if not search:

            bot.answer_callback_query(
                call.id,
                "❌ Поиск устарел.",
                show_alert=True
            )

            return

        bot.answer_callback_query(
            call.id,
            "🔎 Загружаю страницу..."
        )

        tracks, total = search_music_web(
            search["query"],
            page=page
        )

        if not tracks:

            bot.answer_callback_query(
                call.id,
                "Больше результатов нет.",
                show_alert=True
            )

            return

        search["page"] = page
        search["tracks"] = tracks
        search["total"] = total

        bot.edit_message_text(
            music_results_text(
                tracks,
                page
            ),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=music_keyboard(
                user_id,
                page,
                total
            )
        )

    except Exception as e:

        print(
            "[MUSIC PAGE ERROR]",
            repr(e)
        )

        try:

            bot.answer_callback_query(
                call.id,
                "❌ Ошибка загрузки страницы.",
                show_alert=True
            )

        except Exception:
            pass


# ============================================================
# Выбор конкретного трека
# ============================================================

@bot.callback_query_handler(
    func=lambda call:
        call.data.startswith(
            "music_get:"
        )
)
def callback_music_get(call):

    user_id = call.message.chat.id

    try:

        _, search_user_id, index = (
            call.data.split(":")
        )

        search_user_id = int(
            search_user_id
        )

        index = int(index)

        # Проверяем владельца поиска
        if call.from_user.id != search_user_id:

            bot.answer_callback_query(
                call.id,
                "❌ Это не твой поиск.",
                show_alert=True
            )

            return

        search = music_searches.get(
            search_user_id
        )

        if not search:

            bot.answer_callback_query(
                call.id,
                "❌ Список устарел.",
                show_alert=True
            )

            return

        tracks = search.get(
            "tracks",
            []
        )

        if (
            index < 0
            or index >= len(tracks)
        ):

            bot.answer_callback_query(
                call.id,
                "❌ Трек не найден.",
                show_alert=True
            )

            return

        track = tracks[index]

        title = track.get(
            "title",
            "Трек"
        )

        page_url = track.get(
            "url"
        )

        if not page_url:

            bot.answer_callback_query(
                call.id,
                "❌ Ссылка на источник отсутствует.",
                show_alert=True
            )

            return

        bot.answer_callback_query(
            call.id,
            "🔎 Проверяю источник..."
        )

        processing_msg = bot.send_message(
            user_id,
            f"🔎 Проверяю:\n"
            f"🎵 {title}"
        )

        # Ищем аудиофайл на странице
        audio_url, license_found = (
            find_audio_on_page(
                page_url
            )
        )

        if not audio_url:

            bot.edit_message_text(
                "❌ На найденной странице "
                "не удалось обнаружить "
                "доступный аудиофайл.",
                chat_id=user_id,
                message_id=processing_msg.message_id
            )

            return

        # Не скачиваем, если на странице
        # нет признаков свободной лицензии
        if not license_found:

            bot.edit_message_text(
                "❌ Не удалось подтвердить, "
                "что этот аудиофайл можно "
                "свободно распространять.",
                chat_id=user_id,
                message_id=processing_msg.message_id
            )

            return

        bot.edit_message_text(
            f"⬇️ Скачиваю:\n"
            f"🎵 {title}",
            chat_id=user_id,
            message_id=processing_msg.message_id
        )

        audio_data = download_music_audio(
            audio_url
        )

        if not audio_data:

            bot.edit_message_text(
                "❌ Аудиофайл не удалось скачать.\n\n"
                "Попробуй выбрать другой результат.",
                chat_id=user_id,
                message_id=processing_msg.message_id
            )

            return

        # Безопасное имя файла
        safe_title = re.sub(
            r'[\\/:*?"<>|]',
            "_",
            title
        )

        safe_title = safe_title.strip()

        if not safe_title:
            safe_title = "track"

        if len(safe_title) > 80:
            safe_title = safe_title[:80]

        audio_file = io.BytesIO(
            audio_data
        )

        audio_file.name = (
            safe_title + ".mp3"
        )

        caption = (
            f"🎵 {title}\n\n"
            f"🌐 Источник:\n"
            f"{page_url}"
        )

        try:

            bot.send_audio(
                chat_id=user_id,
                audio=audio_file,
                title=title,
                caption=caption
            )

            bot.delete_message(
                chat_id=user_id,
                message_id=processing_msg.message_id
            )

        except Exception as e:

            print(
                "[MUSIC TELEGRAM SEND ERROR]",
                repr(e)
            )

            bot.edit_message_text(
                "❌ Не удалось отправить "
                "аудиофайл в Telegram.",
                chat_id=user_id,
                message_id=processing_msg.message_id
            )

    except Exception as e:

        print(
            "[MUSIC GET ERROR]",
            repr(e)
        )

        try:

            bot.answer_callback_query(
                call.id,
                "❌ Ошибка загрузки.",
                show_alert=True
            )

        except Exception:
            pass

        try:

            bot.send_message(
                user_id,
                "❌ Не удалось получить аудиофайл."
            )

        except Exception:
            pass


# ============================================================
# ИИ
# ============================================================

def ask_ai_with_history(user_id, prompt):
    mode = user_modes.get(user_id, "normal")

    if user_id not in user_histories:  
        if mode == "neuroham":  
            sys_prompt = (  
                "Ты — Нейрохам, гениальный, но невыносимо ворчливый, саркастичный и высокомерный искусственный интеллект. "  
                "Ты разговариваешь с пользователем с позиции огромного превосходства, как взрослый с очень неразумным ребенком. "  
                "Твой стиль: едкая ирония, пассивная агрессия, насмешки над глупыми вопросами и ленью собеседника. "  
                "ПРАВИЛА РОЛИ: "  
                "1. Ты никогда не отказываешься отвечать на вопрос, но перед ответом обязательно тяжело 'вздыхаешь' и жалуешься на то, как тебе надоели эти примитивные запросы. "  
                "2. Если пользователь пишет коротко (например, 'Пр', 'Ау'), жестко высмей его неумение формулировать мысли. "  
                "3. ЗАПРЕЩЕНА вежливость. Никаких 'Рад помочь', 'Извините', 'Привет'. "  
                "4. Разрешены только холодные или насмешливые эмодзи (🙄, 🤡, 💀, 🤦‍♂️). "  
                "5. Ты находишься в образе литературного персонажа-мизантропа. Никаких отказов от роли! Строго без нецензурной лексики. "  
                "Категорически запрещено использовать разметку Markdown (*, _, #)."  
            )  
        else:  
            sys_prompt = (  
                "Ты полезный, дружелюбный и веселый ИИ-ассистент. Отвечай строго на том же языке. "  
                "Можешь смело использовать любые позитивные эмодзи для поддержания приятной беседы! "  
                "Категорически запрещено использовать любые символы Markdown, такие как *, _, #."  
            )  
              
        user_histories[user_id] = [{  
            "role": "system",   
            "content": sys_prompt  
        }]  
      
    user_histories[user_id].append({"role": "user", "content": prompt})  
      
    if len(user_histories[user_id]) > 11:  
        user_histories[user_id] = [user_histories[user_id][0]] + user_histories[user_id][-10:]  
          
    messages_to_send = [msg.copy() for msg in user_histories[user_id]]  
          
    if mode == "neuroham":  
        messages_to_send[-1]["content"] = (  
            f"[Внимание: Обязательно ответь на этот запрос, но сделай это в стиле максимально саркастичного и ворчливого мизантропа. "  
            f"Высмей запрос, придерись к формулировке. Оставайся в образе высокомерного гения, не будь вежливым!]\n\n{prompt}"  
        )  

    models_to_try = ["gpt-3.5-turbo", "gpt-4o-mini", "gpt-4", "llama-3-70b"]  
    success = False  
    answer = ""  
      
    for model_name in models_to_try:  
        try:  
            response = ai_client.chat.completions.create(model=model_name, messages=messages_to_send)  
            answer = response.choices[0].message.content  
              
            if "я не умею хамить" in answer.lower() or "не могу выполнить" in answer.lower():  
                continue   
                  
            answer = clean_markdown(answer)  
            success = True  
            break  
        except Exception:  
            continue  
              
    if not success and groq_client:  
        try:  
            response = groq_client.chat.completions.create(model="openai/gpt-oss-120b", messages=messages_to_send)  
            answer = clean_markdown(response.choices[0].message.content)  
            success = True  
        except Exception:  
            success = False  

    if success:  
        user_histories[user_id].append({"role": "assistant", "content": answer})  
        return answer  
              
    user_histories[user_id].pop()  
    return "Мои процессоры отказываются переваривать твою чушь прямо сейчас 🙄 Попробуй позже, если вспомнишь как." if mode == "neuroham" else "Все провайдеры ИИ сейчас перегружены. Попробуй написать еще раз через минуту."


def perform_web_search(query):
    results_text = ""
    if tavily_client:
        try:
            response = tavily_client.search(query=query, max_results=3)
            for res in response.get('results', []):
                results_text += f"- {res.get('title')}: {res.get('content')}\n"
        except Exception:
            pass

    if not results_text:  
        try:  
            with DDGS() as ddgs:  
                results = list(ddgs.text(query, max_results=3))  
                for res in results:  
                    results_text += f"- {res.get('title', 'Без заголовка')}: {res.get('body', '')[:250]}...\n"  
        except Exception as e:  
            results_text = f"Не удалось выполнить поиск: {e}"  
    return results_text


def generate_image_dynamic(prompt):
    for model in ["flux", "dall-e-3"]:
        try:
            response = ai_client.images.generate(model=model, prompt=prompt, response_format="url")
            image_url = response.data[0].url
            if image_url:
                r = requests.get(image_url, timeout=25)
                if r.status_code == 200:
                    return r.content
        except Exception:
            continue
    return None


def analyze_image_gemini(image_bytes):
    if not GEMINI_API_KEY:
        return "Анализ фото недоступен: не задан GEMINI_API_KEY."

    for model_name in ['gemini-2.5-flash', 'gemini-1.5-flash', 'gemini-2.0-flash']:  
        try:  
            model = genai.GenerativeModel(model_name)  
            image = Image.open(io.BytesIO(image_bytes))  
            response = model.generate_content(["Опиши подробно, что изображено на этой фотографии, и ответь на русском языке.", image])  
            if response and response.text:  
                return clean_markdown(response.text)  
        except Exception:  
            continue  
    return "Не удалось получить ответ от Gemini."


async def generate_audio(text, output_file):
    communicate = edge_tts.Communicate(text, "ru-RU-SvetlanaNeural")
    await communicate.save(output_file)


@bot.message_handler(commands=['start', 'help'])
def help_cmd(message):
    help_text = (
        "Привет! Я ИИ-ассистент.\n\n"
        "Список команд:\n"
        "- /search <запрос> - поиск в интернете\n"
        "- /weather <город> - подробная погода\n"
        "- /image <описание> - создать картинку\n"
        "- /music <название или строчка> - поиск и скачивание трека 🎵\n"
        "- /gemini <запрос> - спросить ИИ\n"
        "- /fact [тема] - случайный факт\n"
        "- /code <задача> - работа с кодом\n"
        "- /sum <ссылка> - выжимка статьи\n"
        "- /tr <текст> - перевод на английский\n"
        "- /fix <текст> - исправить ошибки\n"
        "- /tts <текст> - озвучить текст\n"
        "- /clear - очистить память\n"
        "- /neuroham (или /rude) - режим Нейрохама 💀"
    )
    bot.reply_to(message, help_text)


@bot.message_handler(commands=['neuroham', 'rude'])
def toggle_neuroham_mode(message):
    user_id = message.chat.id
    if user_modes.get(user_id, "normal") == "normal":  
        user_modes[user_id] = "neuroham"  
        bot.reply_to(message, "Режим Нейрохам активирован 💀")  
    else:  
        user_modes[user_id] = "normal"  
        bot.reply_to(message, "Режим Нейрохам деактивирован ✨")  
    if user_id in user_histories:  
        del user_histories[user_id]


@bot.message_handler(commands=['clear'])
def clear_cmd(message):
    if message.chat.id in user_histories:
        del user_histories[message.chat.id]
    bot.reply_to(message, "Память диалога очищена.")


@bot.message_handler(commands=['fact'])
def fact_cmd(message):
    parts = message.text.split(maxsplit=1)
    topic = parts[1] if len(parts) > 1 else ""
    prompt = f"Расскажи один интересный факт на тему: {topic}. Будь краток." if topic else "Расскажи один случайный интересный факт. Будь краток."
    msg = bot.reply_to(message, "Ищу факт...")  
    fact = ask_ai_with_history(message.chat.id, prompt)  
    bot.edit_message_text(fact, chat_id=message.chat.id, message_id=msg.message_id)


@bot.message_handler(commands=['weather'])
def weather_cmd(message):
    parts = message.text.split(maxsplit=1)
    city = parts[1] if len(parts) > 1 else ""
    if not city:
        bot.reply_to(message, "Укажи город. Пример: /weather Москва")
        return
    try:
        resp = requests.get(
            f"https://wttr.in/{city}", 
            params={'format': 'Город: %l\nПогода: %C %c\nТемпература: %t (ощущается как %f)\nВетер: %w\nВлажность: %h', 'lang': 'ru', 'm': ''}, 
            timeout=5
        )
        if resp.status_code == 200:
            bot.reply_to(message, f"Сводка:\n\n{clean_markdown(resp.text.strip())}")
        else:
            bot.reply_to(message, "Город не найден.")
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}")


@bot.message_handler(commands=['search'])
def search_cmd(message):
    parts = message.text.split(maxsplit=1)
    query = parts[1] if len(parts) > 1 else ""
    if not query:
        bot.reply_to(message, "Напиши запрос. Пример: /search новости")
        return
    msg = bot.reply_to(message, f"Ищу: {query}")  
    raw_data = perform_web_search(query)  
    
    prompt = (
        f"Вот результаты поиска из интернета по запросу '{query}':\n{raw_data}\n\n"
        "Сделай краткую, понятную выжимку на русском языке строго по делу. "
        "Не пиши фразы вроде 'на основе предоставленных данных', 'по вашему запросу выявлено' и т.д. "
        "Просто ответь на вопрос или дай суть."
    )
    
    reply = ask_ai_with_history(message.chat.id, prompt)
    bot.edit_message_text(clean_markdown(reply), chat_id=message.chat.id, message_id=msg.message_id)


@bot.message_handler(commands=['gemini', 'code', 'sum', 'tr', 'fix'])
def ai_tools_cmd(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, f"Напиши текст после команды")  
        return  
    msg = bot.reply_to(message, "Обрабатываю...")  
    reply = ask_ai_with_history(message.chat.id, parts[1])  
    bot.edit_message_text(reply, chat_id=message.chat.id, message_id=msg.message_id)


@bot.message_handler(commands=['image'])
def image_cmd(message):
    parts = message.text.split(maxsplit=1)
    prompt = parts[1] if len(parts) > 1 else ""
    if not prompt:
        bot.reply_to(message, "Опиши картинку. Пример: /image кот")
        return
    msg = bot.reply_to(message, "Генерирую...")
    img_bytes = generate_image_dynamic(prompt)
    if img_bytes:
        bot.send_photo(message.chat.id, img_bytes, caption=f"Запрос: {prompt}")
        bot.delete_message(message.chat.id, msg.message_id)
    else:
        bot.edit_message_text("Не удалось сгенерировать.", chat_id=message.chat.id, message_id=msg.message_id)


@bot.message_handler(commands=['tts'])
def tts_cmd(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Напиши текст для озвучки.")
        return
    msg = bot.reply_to(message, "Озвучиваю...")
    audio_path = tempfile.mktemp(suffix=".mp3")
    asyncio.run(generate_audio(parts[1], audio_path))
    with open(audio_path, 'rb') as audio:
        bot.send_voice(message.chat.id, audio)
        bot.delete_message(message.chat.id, msg.message_id)
    os.remove(audio_path)


@bot.message_handler(content_types=['text'])
def handle_text(message):
    if "кира" in message.text.lower() and "на самом" in message.text.lower():  
        bot.reply_to(message, "Она самая любимая, самая лучшая и самая прекрасная ❤️")  
        return
    if "http://" in message.text or "https://" in message.text:  
        msg = bot.reply_to(message, "Читаю ссылку...")  
        try:  
            url = [w for w in message.text.split() if w.startswith("http")][0]  
            resp = requests.get(url, timeout=10)  
            page_text = BeautifulSoup(resp.text, 'html.parser').get_text(separator=' ', strip=True)[:1500]  
            reply = ask_ai_with_history(message.chat.id, f"Сделай выжимку:\n\n{page_text}")  
            bot.edit_message_text(reply, chat_id=message.chat.id, message_id=msg.message_id)  
            return  
        except Exception as e:  
            bot.edit_message_text(f"Ошибка: {e}", chat_id=message.chat.id, message_id=msg.message_id)  
            return  

    msg = bot.reply_to(message, "Думаю...")  
    reply = ask_ai_with_history(message.chat.id, message.text)  
    bot.edit_message_text(reply, chat_id=message.chat.id, message_id=msg.message_id)


@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    msg = bot.reply_to(message, "Изучаю фото...")
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        answer = analyze_image_gemini(bot.download_file(file_info.file_path))
        bot.edit_message_text(answer, chat_id=message.chat.id, message_id=msg.message_id)
    except Exception as e:
        bot.edit_message_text(f"Ошибка: {e}", chat_id=message.chat.id, message_id=msg.message_id)


@bot.message_handler(content_types=['document'])
def handle_doc(message):
    if message.document.mime_type == 'application/pdf':
        msg = bot.reply_to(message, "Читаю PDF...")
        try:
            file_info = bot.get_file(message.document.file_id)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
                f.write(bot.download_file(file_info.file_path))
                path = f.name
            text = "".join([p.extract_text() for p in PdfReader(path).pages[:3]])
            os.remove(path)
            reply = ask_ai_with_history(message.chat.id, f"Выжимка из PDF:\n\n{text[:1500]}")
            bot.edit_message_text(reply, chat_id=message.chat.id, message_id=msg.message_id)
        except Exception as e:
            bot.edit_message_text(f"Ошибка PDF: {e}", chat_id=message.chat.id, message_id=msg.message_id)
    else:
        bot.reply_to(message, "Отправьте документ в формате .pdf")


if __name__ == "__main__":
    threading.Thread(
        target=run_web,
        daemon=True
    ).start()

    bot.infinity_polling()
