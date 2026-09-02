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
# ПОИСК МУЗЫКИ
# ============================================================

from difflib import SequenceMatcher
from urllib.parse import urljoin, urlparse, quote_plus

MUSIC_SITE = "https://mp3party.net"
MUSIC_SEARCH_URL = f"{MUSIC_SITE}/search"

MUSIC_PER_PAGE = 8
MUSIC_MAX_RESULTS = 40

music_searches = {}

MUSIC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 15) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/140.0 Mobile Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

SOUNDALIKE_WORDS = {
    "невер": "never", "нэвер": "never", "невэр": "never",
    "гонна": "gonna", "ганна": "gonna",
    "гив": "give", "гивв": "give",
    "ю": "you", "йу": "you", "юу": "you",
    "юр": "your", "ёр": "your",
    "ай": "i", "айм": "im",
    "билив": "believe", "белив": "believe", "бэлив": "believe",
    "ин": "in", "эн": "and", "энд": "and",
    "уот": "what", "вот": "what", "уонт": "want", "вонт": "want",
    "ит": "it", "из": "is", "ту": "to", "фор": "for", "ме": "me", "май": "my",
    "лайк": "like", "лав": "love", "лов": "love", "кан": "can", "кэн": "can",
    "вилл": "will", "уилл": "will", "ноу": "know", "би": "be",
    "хэарт": "heart", "харт": "heart", "драйв": "drive",
}

CYR_TO_LAT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", 
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", 
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", 
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", 
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})

def music_normalize(value):
    value = str(value or "").lower()
    value = value.replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9]+", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value)
    return value.strip()

def music_transliterate(query):
    return music_normalize(str(query or "").lower().translate(CYR_TO_LAT))

def music_soundalike_transliterate(value):
    normalized = music_normalize(value)
    if not normalized:
        return []
    tokens = normalized.split()
    mapped = [SOUNDALIKE_WORDS.get(t, t) for t in tokens]
    results = [" ".join(mapped)]
    for i, token in enumerate(tokens):
        if token in SOUNDALIKE_WORDS:
            partial = tokens[:]
            partial[i] = SOUNDALIKE_WORDS[token]
            res = " ".join(partial)
            if res not in results:
                results.append(res)
    trans = music_transliterate(value)
    if trans:
        results.append(trans)
    return [music_normalize(r) for r in results if music_normalize(r)]

def music_translate_query(query):
    try:
        response = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "en", "dt": "t", "q": query},
            timeout=8,
        )
        if not response.ok:
            return ""
        data = response.json()
        translated = "".join(part[0] for part in data[0] if part and part[0])
        return translated.strip()
    except Exception:
        return ""

def music_query_variants(query):
    query = (query or "").strip()
    if not query:
        return []
    variants = []
    def add(v):
        v = str(v or "").strip()
        if v and v not in variants:
            variants.append(v)
    add(query)
    add(music_normalize(query))
    add(re.sub(r"[^\w\s-]", " ", query, flags=re.UNICODE))
    tr = music_translate_query(query)
    if tr:
        add(tr)
    trans = music_transliterate(query)
    if trans:
        add(trans)
    for res in music_soundalike_transliterate(query):
        add(res)
    return variants[:12]

def _same_music_site(url):
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
        return host in {"mp3party.net", "www.mp3party.net"}
    except Exception:
        return False

def _mp3party_music_link(url):
    if not _same_music_site(url):
        return False
    try:
        path = urlparse(url).path.lower().rstrip("/")
        return path.startswith("/music/") and path != "/music"
    except Exception:
        return False

def _parse_mp3party_results(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a.get("href", ""))
        if not _mp3party_music_link(href):
            continue
        if href in seen:
            continue
        title = a.get_text(" ", strip=True) or (a.get("title") or "").strip()
        if not title:
            continue
        normalized = music_normalize(title)
        if not normalized or normalized in {"слушать", "скачать", "онлайн"}:
            continue
        seen.add(href)
        results.append({"title": title, "url": href})
    return results

def _mp3party_search_page(query):
    try:
        response = requests.get(
            f"{MUSIC_SEARCH_URL}?q={quote_plus(query)}",
            timeout=20,
            headers=MUSIC_HEADERS,
            allow_redirects=True,
        )
        response.raise_for_status()
        if not _same_music_site(response.url):
            return []
        return _parse_mp3party_results(response.text, response.url)
    except Exception:
        return []

def _mp3party_fallback_from_home(query):
    collected = []
    seen = set()
    for page_url in [MUSIC_SITE + "/", MUSIC_SITE + "/popular/", MUSIC_SITE + "/new/"]:
        try:
            response = requests.get(page_url, timeout=15, headers=MUSIC_HEADERS, allow_redirects=True)
            if not response.ok:
                continue
            for item in _parse_mp3party_results(response.text, response.url):
                if item["url"] not in seen:
                    seen.add(item["url"])
                    collected.append(item)
        except Exception:
            pass
    return collected

