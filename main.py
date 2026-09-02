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
# УМНЫЙ ПОИСК МУЗЫКИ MP3PARTY
# Понимает русский, английский, перевод,
# транслитерацию и запись английских слов "на слух"
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

# ============================================================
# СЛОВАРЬ "НА СЛУХ"
# ============================================================

SOUNDALIKE_WORDS = {
    "невер": "never", "нэвер": "never", "невэр": "never",
    "гонна": "gonna", "ганна": "gonna",
    "гив": "give", "гивв": "give",
    "ю": "you", "йу": "you", "юу": "you",
    "юр": "your", "ёр": "your",
    "юрсэлф": "yourself", "ёрселф": "yourself", "йорселф": "yourself", "юрселф": "yourself",
    "селф": "self",
    "ай": "i", "айм": "im",
    "билив": "believe", "белив": "believe", "бэлив": "believe", "билиф": "believe",
    "ин": "in",
    "эн": "and", "энд": "and",
    "зэт": "that", "зет": "that",
    "уот": "what", "вот": "what",
    "уонт": "want", "вонт": "want",
    "ит": "it",
    "из": "is",
    "ту": "to", "тy": "to",
    "тэ": "the", "те": "the", "зе": "the", "ди": "the",
    "фор": "for", "фо": "for",
    "ме": "me", "ми": "me",
    "май": "my",
    "вай": "why",
    "хау": "how",
    "хай": "hi",
    "хеллоу": "hello", "хелоу": "hello",
    "лайк": "like",
    "лав": "love", "лов": "love",
    "кан": "can", "кэн": "can",
    "коз": "cause", "кос": "cause",
    "вилл": "will", "уилл": "will",
    "ноу": "know", "но": "no",
    "би": "be", "бэ": "be",
    "эй": "hey",
    "вэри": "very", "вери": "very",
    "хёрт": "hurt",
    "харт": "heart", "хэарт": "heart",
    "броукен": "broken",
    "брэйк": "break", "брейк": "break",
    "дрим": "dream", "дримс": "dreams",
    "скай": "sky",
    "файр": "fire", "фая": "fire",
    "рейн": "rain", "рэйн": "rain",
    "найт": "night",
    "лайт": "light",
    "дарк": "dark",
    "уэй": "way", "вей": "way",
    "тейк": "take",
    "мэйк": "make", "мейк": "make",
    "кам": "come", "ком": "come",
    "гоу": "go",
    "оу": "oh",
    "бэби": "baby", "бейби": "baby",
    "гирл": "girl",
    "бой": "boy", "бойс": "boys",
    "бэд": "bad",
    "гуд": "good", "год": "good",
    "хэппи": "happy", "хэпи": "happy",
    "донт": "dont", "доунт": "dont",
    "доу": "do", "ду": "do",
    "йес": "yes", "ес": "yes",
    "мэй": "may", "мэйби": "maybe",
    "бэк": "back",
    "бэст": "best",
    "биг": "big",
    "литл": "little",
    "лонг": "long",
    "тру": "true",
    "фри": "free",
    "френд": "friend", "френдс": "friends",
    "вумен": "woman", "вумэн": "woman",
    "мэн": "man", "мэнс": "mans",
    "мьюзик": "music",
    "сонг": "song",
    "синг": "sing", "сингин": "singing",
    "фил": "feel", "филинг": "feeling", "филс": "feels",
    "мисс": "miss", "миссин": "missing",
    "стэй": "stay", "стэйт": "state",
    "стори": "story",
    "труф": "truth",
    "фолоу": "follow", "фоллоу": "follow",
    "лост": "lost",
    "фоллен": "fallen",
    "хоуп": "hope", "хоупс": "hopes",
    "хевен": "heaven", "хэвэн": "heaven",
    "хелл": "hell",
    "плей": "play", "плейс": "place",
    "тайм": "time", "таймс": "times",
    "дэй": "day", "дэйс": "days",
    "тумороу": "tomorrow", "томороу": "tomorrow",
    "тудэй": "today",
    "йестердей": "yesterday",
}

# ============================================================
# ТРАНСЛИТЕРАЦИЯ
# ============================================================

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


# ============================================================
# УМНЫЙ ПОИСК "НА СЛУХ"
# ============================================================

