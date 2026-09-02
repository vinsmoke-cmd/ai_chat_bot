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
# Поиск только на Sefon: sefon.pro
# Многоступенчатый поиск + fuzzy matching + опечатки
# ============================================================

from difflib import SequenceMatcher
from urllib.parse import urljoin, urlparse, quote_plus

MUSIC_SITE = "https://sefon.pro"
MUSIC_START_URL = f"{MUSIC_SITE}/best/"
MUSIC_PER_PAGE = 8
MUSIC_MAX_RESULTS = 40
MUSIC_MAX_SCAN_PAGES = 8
music_searches = {}

AUDIO_EXTENSIONS = (".mp3", ".ogg", ".wav", ".m4a", ".flac", ".opus", ".aac", ".webm")
MAX_AUDIO_SIZE = 50 * 1024 * 1024
MUSIC_HEADERS = {"User-Agent": "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/140.0 Mobile Safari/537.36", "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}


def music_normalize(text):
    text = str(text or "").lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def music_similarity(query, candidate):
    q, c = music_normalize(query), music_normalize(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if q in c:
        return 0.95
    q_tokens, c_tokens = q.split(), c.split()
    base = SequenceMatcher(None, q, c).ratio()
    token_scores = []
    for qt in q_tokens:
        token_scores.append(max(SequenceMatcher(None, qt, ct).ratio() for ct in c_tokens))
    token_score = sum(token_scores) / len(token_scores) if token_scores else 0.0
    overlap = len(set(q_tokens) & set(c_tokens)) / max(1, len(set(q_tokens)))
    return min(1.0, base * 0.35 + token_score * 0.45 + overlap * 0.20)


def _same_music_site(url):
    try:
        return urlparse(url).netloc.lower().split(":")[0] in {"sefon.pro", "www.sefon.pro"}
    except Exception:
        return False


def _looks_like_audio_url(url):
    return bool(url) and any(ext in str(url).lower() for ext in AUDIO_EXTENSIONS)


def check_audio_url(url):
    if not url:
        return False
    try:
        r = requests.get(url, stream=True, timeout=15, headers=MUSIC_HEADERS, allow_redirects=True)
        ok = r.status_code < 400 and ((r.headers.get("Content-Type") or "").lower().startswith("audio/") or _looks_like_audio_url(r.url))
        r.close()
        return ok
    except Exception:
        return False


def find_audio_on_page(url):
    if not url or not _same_music_site(url):
        return None
    try:
        r = requests.get(url, timeout=20, headers=MUSIC_HEADERS, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        candidates = []
        for tag in soup.find_all(["audio", "source"]):
            for attr in ("src", "data-src", "data-url"):
                if tag.get(attr):
                    candidates.append(urljoin(r.url, tag.get(attr)))
        for tag in soup.find_all("a", href=True):
            href = urljoin(r.url, tag["href"])
            label = tag.get_text(" ", strip=True).lower()
            if _looks_like_audio_url(href) or "скач" in label or "download" in label:
                candidates.append(href)
        for tag in soup.find_all(True):
            for attr, value in tag.attrs.items():
                if not str(attr).startswith("data-"):
                    continue
                value = " ".join(value) if isinstance(value, list) else str(value)
                if ".mp3" in value.lower() or "audio" in value.lower():
                    candidates.append(urljoin(r.url, value.strip(" '\"")))
        seen = set()
        for candidate in candidates:
            candidate = str(candidate).strip()
            if candidate and candidate not in seen:
                seen.add(candidate)
                if check_audio_url(candidate):
                    return candidate
    except Exception as e:
        print("[MUSIC AUDIO PAGE ERROR]", repr(e))
    return None


def download_music_audio(url):
    try:
        r = requests.get(url, stream=True, timeout=30, headers=MUSIC_HEADERS, allow_redirects=True)
        r.raise_for_status()
        ct = (r.headers.get("Content-Type") or "").lower()
        if not ct.startswith("audio/") and not _looks_like_audio_url(r.url):
            r.close(); return None
        size = r.headers.get("Content-Length")
        if size and int(size) > MAX_AUDIO_SIZE:
            r.close(); return None
        buf, total = io.BytesIO(), 0
        for chunk in r.iter_content(64 * 1024):
            if chunk:
                total += len(chunk)
                if total > MAX_AUDIO_SIZE:
                    r.close(); return None
                buf.write(chunk)
        r.close()
        return buf.getvalue()
    except Exception as e:
        print("[MUSIC DOWNLOAD ERROR]", repr(e))
        return None


def get_audio_extension(url, content_type=""):
    mapping = {"audio/mpeg":".mp3", "audio/mp3":".mp3", "audio/mp4":".m4a", "audio/x-m4a":".m4a", "audio/ogg":".ogg", "audio/wav":".wav", "audio/x-wav":".wav", "audio/flac":".flac", "audio/opus":".opus", "audio/aac":".aac", "audio/webm":".webm"}
    ct = (content_type or "").lower().split(";", 1)[0].strip()
    if ct in mapping:
        return mapping[ct]
    for ext in AUDIO_EXTENSIONS:
        if ext in (url or "").lower():
            return ext
    return ".mp3"


def _extract_sefon_links(page_url):
    links = []
    try:
        r = requests.get(page_url, timeout=20, headers=MUSIC_HEADERS, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = urljoin(r.url, a["href"])
            if _same_music_site(href) and urlparse(href).path.lower().rstrip("/").startswith("/mp3/") and href not in links:
                links.append(href)
                if len(links) >= MUSIC_MAX_RESULTS * 3:
                    break
    except Exception as e:
        print("[MUSIC LINKS ERROR]", repr(e))
    return links


def _parse_sefon_track(url):
    try:
        r = requests.get(url, timeout=20, headers=MUSIC_HEADERS, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else ""
        if not title:
            og = soup.find("meta", attrs={"property":"og:title"})
            title = og.get("content", "") if og else ""
        if not title and soup.title:
            title = soup.title.get_text(" ", strip=True)
        meta = soup.find("meta", attrs={"name":"description"})
        description = meta.get("content", "") if meta else ""
        return {"title": title or url, "description": description or "", "text": soup.get_text(" ", strip=True)[:30000], "url": url}
    except Exception as e:
        print("[MUSIC TRACK PARSE ERROR]", repr(e))
        return {"title": url, "description": "", "text": "", "url": url}


def _sefon_candidate_pages(query):
    """Собирает только страницы треков Sefon. Несколько вариантов URL поиска используются как запасные."""
    encoded = quote_plus(query)
    index_pages = [MUSIC_START_URL]
    index_pages += [f"{MUSIC_SITE}/best/page/{n}/" for n in range(2, MUSIC_MAX_SCAN_PAGES + 1)]
    index_pages += [f"{MUSIC_SITE}/search/?q={encoded}", f"{MUSIC_SITE}/search/?query={encoded}", f"{MUSIC_SITE}/search/{encoded}/"]
    links, seen = [], set()
    for page_url in index_pages:
        for link in _extract_sefon_links(page_url):
            if link not in seen:
                seen.add(link); links.append(link)
            if len(links) >= MUSIC_MAX_RESULTS * 3:
                return links
    return links


def music_search_web(query, page=0):
    query = (query or "").strip()
    if not query:
        return [], 0
    links, seen = [], set()
    for variant in (query, music_normalize(query)):
        if not variant:
            continue
        for link in _sefon_candidate_pages(variant):
            if link not in seen:
                seen.add(link); links.append(link)
            if len(links) >= MUSIC_MAX_RESULTS * 3:
                break
    scored = []
    for link in links:
        info = _parse_sefon_track(link)
        title, desc, text = info["title"], info["description"], info["text"]
        title_score = music_similarity(query, title)
        desc_score = music_similarity(query, desc)
        text_score = music_similarity(query, text)
        score = max(title_score * .75 + desc_score * .15 + text_score * .10, title_score * .60 + music_similarity(query, title + " " + desc) * .40)
        if score >= .36:
            scored.append({"title": title, "url": link, "description": desc, "score": score})
    scored.sort(key=lambda x: x["score"], reverse=True)
    unique, used = [], set()
    for item in scored:
        if item["url"] in used: continue
        used.add(item["url"]); unique.append(item)
        if len(unique) >= MUSIC_MAX_RESULTS: break
    start, end = page * MUSIC_PER_PAGE, (page + 1) * MUSIC_PER_PAGE
    return unique[start:end], len(unique)


def music_results_text(tracks, page):
    text = f"🎵 Результаты поиска на Sefon\nСтраница {page + 1}\n\n"
    for i, track in enumerate(tracks, 1):
        text += f"{i}. {track.get('title', 'Без названия')} • {int(round(track.get('score', 0) * 100))}%\n"
    return text + "\n👇 Выбери подходящий трек:"


def music_keyboard(user_id, page, total):
    keyboard = InlineKeyboardMarkup(row_width=1)
    search = music_searches.get(user_id)
    if not search: return keyboard
    for i, track in enumerate(search.get("tracks", [])):
        title = track.get("title", "Без названия")
        score = int(round(track.get("score", 0) * 100))
        label = f"{i + 1}. {title} ({score}%)"
        keyboard.add(InlineKeyboardButton(label[:50], callback_data=f"music_get:{user_id}:{i}"))
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("◀️", callback_data=f"music_page:{user_id}:{page - 1}"))
    if total > (page + 1) * MUSIC_PER_PAGE: nav.append(InlineKeyboardButton("▶️", callback_data=f"music_page:{user_id}:{page + 1}"))
    if nav: keyboard.row(*nav)
    return keyboard


@bot.message_handler(commands=["music"])
def music_cmd(message):
    user_id = message.chat.id
    parts = message.text.split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""
    if not query:
        bot.reply_to(message, "🎵 Напиши исполнителя, название или фрагмент текста.\n\nПример:\n/music виртуалная любв")
        return
    msg = bot.reply_to(message, "🔎 Ищу только на Sefon... 🎵")
    try:
        tracks, total = music_search_web(query, 0)
        if not tracks:
            bot.edit_message_text("❌ На Sefon ничего подходящего не найдено.\n\nПопробуй более длинный фрагмент или добавь исполнителя.", chat_id=user_id, message_id=msg.message_id)
            return
        music_searches[user_id] = {"query": query, "page": 0, "tracks": tracks, "total": total}
        bot.edit_message_text(music_results_text(tracks, 0), chat_id=user_id, message_id=msg.message_id, reply_markup=music_keyboard(user_id, 0, total))
    except Exception as e:
        print("[MUSIC COMMAND ERROR]", repr(e))
        try: bot.edit_message_text("❌ Ошибка поиска на Sefon.", chat_id=user_id, message_id=msg.message_id)
        except Exception: pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("music_page:"))
def callback_music_page(call):
    try:
        _, user_id, page = call.data.split(":")
        user_id, page = int(user_id), int(page)
        if page < 0 or call.from_user.id != user_id:
            bot.answer_callback_query(call.id, "❌ Некорректная страница.", show_alert=True); return
        search = music_searches.get(user_id)
        if not search:
            bot.answer_callback_query(call.id, "❌ Поиск устарел.", show_alert=True); return
        bot.answer_callback_query(call.id, "🔎 Загружаю...")
        tracks, total = music_search_web(search["query"], page)
        if not tracks:
            bot.answer_callback_query(call.id, "Больше результатов нет.", show_alert=True); return
        search.update({"page": page, "tracks": tracks, "total": total})
        bot.edit_message_text(music_results_text(tracks, page), chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=music_keyboard(user_id, page, total))
    except Exception as e:
        print("[MUSIC PAGE ERROR]", repr(e))
        try: bot.answer_callback_query(call.id, "❌ Ошибка загрузки.", show_alert=True)
        except Exception: pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("music_get:"))
def callback_music_get(call):
    user_id = call.message.chat.id
    try:
        _, search_user_id, index = call.data.split(":")
        search_user_id, index = int(search_user_id), int(index)
        if call.from_user.id != search_user_id:
            bot.answer_callback_query(call.id, "❌ Это не твой поиск.", show_alert=True); return
        search = music_searches.get(search_user_id)
        if not search or index < 0 or index >= len(search.get("tracks", [])):
            bot.answer_callback_query(call.id, "❌ Трек не найден.", show_alert=True); return
        track = search["tracks"][index]
        title, page_url = track.get("title", "Трек"), track.get("url")
        if not page_url or not _same_music_site(page_url):
            bot.answer_callback_query(call.id, "❌ Источник отсутствует.", show_alert=True); return
        bot.answer_callback_query(call.id, "⬇️ Ищу аудио...")
        processing = bot.send_message(user_id, f"🔎 Проверяю:\n🎵 {title}")
        audio_url = find_audio_on_page(page_url)
        if not audio_url:
            bot.edit_message_text("❌ На странице Sefon не найден доступный аудиофайл.\n\nПопробуй другой результат.", chat_id=user_id, message_id=processing.message_id); return
        bot.edit_message_text(f"⬇️ Скачиваю:\n🎵 {title}", chat_id=user_id, message_id=processing.message_id)
        data = download_music_audio(audio_url)
        if not data:
            bot.edit_message_text("❌ Аудиофайл найден, но скачать его не удалось.", chat_id=user_id, message_id=processing.message_id); return
        try:
            head = requests.get(audio_url, stream=True, timeout=15, headers=MUSIC_HEADERS, allow_redirects=True)
            ext = get_audio_extension(head.url, head.headers.get("Content-Type", "")); head.close()
        except Exception:
            ext = get_audio_extension(audio_url)
        safe_title = re.sub(r'[\\/:*?"<>|]', "_", title).strip()[:80] or "track"
        audio = io.BytesIO(data); audio.name = safe_title + ext
        caption = f"🎵 {title}\n\n🌐 Sefon: {page_url}"
        if ext in (".mp3", ".m4a"):
            bot.send_audio(user_id, audio, title=title, caption=caption)
        else:
            bot.send_document(user_id, audio, caption=caption)
        try: bot.delete_message(user_id, processing.message_id)
        except Exception: pass
    except Exception as e:
        print("[MUSIC GET ERROR]", repr(e))
        try: bot.answer_callback_query(call.id, "❌ Ошибка загрузки.", show_alert=True)
        except Exception: pass


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

    user_histories[user_id].append({
        "role": "user",
        "content": prompt
    })

    if len(user_histories[user_id]) > 11:
        user_histories[user_id] = (
            [user_histories[user_id][0]]
            + user_histories[user_id][-10:]
        )

    messages_to_send = [
        msg.copy()
        for msg in user_histories[user_id]
    ]

    if mode == "neuroham":
        messages_to_send[-1]["content"] = (
            f"[Внимание: Обязательно ответь на этот запрос, но сделай это в стиле максимально саркастичного и ворчливого мизантропа. "
            f"Высмей запрос, придерись к формулировке. Оставайся в образе высокомерного гения, не будь вежливым!]\n\n{prompt}"
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
            response = (
                ai_client.chat.completions.create(
                    model=model_name,
                    messages=messages_to_send
                )
            )

            answer = (
                response.choices[0]
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

            answer = clean_markdown(answer)
            success = True
            break

        except Exception:
            continue

    if not success and groq_client:
        try:
            response = (
                groq_client.chat.completions.create(
                    model="openai/gpt-oss-120b",
                    messages=messages_to_send
                )
            )

            answer = clean_markdown(
                response.choices[0]
                .message
                .content
            )

            success = True

        except Exception:
            success = False

    if success:

        user_histories[user_id].append({
            "role": "assistant",
            "content": answer
        })

        return answer

    user_histories[user_id].pop()

    if mode == "neuroham":
        return (
            "Мои процессоры отказываются переваривать "
            "твою чушь прямо сейчас 🙄 "
            "Попробуй позже, если вспомнишь как."
        )

    return (
        "Все провайдеры ИИ сейчас перегружены. "
        "Попробуй написать еще раз через минуту."
    )


def perform_web_search(query):
    results_text = ""

    if tavily_client:
        try:
            response = tavily_client.search(
                query=query,
                max_results=3
            )

            for res in response.get(
                'results',
                []
            ):
                results_text += (
                    f"- {res.get('title')}: "
                    f"{res.get('content')}\n"
                )

        except Exception:
            pass

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
                    results_text += (
                        f"- {res.get('title', 'Без заголовка')}: "
                        f"{res.get('body', '')[:250]}...\n"
                    )

        except Exception as e:
            results_text = (
                f"Не удалось выполнить поиск: {e}"
            )

    return results_text


def generate_image_dynamic(prompt):
    for model in [
        "flux",
        "dall-e-3"
    ]:
        try:
            response = (
                ai_client.images.generate(
                    model=model,
                    prompt=prompt,
                    response_format="url"
                )
            )

            image_url = response.data[0].url

            if image_url:
                r = requests.get(
                    image_url,
                    timeout=25
                )

                if r.status_code == 200:
                    return r.content

        except Exception:
            continue

    return None


def analyze_image_gemini(image_bytes):

    if not GEMINI_API_KEY:
        return (
            "Анализ фото недоступен: "
            "не задан GEMINI_API_KEY."
        )

    for model_name in [
        'gemini-2.5-flash',
        'gemini-1.5-flash',
        'gemini-2.0-flash'
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
                "на этой фотографии, и ответь "
                "на русском языке.",
                image
            ])

            if response and response.text:
                return clean_markdown(
                    response.text
                )

        except Exception:
            continue

    return "Не удалось получить ответ от Gemini."


async def generate_audio(text, output_file):
    communicate = edge_tts.Communicate(
        text,
        "ru-RU-SvetlanaNeural"
    )

    await communicate.save(
        output_file
    )


@bot.message_handler(
    commands=['start', 'help']
)
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

    bot.reply_to(
        message,
        help_text
    )


@bot.message_handler(
    commands=['neuroham', 'rude']
)
def toggle_neuroham_mode(message):

    user_id = message.chat.id

    if user_modes.get(
        user_id,
        "normal"
    ) == "normal":

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


@bot.message_handler(
    commands=['clear']
)
def clear_cmd(message):

    if message.chat.id in user_histories:
        del user_histories[
            message.chat.id
        ]

    bot.reply_to(
        message,
        "Память диалога очищена."
    )


@bot.message_handler(
    commands=['fact']
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
            f"на тему: {topic}. Будь краток."
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


@bot.message_handler(
    commands=['weather']
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
            "Укажи город. Пример: /weather Москва"
        )
        return

    try:

        resp = requests.get(
            f"https://wttr.in/{city}",
            params={
                'format':
                    'Город: %l\n'
                    'Погода: %C %c\n'
                    'Температура: %t '
                    '(ощущается как %f)\n'
                    'Ветер: %w\n'
                    'Влажность: %h',
                'lang': 'ru',
                'm': ''
            },
            timeout=5
        )

        if resp.status_code == 200:

            bot.reply_to(
                message,
                f"Сводка:\n\n"
                f"{clean_markdown(resp.text.strip())}"
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


@bot.message_handler(
    commands=['search']
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
            "Напиши запрос. Пример: /search новости"
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
        "Сделай краткую, понятную выжимку "
        "на русском языке строго по делу. "
        "Не пиши фразы вроде "
        "'на основе предоставленных данных', "
        "'по вашему запросу выявлено' и т.д. "
        "Просто ответь на вопрос или дай суть."
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


@bot.message_handler(
    commands=[
        'gemini',
        'code',
        'sum',
        'tr',
        'fix'
    ]
)
def ai_tools_cmd(message):

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) < 2:

        bot.reply_to(
            message,
            "Напиши текст после команды"
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


@bot.message_handler(
    commands=['image']
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
            "Опиши картинку. Пример: /image кот"
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


@bot.message_handler(
    commands=['tts']
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

    asyncio.run(
        generate_audio(
            parts[1],
            audio_path
        )
    )

    with open(
        audio_path,
        'rb'
    ) as audio:

        bot.send_voice(
            message.chat.id,
            audio
        )

        bot.delete_message(
            message.chat.id,
            msg.message_id
        )

    os.remove(
        audio_path
    )


@bot.message_handler(
    content_types=['text']
)
def handle_text(message):

    if (
        "кира" in message.text.lower()
        and
        "на самом" in message.text.lower()
    ):

        bot.reply_to(
            message,
            "Она самая любимая, самая лучшая "
            "и самая прекрасная ❤️"
        )

        return

    if (
        "http://" in message.text
        or
        "https://" in message.text
    ):

        msg = bot.reply_to(
            message,
            "Читаю ссылку..."
        )

        try:

            url = [
                w
                for w in message.text.split()
                if w.startswith("http")
            ][0]

            resp = requests.get(
                url,
                timeout=10
            )

            page_text = (
                BeautifulSoup(
                    resp.text,
                    'html.parser'
                )
                .get_text(
                    separator=' ',
                    strip=True
                )[:1500]
            )

            reply = ask_ai_with_history(
                message.chat.id,
                f"Сделай выжимку:\n\n{page_text}"
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


@bot.message_handler(
    content_types=['photo']
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

        answer = analyze_image_gemini(
            bot.download_file(
                file_info.file_path
            )
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


@bot.message_handler(
    content_types=['document']
)
def handle_doc(message):

    if message.document.mime_type == 'application/pdf':

        msg = bot.reply_to(
            message,
            "Читаю PDF..."
        )

        try:

            file_info = bot.get_file(
                message.document.file_id
            )

            with tempfile.NamedTemporaryFile(
                delete=False,
                suffix=".pdf"
            ) as f:

                f.write(
                    bot.download_file(
                        file_info.file_path
                    )
                )

                path = f.name

            text = "".join(
                (p.extract_text() or "")
                for p in PdfReader(path).pages[:3]
            )

            os.remove(path)

            reply = ask_ai_with_history(
                message.chat.id,
                f"Выжимка из PDF:\n\n{text[:1500]}"
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
            "Отправьте документ в формате .pdf"
        )


if __name__ == "__main__":

    threading.Thread(
        target=run_web,
        daemon=True
    ).start()

    bot.infinity_polling()