def music_similarity(query, candidate):
    q = music_normalize(query)
    c = music_normalize(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if q in c:
        return 0.96
    q_tokens = q.split()
    c_tokens = c.split()
    base = SequenceMatcher(None, q, c).ratio()
    token_scores = [max(SequenceMatcher(None, qt, ct).ratio() for ct in c_tokens) for qt in q_tokens]
    token_score = sum(token_scores) / len(token_scores) if token_scores else 0.0
    overlap = len(set(q_tokens) & set(c_tokens)) / max(1, len(set(q_tokens)))
    return min(1.0, base * 0.35 + token_score * 0.45 + overlap * 0.20)

def music_best_similarity(query, title):
    variants = music_query_variants(query)
    title_variants = [title, re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", title)]
    best_score = 0.0
    best_variant = ""
    for variant in variants:
        for candidate in title_variants:
            score = music_similarity(variant, candidate)
            if score > best_score:
                best_score = score
                best_variant = variant
    return best_score, best_variant

def music_search_web(query, page=0):
    query = (query or "").strip()
    if not query:
        return [], 0
    variants = music_query_variants(query)
    if not variants:
        return [], 0

    candidates = []
    seen = set()
    for variant in variants:
        for item in _mp3party_search_page(variant):
            if item["url"] not in seen:
                seen.add(item["url"])
                candidates.append(item)

    if not candidates:
        for item in _mp3party_fallback_from_home(query):
            if item["url"] not in seen:
                seen.add(item["url"])
                candidates.append(item)

    scored = []
    for item in candidates:
        score, matched = music_best_similarity(query, item["title"])
        if score < 0.15:
            continue
        scored.append({"title": item["title"], "url": item["url"], "score": score, "matched_variant": matched})

    scored.sort(key=lambda x: (x["score"], len(music_normalize(x["title"]))), reverse=True)
    
    unique = []
    used = set()
    for item in scored:
        key = music_normalize(item["title"])
        if key not in used:
            used.add(key)
            unique.append(item)

    start = page * MUSIC_PER_PAGE
    return unique[start:start + MUSIC_PER_PAGE], len(unique)

def music_results_text(tracks, page):
    text = f"🎵 Найденные треки\nСтраница {page + 1}\n\n"
    for idx, track in enumerate(tracks, 1):
        score = int(round(track.get("score", 0) * 100))
        text += f"{idx}. {track.get('title')} • {score}%\n"
    text += "\n👇 Выбери подходящий трек для скачивания:"
    return text

def music_keyboard(user_id, page, total):
    keyboard = InlineKeyboardMarkup(row_width=1)
    search = music_searches.get(user_id)
    if not search:
        return keyboard

    for idx, track in enumerate(search.get("tracks", [])):
        score = int(round(track.get("score", 0) * 100))
        label = f"{idx + 1}. {track.get('title')} ({score}%)"
        keyboard.add(InlineKeyboardButton(label[:50], callback_data=f"music_get:{user_id}:{idx}"))

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"music_page:{user_id}:{page - 1}"))
    if total > (page + 1) * MUSIC_PER_PAGE:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"music_page:{user_id}:{page + 1}"))
    if nav:
        keyboard.row(*nav)
    return keyboard

def _get_direct_mp3_link(track_url):
    try:
        resp = requests.get(track_url, headers=MUSIC_HEADERS, timeout=10)
        if not resp.ok:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = urljoin(track_url, a.get("href", ""))
            if "download" in href or href.endswith(".mp3"):
                return href
    except Exception:
        pass
    return None

@bot.message_handler(commands=["music"])
def music_cmd(message):
    user_id = message.chat.id
    parts = message.text.split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""

    if not query:
        bot.reply_to(
            message,
            "🎵 Чтобы найти трек, напиши его название и исполнителя после команды.\n\n"
            "Укажи имя артиста и трек через пробел."
        )
        return

    msg = bot.reply_to(message, "🔎 Ищу трек...")
    try:
        tracks, total = music_search_web(query, 0)
        if not tracks:
            bot.edit_message_text(
                "❌ Ничего не найдено. Попробуй изменить поисковый запрос.",
                chat_id=user_id,
                message_id=msg.message_id
            )
            return

        music_searches[user_id] = {"query": query, "page": 0, "tracks": tracks, "total": total}
        bot.edit_message_text(
            music_results_text(tracks, 0),
            chat_id=user_id,
            message_id=msg.message_id,
            reply_markup=music_keyboard(user_id, 0, total)
        )
    except Exception:
        bot.edit_message_text("❌ Произошла ошибка при поиске.", chat_id=user_id, message_id=msg.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("music_page:"))
def callback_music_page(call):
    try:
        _, user_id, page = call.data.split(":")
        user_id, page = int(user_id), int(page)
        if call.from_user.id != user_id:
            bot.answer_callback_query(call.id, "❌ Это не ваш поиск.", show_alert=True)
            return
        search = music_searches.get(user_id)
        if not search:
            bot.answer_callback_query(call.id, "❌ Поиск устарел.", show_alert=True)
            return
        tracks, total = music_search_web(search["query"], page)
        search.update({"page": page, "tracks": tracks, "total": total})
        bot.edit_message_text(music_results_text(tracks, page), chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=music_keyboard(user_id, page, total))
    except Exception:
        pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("music_get:"))