def music_soundalike_transliterate(value):
    normalized = music_normalize(value)
    if not normalized:
        return []

    tokens = normalized.split()
    mapped = []

    for token in tokens:
        if token in SOUNDALIKE_WORDS:
            mapped.append(SOUNDALIKE_WORDS[token])
        else:
            mapped.append(token)

    results = []
    full_result = " ".join(mapped)
    if full_result:
        results.append(full_result)

    for i, token in enumerate(tokens):
        replacement = SOUNDALIKE_WORDS.get(token)
        if replacement:
            partial = tokens[:]
            partial[i] = replacement
            result = " ".join(partial)
            if result not in results:
                results.append(result)

    transliterated = music_transliterate(value)
    if transliterated:
        results.append(transliterated)

    unique = []
    for result in results:
        result = music_normalize(result)
        if result and result not in unique:
            unique.append(result)

    return unique


# ============================================================
# ПЕРЕВОД ЗАПРОСА НА АНГЛИЙСКИЙ
# ============================================================

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
        translated = ""
        if data and data[0]:
            for part in data[0]:
                if part and part[0]:
                    translated += part[0]
        return translated.strip()
    except Exception as exc:
        print("[MUSIC TRANSLATE ERROR]", repr(exc))
        return ""


# ============================================================
# ВСЕ ВАРИАНТЫ ЗАПРОСА
# ============================================================

def music_query_variants(query):
    query = (query or "").strip()
    if not query:
        return []

    variants = []
    def add(value):
        value = str(value or "").strip()
        if value and value not in variants:
            variants.append(value)

    add(query)
    add(music_normalize(query))
    add(re.sub(r"[^\w\s-]", " ", query, flags=re.UNICODE))
    translated = music_translate_query(query)
    if translated:
        add(translated)
    transliterated = music_transliterate(query)
    if transliterated:
        add(transliterated)
    for result in music_soundalike_transliterate(query):
        add(result)

    return variants[:12]


# ============================================================
# ПРОВЕРКА MP3PARTY
# ============================================================

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


# ============================================================
# ПАРСИНГ MP3PARTY
# ============================================================

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
        
        title = a.get_text(" ", strip=True)
        if not title:
            title = (a.get("title") or a.get("aria-label") or "").strip()
        if not title:
            continue
            
        normalized = music_normalize(title)
        if not normalized or normalized in {"слушать", "скачать", "онлайн"}:
            continue

        seen.add(href)
        results.append({"title": title, "url": href})
        
        if len(results) >= MUSIC_MAX_RESULTS * 4:
            break

    return results


# ============================================================
# ПОИСК НА MP3PARTY
# ============================================================

def _mp3party_search_page(query):
    encoded = quote_plus(query)
    url = f"{MUSIC_SEARCH_URL}?q={encoded}"

    try:
        response = requests.get(
            url,
            timeout=20,
            headers=MUSIC_HEADERS,
            allow_redirects=True,
        )
        response.raise_for_status()

        if not _same_music_site(response.url):
            return []

        return _parse_mp3party_results(response.text, response.url)
    except Exception as exc:
        print("[MUSIC SEARCH ERROR]", repr(exc))
        return []


# ============================================================
# FALLBACK MP3PARTY
# ============================================================

def _mp3party_fallback_from_home(query):
    pages = [
        MUSIC_SITE + "/",
        MUSIC_SITE + "/popular/",
        MUSIC_SITE + "/new/",
    ]
    collected = []
    seen = set()

    for page_url in pages:
        try:
            response = requests.get(
                page_url,
                timeout=15,
                headers=MUSIC_HEADERS,
                allow_redirects=True,
            )
            response.raise_for_status()

            if not _same_music_site(response.url):
                continue

            items = _parse_mp3party_results(response.text, response.url)
            for item in items:
                if item["url"] in seen:
                    continue
                seen.add(item["url"])
                collected.append(item)
        except Exception as exc:
            print("[MUSIC FALLBACK ERROR]", repr(exc))

    return collected


# ============================================================
# СРАВНЕНИЕ
# ============================================================

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
    token_scores = []

    for qt in q_tokens:
        best = max(SequenceMatcher(None, qt, ct).ratio() for ct in c_tokens)
        token_scores.append(best)

    token_score = sum(token_scores) / len(token_scores) if token_scores else 0.0
    overlap = len(set(q_tokens) & set(c_tokens)) / max(1, len(set(q_tokens)))

    score = base * 0.35 + token_score * 0.45 + overlap * 0.20
    return min(1.0, score)


def music_best_similarity(query, title):
    variants = music_query_variants(query)
    title_variants = [title]
    
    simplified = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", title)
    title_variants.append(simplified)

    best_score = 0.0
    best_variant = ""

    for variant in variants:
        for candidate in title_variants:
            score = music_similarity(variant, candidate)
            if score > best_score:
                best_score = score
                best_variant = variant

    return best_score, best_variant


# ============================================================
# ГЛАВНЫЙ ПОИСК (ИСПРАВЛЕННЫЙ ПОРОГ)
# ============================================================

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
        items = _mp3party_search_page(variant)
        for item in items:
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            candidates.append(item)

    if not candidates:
        fallback = _mp3party_fallback_from_home(query)
        for item in fallback:
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            candidates.append(item)

    scored = []
    for item in candidates:
        title = item.get("title", "")
        score, matched_variant = music_best_similarity(query, title)
        
        # Смягченный порог для смешанных запросов
        if score < 0.15:
            continue

        scored.append({
            "title": title,
            "url": item["url"],
            "score": score,
            "matched_variant": matched_variant,
        })

    scored.sort(
        key=lambda item: (item["score"], len(music_normalize(item["title"]))),
        reverse=True
    )

    unique = []
    used_titles = set()

    for item in scored:
        key = music_normalize(item["title"])
        if key in used_titles:
            continue
        used_titles.add(key)
        unique.append(item)
        if len(unique) >= MUSIC_MAX_RESULTS:
            break

    total = len(unique)
    start_index = page * MUSIC_PER_PAGE
    end_index = start_index + MUSIC_PER_PAGE

    return unique[start_index:end_index], total


# ============================================================
# ТЕКСТ РЕЗУЛЬТАТОВ
# ============================================================

def music_results_text(tracks, page):
    text = f"🎵 Результаты поиска на MP3Party\nСтраница {page + 1}\n\n"
    for index, track in enumerate(tracks, 1):
        title = track.get("title", "Без названия")
        score = int(round(track.get("score", 0) * 100))
        text += f"{index}. {title} • {score}%\n"
    text += "\n👇 Выбери подходящий трек:"
    return text


# ============================================================
# КЛАВИАТУРА
# ============================================================

def music_keyboard(user_id, page, total):
    keyboard = InlineKeyboardMarkup(row_width=1)
    search = music_searches.get(user_id)
    if not search:
        return keyboard

    for index, track in enumerate(search.get("tracks", [])):
        title = track.get("title", "Без названия")
        score = int(round(track.get("score", 0) * 100))
        label = f"{index + 1}. {title} ({score}%)"
        keyboard.add(InlineKeyboardButton(label[:50], callback_data=f"music_get:{user_id}:{index}"))

    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("◀️", callback_data=f"music_page:{user_id}:{page - 1}"))
    if total > (page + 1) * MUSIC_PER_PAGE:
        navigation.append(InlineKeyboardButton("▶️", callback_data=f"music_page:{user_id}:{page + 1}"))

    if navigation:
        keyboard.row(*navigation)
    return keyboard


# ============================================================
# /MUSIC
# ============================================================

@bot.message_handler(commands=["music"])
def music_cmd(message):
    user_id = message.chat.id
    parts = message.text.split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""

    if not query:
        bot.reply_to(
            message,
            "🎵 Напиши исполнителя, название или строчку из песни.\n\n"
            "Примеры:\n"
            "/music Tanin Jazz Виртуальная любовь\n"
            "/music never gonna give you up"
        )
        return

    msg = bot.reply_to(
        message,
        "🔎 Ищу на MP3Party..."
    )

    try:
        tracks, total = music_search_web(query, 0)
        if not tracks:
            bot.edit_message_text(
                "❌ На MP3Party ничего подходящего не найдено.\n\n"
                "Попробуй изменить поисковый запрос.",
                chat_id=user_id,
                message_id=msg.message_id
            )
            return

        music_searches[user_id] = {
            "query": query,
            "page": 0,
            "tracks": tracks,
            "total": total,
        }

        bot.edit_message_text(
            music_results_text(tracks, 0),
            chat_id=user_id,
            message_id=msg.message_id,
            reply_markup=music_keyboard(user_id, 0, total)
        )
    except Exception as exc:
        print("[MUSIC COMMAND ERROR]", repr(exc))
        try:
            bot.edit_message_text("❌ Произошла ошибка при поиске музыки.", chat_id=user_id, message_id=msg.message_id)
        except Exception:
            pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("music_page:"))