def callback_music_get(call):
    try:
        _, user_id, index = call.data.split(":")
        user_id, index = int(user_id), int(index)
        if call.from_user.id != user_id:
            bot.answer_callback_query(call.id, "❌ Чужой результат.", show_alert=True)
            return
        search = music_searches.get(user_id)
        if not search or index >= len(search.get("tracks", [])):
            bot.answer_callback_query(call.id, "❌ Трек не найден.", show_alert=True)
            return

        track = search["tracks"][index]
        bot.answer_callback_query(call.id, "📥 Скачиваю трек...")

        direct_link = _get_direct_mp3_link(track["url"])
        if not direct_link:
            bot.send_message(call.message.chat.id, f"🎵 Не удалось скачать файл напрямую. Ссылка на трек:\n{track['url']}")
            return

        file_resp = requests.get(direct_link, headers=MUSIC_HEADERS, stream=True, timeout=25)
        if not file_resp.ok:
            bot.send_message(call.message.chat.id, "❌ Ошибка загрузки файла.")
            return

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            for chunk in file_resp.iter_content(chunk_size=8192):
                if chunk:
                    tmp.write(chunk)
            tmp_path = tmp.name

        with open(tmp_path, "rb") as audio:
            bot.send_audio(call.message.chat.id, audio, title=track["title"][:64], caption=f"🎵 {track['title']}")
        os.remove(tmp_path)
    except Exception:
        bot.answer_callback_query(call.id, "❌ Ошибка скачивания.", show_alert=True)


# ============================================================
# ИИ И ДОП. ФУНКЦИОНАЛ
# ============================================================

def ask_ai_with_history(user_id, prompt):
    mode = user_modes.get(user_id, "normal")
    if user_id not in user_histories:
        sys_prompt = "Ты полезный ассистент. Не используй Markdown (*, _, #)." if mode != "neuroham" else "Ты ворчливый саркастичный бот. Без Markdown."
        user_histories[user_id] = [{"role": "system", "content": sys_prompt}]
    user_histories[user_id].append({"role": "user", "content": prompt})
    if len(user_histories[user_id]) > 11:
        user_histories[user_id] = [user_histories[user_id][0]] + user_histories[user_id][-10:]
    try:
        response = ai_client.chat.completions.create(model="gpt-3.5-turbo", messages=user_histories[user_id])
        answer = clean_markdown(response.choices[0].message.content)
        user_histories[user_id].append({"role": "assistant", "content": answer})
        return answer
    except Exception:
        return "Временная ошибка ИИ. Попробуй еще раз."

def perform_web_search(query):
    try:
        with DDGS() as ddgs:
            return "".join(f"- {r.get('title')}: {r.get('body')}\n" for r in list(ddgs.text(query, max_results=3)))
    except Exception:
        return "Поиск недоступен."

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
        return "Анализ фото недоступен."
    for model_name in ['gemini-2.5-flash', 'gemini-1.5-flash', 'gemini-2.0-flash']:
        try:
            model = genai.GenerativeModel(model_name)
            image = Image.open(io.BytesIO(image_bytes))
            response = model.generate_content(["Опиши подробно, что изображено на фото, на русском языке.", image])
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
        "Привет! Я бот-ассистент.\n\n"
        "Команды:\n"
        "- /music <артист> - <трек> - поиск и скачивание музыки\n"
        "- /search <запрос> - поиск в интернете\n"
        "- /weather <город> - погода\n"
        "- /image <описание> - генерация картинки\n"
        "- /tts <текст> - озвучить текст\n"
        "- /fact - интересный факт\n"
        "- /clear - очистить память\n"
        "- /neuroham - режим Нейрохама 💀"
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
    msg = bot.reply_to(message, "Ищу факт...")
    fact = ask_ai_with_history(message.chat.id, "Расскажи один случайный интересный факт. Будь краток.")
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
            params={'format': 'Город: %l\nПогода: %C %c\nТемпература: %t\nВетер: %w', 'lang': 'ru', 'm': ''},
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
    reply = ask_ai_with_history(message.chat.id, f"Результаты поиска:\n{raw_data}\n\nСделай краткую выжимку.")
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
    # Пасхалка про Киру
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
            text = "".join((p.extract_text() or "") for p in PdfReader(path).pages[:3])
            os.remove(path)
            reply = ask_ai_with_history(message.chat.id, f"Выжимка из PDF:\n\n{text[:1500]}")
            bot.edit_message_text(reply, chat_id=message.chat.id, message_id=msg.message_id)
        except Exception as e:
            bot.edit_message_text(f"Ошибка PDF: {e}", chat_id=message.chat.id, message_id=msg.message_id)
    else:
        bot.reply_to(message, "Отправьте документ в формате .pdf")

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    bot.infinity_polling()