def callback_music_page(call):
    try:
        _, user_id, page = call.data.split(":")
        user_id = int(user_id)
        page = int(page)

        if page < 0:
            bot.answer_callback_query(call.id, "❌ Некорректная страница.", show_alert=True)
            return

        if call.from_user.id != user_id:
            bot.answer_callback_query(call.id, "❌ Это не ваш поиск.", show_alert=True)
            return

        search = music_searches.get(user_id)
        if not search:
            bot.answer_callback_query(call.id, "❌ Поиск устарел.", show_alert=True)
            return

        bot.answer_callback_query(call.id, "🔎 Загружаю...")
        tracks, total = music_search_web(search["query"], page)
        if not tracks:
            bot.answer_callback_query(call.id, "Больше результатов нет.", show_alert=True)
            return

        search["page"] = page
        search["tracks"] = tracks
        search["total"] = total

        bot.edit_message_text(
            music_results_text(tracks, page),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=music_keyboard(user_id, page, total)
        )
    except Exception as exc:
        print("[MUSIC PAGE ERROR]", repr(exc))
        try:
            bot.answer_callback_query(call.id, "❌ Ошибка.", show_alert=True)
        except Exception:
            pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("music_get:"))
def callback_music_get(call):
    try:
        _, user_id, index = call.data.split(":")
        user_id = int(user_id)
        index = int(index)

        if call.from_user.id != user_id:
            bot.answer_callback_query(call.id, "❌ Этот результат принадлежит другому пользователю.", show_alert=True)
            return

        search = music_searches.get(user_id)
        if not search:
            bot.answer_callback_query(call.id, "❌ Поиск устарел.", show_alert=True)
            return

        tracks = search.get("tracks", [])
        if index < 0 or index >= len(tracks):
            bot.answer_callback_query(call.id, "❌ Трек не найден.", show_alert=True)
            return

        track = tracks[index]
        url = track.get("url", "")

        if not _same_music_site(url) or not _mp3party_music_link(url):
            bot.answer_callback_query(call.id, "❌ Некорректная ссылка.", show_alert=True)
            return

        bot.answer_callback_query(call.id, "🎵 Открываю...")
        bot.send_message(
            call.message.chat.id,
            f"🎵 {track.get('title', 'Трек')}\n\nОткрыть страницу трека:\n{url}",
            disable_web_page_preview=False
        )
    except Exception as exc:
        print("[MUSIC GET ERROR]", repr(exc))
        try:
            bot.answer_callback_query(call.id, "❌ Ошибка.", show_alert=True)
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
        user_histories[user_id] = [{"role": "system", "content": sys_prompt}]

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
            response = ai_client.chat.completions.create(
                model=model_name,
                messages=messages_to_send
            )
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
            response = groq_client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=messages_to_send
            )
            answer = clean_markdown(response.choices[0].message.content)
            success = True
        except Exception:
            success = False

    if success:
        user_histories[user_id].append({"role": "assistant", "content": answer})
        return answer

    user_histories[user_id].pop()
    if mode == "neuroham":
        return (
            "Мои процессоры отказываются переваривать твою чушь прямо сейчас 🙄 "
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
            response = ai_client.images.generate(
                model=model,
                prompt=prompt,
                response_format="url"
            )
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
            response = model.generate_content([
                "Опиши подробно, что изображено на этой фотографии, и ответь на русском языке.",
                image
            ])
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

    if topic:
        prompt = f"Расскажи один интересный факт на тему: {topic}. Будь краток."
    else:
        prompt = "Расскажи один случайный интересный факт. Будь краток."

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
            params={
                'format': 'Город: %l\nПогода: %C %c\nТемпература: %t (ощущается как %f)\nВетер: %w\nВлажность: %h',
                'lang': 'ru',
                'm': ''
            },
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
        bot.reply_to(message, "Напиши текст после команды")
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
