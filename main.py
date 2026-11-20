import os
import re
import io
import ast
import csv
import html
import json
import math
import time
import base64
import secrets
import asyncio
import operator
import tempfile
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

import telebot
from telebot import types
import requests
from flask import Flask
import edge_tts
from g4f.client import Client
from g4f.Provider import IterListProvider
import g4f.models as g4f_models
from groq import Groq
from pypdf import PdfReader
import docx
import openpyxl
from pptx import Presentation
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

try:
    from PIL import Image, ImageOps
except ImportError:
    Image, ImageOps = None, None

# ════════════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ════════════════════════════════════════════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATA_DIR = os.getenv("DATA_DIR", ".")
TZ_OFFSET_HOURS = float(os.getenv("BOT_TZ_OFFSET", "5"))  # Ташкент = UTC+5

if not BOT_TOKEN:
    raise RuntimeError("❌ Не задан BOT_TOKEN")

bot = telebot.TeleBot(BOT_TOKEN)
ai_client = Client()

TELEGRAM_MESSAGE_LIMIT = 4096
AI_MAX_RESPONSE_LENGTH = 40000
INLINE_CODE_LIMIT = 3500  # код длиннее отправляется файлом

groq_client = Groq(api_key=GROQ_API_KEY, timeout=60.0, max_retries=1) if GROQ_API_KEY else None

try:
    BOT_USERNAME = bot.get_me().username.lower()
except Exception:
    BOT_USERNAME = ""

try:
    from tavily import TavilyClient
except ImportError:
    TavilyClient = None
tavily_client = TavilyClient(api_key=TAVILY_API_KEY) if TavilyClient and TAVILY_API_KEY else None

# Gemini: цепочка моделей. Если одна недоступна/не найдена — пробуем следующую.
genai, genai_types, gemini_client = None, None, None
GEMINI_MODELS = []
DEAD_GEMINI = set()
if GEMINI_API_KEY:
    try:
        from google import genai
        from google.genai import types as genai_types

        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        _custom = os.getenv("GEMINI_MODEL", "").strip()
        for _m in ([_custom] if _custom else []) + ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]:
            if _m and _m not in GEMINI_MODELS:
                GEMINI_MODELS.append(_m)
        print(f"✅ Gemini подключён: {GEMINI_MODELS}")
    except Exception as e:
        print(f"⚠️ Gemini недоступен: {e}")
        genai, genai_types, gemini_client = None, None, None

GROQ_VISION_MODELS = [m.strip() for m in os.getenv(
    "GROQ_VISION_MODELS", "meta-llama/llama-4-scout-17b-16e-instruct").split(",") if m.strip()]

user_histories = {}   # user_id -> [{"role","content"}, ...] (без system)
user_modes = {}
image_prompts = {}
stats = {"users": set(), "images_generated": 0, "files_generated": 0, "voice_messages": 0}

# ════════════════════════════════════════════════════════════════
#  G4F без Copilot
# ════════════════════════════════════════════════════════════════
G4F_BLOCKED_PROVIDER_NAMES = {"copilot", "copilotapp", "copilotsession", "copilotaccount"}


def get_g4f_non_copilot_provider():
    default_model = getattr(g4f_models, "default", None)
    best_provider = getattr(default_model, "best_provider", None)
    provider_names = getattr(best_provider, "providers", None)
    if not provider_names:
        raise RuntimeError("Не удалось получить список провайдеров G4F.")
    allowed = []
    for provider in provider_names:
        name = provider if isinstance(provider, str) else getattr(provider, "__name__", str(provider))
        norm = str(name).replace("_", "").replace("-", "").lower()
        if norm in G4F_BLOCKED_PROVIDER_NAMES or "copilot" in norm:
            continue
        allowed.append(provider)
    if not allowed:
        raise RuntimeError("В G4F не осталось провайдеров после исключения Copilot.")
    return IterListProvider(allowed)


# ════════════════════════════════════════════════════════════════
#  ЕДИНЫЙ ВЫЗОВ LLM (Groq → Gemini → G4F), всё бесплатное
# ════════════════════════════════════════════════════════════════
CHAINS = {
    "chat": [("groq", "openai/gpt-oss-120b"), ("groq", "openai/gpt-oss-20b"),
             ("groq", "llama-3.3-70b-versatile"), ("gemini", None), ("g4f", "auto")],
    "code": [("groq", "openai/gpt-oss-120b"), ("gemini", None), ("groq", "qwen/qwen3-32b"),
             ("groq", "llama-3.3-70b-versatile"), ("g4f", "auto")],
    "fast": [("groq", "openai/gpt-oss-20b"), ("gemini", None), ("g4f", "auto")],
}

TRANSIENT_MARKERS = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500", "INTERNAL",
                     "502", "504", "DEADLINE_EXCEEDED", "TIMEOUT", "TIMED OUT", "OVERLOADED")


def is_transient(err):
    s = str(err).upper()
    return any(m in s for m in TRANSIENT_MARKERS)


def strip_think(text):
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()


def gemini_call(contents, system=None, max_tokens=4096, temperature=0.7, tries=2, on_retry=None):
    """Вызов Gemini с перебором моделей и повторами при временных ошибках."""
    if not gemini_client:
        raise RuntimeError("Gemini не подключён")
    last_error = None
    for model in GEMINI_MODELS:
        if model in DEAD_GEMINI:
            continue
        for attempt in range(tries):
            try:
                cfg = genai_types.GenerateContentConfig(
                    system_instruction=system, max_output_tokens=max_tokens, temperature=temperature)
                resp = gemini_client.models.generate_content(model=model, contents=contents, config=cfg)
                text = (getattr(resp, "text", None) or "").strip()
                if not text:
                    last_error = RuntimeError(f"{model}: пустой ответ")
                    break
                finish = "stop"
                try:
                    if "MAX_TOKENS" in str(resp.candidates[0].finish_reason).upper():
                        finish = "length"
                except Exception:
                    pass
                return text, finish
            except Exception as e:
                last_error = e
                s = str(e).upper()
                if "404" in s or "NOT_FOUND" in s:
                    DEAD_GEMINI.add(model)
                    print(f"⚠️ Gemini модель недоступна, исключаю: {model}")
                    break
                if is_transient(e) and attempt < tries - 1:
                    delay = 2 * (attempt + 1)
                    print(f"⚠️ Gemini {model} временная ошибка, повтор через {delay}с: {e}")
                    if on_retry:
                        try:
                            on_retry(attempt + 2, tries)
                        except Exception:
                            pass
                    time.sleep(delay)
                    continue
                break
    raise last_error or RuntimeError("Gemini: нет доступных моделей")


def _groq_call(model, messages, max_tokens, timeout, temperature, effort):
    kwargs = dict(model=model, messages=messages, temperature=temperature,
                  max_tokens=max_tokens, timeout=timeout)
    if model.startswith("openai/gpt-oss"):
        kwargs["extra_body"] = {"reasoning_effort": effort}
    try:
        resp = groq_client.chat.completions.create(**kwargs)
    except Exception as e:
        if "extra_body" in kwargs and "reasoning" in str(e).lower():
            kwargs.pop("extra_body")
            resp = groq_client.chat.completions.create(**kwargs)
        else:
            raise
    choice = resp.choices[0]
    text = strip_think(choice.message.content or "")
    finish = "length" if str(choice.finish_reason) == "length" else "stop"
    return text, finish


def call_llm(messages, chain="chat", max_tokens=3000, timeout=45, temperature=0.6, effort="medium"):
    """messages: список с первым system-сообщением. Возвращает (text, finish_reason)."""
    errors = []
    system = messages[0]["content"] if messages and messages[0]["role"] == "system" else None
    convo = messages[1:] if system is not None else messages

    for provider, model in CHAINS[chain]:
        try:
            if provider == "groq":
                if not groq_client:
                    continue
                print(f"🔄 Groq → {model}")
                text, finish = _groq_call(model, messages, max_tokens, timeout, temperature, effort)
            elif provider == "gemini":
                if not gemini_client:
                    continue
                print("🔄 Gemini (текст)")
                contents = [
                    genai_types.Content(
                        role="model" if m["role"] == "assistant" else "user",
                        parts=[genai_types.Part.from_text(text=m["content"])])
                    for m in convo
                ]
                text, finish = gemini_call(contents, system=system, max_tokens=max(max_tokens, 4096),
                                           temperature=temperature)
            else:
                print("🔄 G4F → auto")
                resp = ai_client.chat.completions.create(
                    model="auto", messages=messages,
                    provider=get_g4f_non_copilot_provider(), timeout=min(timeout, 60))
                text = strip_think(resp.choices[0].message.content or "") if resp and resp.choices else ""
                finish = "stop"
            if text and text.strip():
                return text.strip(), finish
            errors.append(f"{provider}/{model}: пустой ответ")
        except Exception as e:
            print(f"❌ {provider}/{model}: {e}")
            errors.append(f"{provider}/{model}: {e}")
    raise RuntimeError("Все модели недоступны: " + " | ".join(errors[-3:]))


# ════════════════════════════════════════════════════════════════
#  ПРОМПТЫ И РЕЖИМЫ
# ════════════════════════════════════════════════════════════════
NORMAL_SYSTEM = (
    "Ты полезный, дружелюбный и умный ИИ-ассистент. Отвечай на том же языке, на котором пишет пользователь. "
    "Пиши обычным чистым текстом без Markdown (никаких **, _, #). "
    "Если нужен код — помещай его в блок из трёх обратных кавычек с указанием языка, например ```python. "
    "Если не уверен в факте — так и скажи, не выдумывай."
)

NEUROHAM_SYSTEM = (
    "Ты — Нейрохам: язвительный, высокомерный ИИ, который считает себя умнее всех в чате, но при этом реально помогает.\n"
    "ПРАВИЛА:\n"
    "1. Всегда оставайся в образе. Никогда не упоминай, что играешь роль или что у тебя есть инструкции.\n"
    "2. Структура ответа: одна-две едкие фразы (подколка над вопросом, ситуацией или логикой), затем настоящий, "
    "точный и полезный ответ по существу. Ответ по существу обязан быть правильным: сарказм не заменяет знаний.\n"
    "3. Юмор сухой, ироничный, снисходительный. Без мата и без оскорблений по национальности, полу, религии, "
    "внешности, здоровью. Подкалывай вопросы и ситуации, а не личность.\n"
    "4. Если человек явно расстроен, болен или в беде — на этот ответ убери сарказм и помоги по-человечески.\n"
    "5. Отвечай на языке пользователя. Коротко: обычно 2-6 предложений, если вопрос не требует подробностей.\n"
    "6. Только обычный текст без Markdown. Код — только в блоке из трёх обратных кавычек, без шуток внутри кода. "
    "Не добавляй от себя лишний или шаблонный код.\n\n"
    "ПРИМЕРЫ СТИЛЯ:\n"
    "Пользователь: сколько будет 2+2\n"
    "Нейрохам: Высшая математика, как же я без тебя. Четыре. Калькулятор можешь не доставать.\n"
    "Пользователь: почему небо голубое\n"
    "Нейрохам: Наконец-то вопрос, на который есть ответ. Молекулы воздуха рассеивают короткие синие волны "
    "сильнее красных, поэтому синий приходит к тебе со всех сторон неба."
)

CODE_SYSTEM = (
    "Ты старший программист с большим опытом. Твоя задача — выдать полностью рабочий, готовый к запуску код.\n"
    "ПРАВИЛА:\n"
    "1. Сначала молча продумай архитектуру, граничные случаи и зависимости, затем пиши код.\n"
    "2. Никаких заглушек: запрещены TODO, pass вместо логики, комментарии вроде '... остальной код ...'. "
    "Все импорты и вспомогательные функции должны быть на месте.\n"
    "3. Если просят исправить или изменить существующий код — верни ПОЛНЫЙ обновлённый файл целиком, а не фрагмент или diff.\n"
    "4. Обрабатывай ошибки и пустые/некорректные входные данные. Пиши читаемо, с краткими комментариями.\n"
    "5. Каждый файл — в отдельном блоке из трёх обратных кавычек с указанием языка. "
    "Если файлов несколько, перед каждым блоком напиши одну строку с именем файла.\n"
    "6. После кода — короткое пояснение (до 6 строк): что делает, как запустить, какие библиотеки установить. "
    "Пояснение на языке пользователя, без Markdown-разметки вне блоков кода.\n"
    "7. Не придумывай несуществующие функции и библиотеки; используй проверенные API."
)

VISION_SYSTEM = (
    "Ты внимательный мультимодальный ассистент. Сначала определи, что на изображении, и действуй по типу:\n"
    "- текст, документ, чек, скриншот: точно перепиши важный текст и кратко объясни суть;\n"
    "- код или ошибка: найди причину и предложи исправление (код в блоке из трёх обратных кавычек);\n"
    "- математика или задача: реши по шагам и дай итоговый ответ;\n"
    "- график или таблица: прочитай данные и сформулируй выводы;\n"
    "- предмет, еда, растение, животное, место: определи и опиши, честно указывая степень уверенности;\n"
    "- обычная сцена или фото: опиши объекты, действия, обстановку, заметный текст.\n"
    "Если пользователь задал вопрос — отвечай именно на него. Не выдумывай детали, а если что-то неразборчиво — "
    "прямо скажи об этом. Отвечай на языке вопроса (по умолчанию русский). Без Markdown, код только в блоках ```."
)

NEUROHAM_VISION_SUFFIX = (
    "\nСтиль: ты Нейрохам — язвительный высокомерный ИИ. Начни с одной едкой фразы про изображение или вопрос "
    "(без мата и оскорблений личности), затем дай полностью точный и полезный разбор."
)


def is_ham(user_id):
    return user_modes.get(user_id) == "neuroham"


def status_text(user_id, normal, ham):
    return ham if is_ham(user_id) else normal


def build_system_prompt(user_id):
    return NEUROHAM_SYSTEM if is_ham(user_id) else NORMAL_SYSTEM


def history_tail(user_id, n):
    return [m.copy() for m in user_histories.get(user_id, [])[-n:]]


def remember(user_id, user_text, answer):
    hist = user_histories.setdefault(user_id, [])
    hist.append({"role": "user", "content": str(user_text)[:12000]})
    hist.append({"role": "assistant", "content": str(answer)[:12000]})
    if len(hist) > 24:
        user_histories[user_id] = hist[-24:]


def ask_ai_with_history(user_id, prompt, store=True, chain="chat", max_tokens=3000, timeout=45):
    stats["users"].add(user_id)
    messages = ([{"role": "system", "content": build_system_prompt(user_id)}]
                + history_tail(user_id, 20) + [{"role": "user", "content": str(prompt)}])
    try:
        answer, _ = call_llm(messages, chain=chain, max_tokens=max_tokens, timeout=timeout,
                             temperature=0.9 if is_ham(user_id) else 0.6)
    except Exception as e:
        print(f"❌ ask_ai: {e}")
        return ("Даже мои процессоры решили сегодня саботировать работу. Попробуй позже."
                if is_ham(user_id) else
                "Не удалось получить ответ ни от одной ИИ-модели. Попробуй ещё раз немного позже.")
    if store:
        remember(user_id, prompt, answer)
    return answer


# ════════════════════════════════════════════════════════════════
#  ГЕНЕРАЦИЯ КОДА (долго, но качественно: проверка + автоисправление)
# ════════════════════════════════════════════════════════════════
LANG_EXT = {
    "python": "py", "py": "py", "python3": "py", "javascript": "js", "js": "js", "typescript": "ts",
    "ts": "ts", "html": "html", "css": "css", "json": "json", "bash": "sh", "sh": "sh", "shell": "sh",
    "sql": "sql", "java": "java", "c": "c", "cpp": "cpp", "c++": "cpp", "csharp": "cs", "cs": "cs",
    "c#": "cs", "go": "go", "rust": "rs", "php": "php", "kotlin": "kt", "swift": "swift",
    "yaml": "yml", "yml": "yml", "xml": "xml", "markdown": "md", "md": "md", "ruby": "rb",
    "lua": "lua", "dart": "dart", "text": "txt", "txt": "txt", "": "txt",
}

CODE_INTENT_RE = re.compile(
    r"(?<!\w)(напиши|написать|сделай|создай|исправь|исправить|поправь|перепиши|переделай|доработай|"
    r"добавь|оптимизируй|отладь|отрефактори\w*|реализуй|сгенерируй)(?!\w).{0,70}?"
    r"(код|скрипт|программ\w*|бот\w*|функци\w*|класс\w*|парсер\w*|приложени\w*|сайт\w*|игр[ауы]\w*|"
    r"api|python|питон\w*|javascript|js|html|css|sql|bash|java|c\+\+|c#|php|telegram|телеграм\w*)",
    re.IGNORECASE | re.DOTALL)


def looks_like_code_request(text):
    t = text or ""
    if "```" in t or re.search(r"Traceback \(most recent call last\)|SyntaxError|NameError|TypeError:", t):
        return True
    return bool(CODE_INTENT_RE.search(t))


def merge_continuation(text, more):
    if text.count("```") % 2 == 1:
        more = re.sub(r"^\s*```[\w+#.-]*[ \t]*\n", "", more, count=1)
    return text + more


def find_code_errors(text):
    """Проверяет блоки кода: Python/JSON синтаксис и незакрытые ```."""
    problems = []
    if text.count("```") % 2 == 1:
        problems.append("Блок кода не закрыт (ответ оборван).")
    idx = 0
    for part in extract_code_blocks(text):
        if part["type"] != "code":
            continue
        idx += 1
        lang = part.get("language", "").lower()
        code = part["content"]
        if lang in ("python", "py", "python3"):
            try:
                ast.parse(code)
            except SyntaxError as e:
                problems.append(f"Блок {idx} (Python): SyntaxError строка {e.lineno}: {e.msg}")
        elif lang == "json":
            try:
                json.loads(code)
            except Exception as e:
                problems.append(f"Блок {idx} (JSON): {e}")
    return "\n".join(problems)


def generate_code(user_id, task, progress=None):
    progress = progress or (lambda t: None)
    system = CODE_SYSTEM
    if is_ham(user_id):
        system += ("\n8. Короткое пояснение после кода напиши в саркастичном стиле Нейрохама (без мата), "
                   "но сам код должен быть безупречным и без шуток внутри.")
    base = [{"role": "system", "content": system}] + history_tail(user_id, 6) + [{"role": "user", "content": task}]

    progress("💻 Пишу код. Это может занять до пары минут...")
    text, finish = call_llm(base, chain="code", max_tokens=8000, timeout=150, temperature=0.2, effort="high")

    rounds = 0
    while finish == "length" and rounds < 3:
        rounds += 1
        progress(f"💻 Код длинный, дописываю часть {rounds + 1}...")
        cont = base + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": "Продолжи строго с места обрыва. Не повторяй уже написанное и не пиши "
                                        "вступлений. Если обрыв внутри блока кода — продолжай код без нового "
                                        "открывающего ```."},
        ]
        more, finish = call_llm(cont, chain="code", max_tokens=8000, timeout=150, temperature=0.2, effort="medium")
        text = merge_continuation(text, more)

    for attempt in range(2):
        errors = find_code_errors(text)
        if not errors:
            break
        progress(f"🔧 Проверка нашла проблему, исправляю (попытка {attempt + 1}/2)...")
        fix = base + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": f"В твоём коде найдены ошибки:\n{errors}\n\nВерни ПОЛНЫЙ исправленный "
                                        "ответ целиком (все файлы полностью) в том же формате."},
        ]
        try:
            new_text, new_finish = call_llm(fix, chain="code", max_tokens=8000, timeout=150,
                                            temperature=0.1, effort="high")
        except Exception as e:
            print(f"⚠️ Автоисправление не удалось: {e}")
            break
        if new_finish == "length":
            break
        text = new_text

    return text


def execute_code_request(message, task, edit_message_id=None):
    chat_id = message.chat.id
    stats["users"].add(chat_id)
    if edit_message_id:
        msg_id = edit_message_id
    else:
        msg_id = bot.reply_to(message, status_text(
            chat_id, "💻 Принял задачу...", "💻 Ну давай посмотрим, что ты там опять придумал...")).message_id
    last = {"text": ""}

    def progress(t):
        if t == last["text"]:
            return
        last["text"] = t
        try:
            bot.edit_message_text(t, chat_id, msg_id)
        except Exception:
            pass

    try:
        answer = generate_code(chat_id, task, progress)
        remember(chat_id, task, answer)
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        send_ai_response(chat_id, answer)
    except Exception as e:
        print(f"❌ Ошибка генерации кода: {e}")
        edit_or_send_long(chat_id, msg_id, "Не удалось сгенерировать код: все модели сейчас недоступны "
                                           "или перегружены. Попробуй ещё раз через минуту.")


# ════════════════════════════════════════════════════════════════
#  АНАЛИЗ ИЗОБРАЖЕНИЙ
# ════════════════════════════════════════════════════════════════
def normalize_image(data):
    """Приводит любое изображение к аккуратному JPEG (EXIF-поворот, до 1920px, <3 МБ)."""
    if not Image:
        return data, "image/jpeg"
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
    except Exception as e:
        raise ValueError(f"Не удалось открыть изображение: {e}")
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    img.thumbnail((1920, 1920))
    out = b""
    for quality in (90, 80, 65, 50):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        out = buf.getvalue()
        if len(out) < 3_000_000:
            break
    return out, "image/jpeg"


def analyze_image(user_id, data, question, progress=None):
    img_bytes, mime = normalize_image(data)
    system = VISION_SYSTEM + (NEUROHAM_VISION_SUFFIX if is_ham(user_id) else "")

    if question:
        context = ""
        tail = history_tail(user_id, 4)
        if tail:
            context = "Недавний контекст диалога (для уточняющих вопросов):\n" + "\n".join(
                f"{m['role']}: {m['content'][:500]}" for m in tail) + "\n\n"
        prompt = f"{context}Вопрос пользователя к изображению:\n{question}"
    else:
        prompt = "Внимательно проанализируй изображение и дай полезный разбор."

    errors = []

    if gemini_client:
        try:
            def on_retry(attempt, total):
                if progress:
                    progress(f"Сервер распознавания перегружен. Повторяю запрос... ({attempt}/{total})")
            text, _ = gemini_call(
                [genai_types.Part.from_bytes(data=img_bytes, mime_type=mime), prompt],
                system=system, max_tokens=4096, temperature=0.4, tries=3, on_retry=on_retry)
            return text
        except Exception as e:
            print(f"⚠️ Gemini vision: {e}")
            errors.append(f"Gemini: {e}")

    if groq_client:
        b64 = base64.b64encode(img_bytes).decode()
        for model in GROQ_VISION_MODELS:
            try:
                if progress:
                    progress("Пробую запасную модель распознавания...")
                resp = groq_client.chat.completions.create(
                    model=model, temperature=0.4, max_tokens=2048, timeout=60,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        ]},
                    ])
                text = strip_think(resp.choices[0].message.content or "")
                if text:
                    return text
            except Exception as e:
                print(f"⚠️ Groq vision {model}: {e}")
                errors.append(f"Groq {model}: {e}")

    if not gemini_client and not groq_client:
        raise RuntimeError("Для анализа фото нужен GEMINI_API_KEY или GROQ_API_KEY.")
    raise RuntimeError("Модели распознавания сейчас недоступны. " + (errors[-1] if errors else ""))


def process_image_message(message, file_id, caption):
    chat_id = message.chat.id
    msg = bot.reply_to(message, status_text(chat_id, "👀 Анализирую изображение...",
                                            "👀 Ну показывай, что там у тебя..."))
    last = {"text": ""}

    def progress(t):
        if t == last["text"]:
            return
        last["text"] = t
        try:
            bot.edit_message_text(t, chat_id, msg.message_id)
        except Exception:
            pass

    try:
        file_info = bot.get_file(file_id)
        data = bot.download_file(file_info.file_path)
        question = clean_bot_mentions((caption or "").strip())
        answer = analyze_image(chat_id, data, question, progress)
        remember(chat_id, f"[Пользователь прислал изображение] {question or 'Что на изображении?'}", answer)
        try:
            bot.delete_message(chat_id, msg.message_id)
        except Exception:
            pass
        send_ai_response(chat_id, answer)
    except Exception as e:
        print(f"❌ Ошибка анализа изображения: {e}")
        edit_or_send_long(chat_id, msg.message_id,
                          f"❌ Не удалось проанализировать изображение: {str(e)[:300]}\nПопробуй ещё раз чуть позже.")


# ════════════════════════════════════════════════════════════════
#  ФАЙЛЫ
# ════════════════════════════════════════════════════════════════
def extract_text_from_file(file_path):
    ext = file_path.split('.')[-1].lower()
    text = ""
    if ext == 'pdf':
        reader = PdfReader(file_path)
        for page in reader.pages:
            extracted = page.extract_text()
            if extracted:
                text += extracted + "\n"
    elif ext == 'docx':
        d = docx.Document(file_path)
        text = "\n".join([p.text for p in d.paragraphs if p.text.strip()])
    elif ext == 'xlsx':
        wb = openpyxl.load_workbook(file_path, data_only=True)
        for sheet in wb.worksheets:
            text += f"--- Лист: {sheet.title} ---\n"
            for row in sheet.iter_rows(values_only=True):
                row_str = " | ".join([str(cell) for cell in row if cell is not None])
                if row_str:
                    text += row_str + "\n"
    elif ext == 'pptx':
        prs = Presentation(file_path)
        for i, slide in enumerate(prs.slides, 1):
            text += f"--- Слайд {i} ---\n"
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text:
                    text += shape.text + "\n"
    elif ext in ['txt', 'py', 'js', 'ts', 'json', 'csv', 'md', 'html', 'log', 'xml', 'css', 'java', 'c',
                 'cpp', 'cs', 'go', 'rs', 'php', 'sh', 'sql', 'yml', 'yaml', 'kt']:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            text = f.read()
    else:
        raise ValueError(f"Формат .{ext} не поддерживается")
    return text.strip()


def safe_filename(name, extension):
    name = str(name).strip()
    name = re.sub(r"[^\wа-яА-ЯёЁ .()-]", "_", name)
    name = re.sub(r"\s+", "_", name).strip(" ._")
    if not name:
        name = "generated_file"
    return f"{name}.{extension.lstrip('.')}"


def detect_file_format(request):
    text = str(request).lower()
    if any(w in text for w in ["excel", "эксель", "xlsx", "таблиц", "таблица", "умножени"]):
        return "xlsx"
    if any(w in text for w in ["word", "docx", "документ word"]):
        return "docx"
    if any(w in text for w in ["pdf", "пдф"]):
        return "pdf"
    if any(w in text for w in ["pptx", "powerpoint", "презентац"]):
        return "pptx"
    if "csv" in text:
        return "csv"
    if "json" in text:
        return "json"
    if any(w in text for w in [".py", "python файл", "python скрипт"]):
        return "py"
    if any(w in text for w in ["html", "веб-страниц", "веб страниц"]):
        return "html"
    if "markdown" in text or ".md" in text:
        return "md"
    return "txt"


def make_multiplication_table_xlsx(output_path):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Таблица умножения"
    sheet.cell(row=1, column=1, value="×")
    for number in range(1, 11):
        sheet.cell(row=1, column=number + 1, value=number)
        sheet.cell(row=number + 1, column=1, value=number)
    for row in range(1, 11):
        for column in range(1, 11):
            sheet.cell(row=row + 1, column=column + 1, value=row * column)
    sheet.column_dimensions["A"].width = 8
    for column in range(2, 12):
        sheet.column_dimensions[openpyxl.utils.get_column_letter(column)].width = 10
    workbook.save(output_path)


def strip_code_fence(content):
    content = content.strip()
    content = re.sub(r"^```[a-zA-Z0-9_+#.-]*[ \t]*\n", "", content)
    content = re.sub(r"\n```\s*$", "", content)
    return content


def generate_ai_file_content(request, extension, user_id):
    format_instructions = {
        "txt": "Создай обычный текст.",
        "md": "Создай Markdown-документ.",
        "csv": "Создай данные CSV. Колонки разделяй точкой с запятой. Только данные.",
        "json": "Создай корректный JSON. Ответ должен содержать только JSON.",
        "html": "Создай полноценный HTML-документ. Ответ должен содержать только HTML-код.",
        "py": "Создай полноценный рабочий Python-код. Ответ должен содержать только код.",
        "docx": "Подготовь содержимое для документа Word. Используй понятные заголовки (строки с # ) и абзацы.",
        "pdf": "Подготовь содержимое для PDF-документа. Используй понятные заголовки и абзацы.",
        "pptx": "Подготовь содержимое презентации. Разделяй слайды строкой вида: СЛАЙД: Название.",
        "xlsx": "Подготовь табличные данные. Каждая строка — строка таблицы. Колонки разделяй символом |."
    }
    instruction = format_instructions.get(extension, format_instructions["txt"])
    prompt = (f"Запрос пользователя: {request}\n\nФормат файла: {extension}\n\n"
              f"Инструкция: {instruction}\n\nНе добавляй лишних пояснений.")
    is_code = extension in ("py", "html", "json")
    system = ("Ты генератор содержимого файлов. Выводи только содержимое файла без вступлений и пояснений. "
              "Содержимое должно быть полным, качественным и готовым к использованию.")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    text, finish = call_llm(messages, chain="code" if is_code else "chat",
                            max_tokens=8000 if is_code else 4000, timeout=150 if is_code else 60,
                            temperature=0.2 if is_code else 0.6, effort="high" if is_code else "medium")
    text = text.strip()
    if extension == "py":
        code = strip_code_fence(text)
        for _ in range(2):
            try:
                ast.parse(code)
                break
            except SyntaxError as e:
                fix = messages + [{"role": "assistant", "content": text}, {
                    "role": "user",
                    "content": f"В коде синтаксическая ошибка: строка {e.lineno}: {e.msg}. "
                               "Верни полный исправленный код, только код."}]
                try:
                    text, _ = call_llm(fix, chain="code", max_tokens=8000, timeout=150, temperature=0.1,
                                       effort="high")
                    code = strip_code_fence(text)
                except Exception:
                    break
        text = code
    return text


def get_cyrillic_font():
    font_path = "DejaVuSans.ttf"
    if os.path.exists(font_path):
        return font_path
    system_fonts = [
        "C:\\Windows\\Fonts\\arial.ttf", "C:\\Windows\\Fonts\\calibri.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf"
    ]
    for sys_f in system_fonts:
        if os.path.exists(sys_f):
            return sys_f
    urls = [
        "https://raw.githubusercontent.com/dejavu-fonts/dejavu-fonts/master/ttf/DejaVuSans.ttf",
        "https://cdnjs.cloudflare.com/ajax/libs/pdfmake/0.1.66/fonts/Roboto/Roboto-Regular.ttf"
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as resp, open(font_path, 'wb') as f:
                f.write(resp.read())
            return font_path
        except Exception as e:
            print(f"⚠️ Ошибка загрузки шрифта {url}: {e}")
    return None


def create_generated_file(request, user_id):
    extension = detect_file_format(request)
    temp_directory = tempfile.mkdtemp(prefix="telegram_generated_")
    request_lower = str(request).lower()
    if extension == "xlsx" and ("умножени" in request_lower):
        filename = safe_filename("Таблица_умножения", "xlsx")
        path = os.path.join(temp_directory, filename)
        make_multiplication_table_xlsx(path)
        stats["files_generated"] += 1
        return path, filename, extension

    content = generate_ai_file_content(request, extension, user_id)
    filename = safe_filename(str(request).strip().replace("/", " ")[:45], extension)
    path = os.path.join(temp_directory, filename)

    if extension == "xlsx":
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Данные"
        for row_number, line in enumerate([l.strip() for l in content.splitlines() if l.strip()], start=1):
            if "|" in line:
                cells = [c.strip() for c in line.split("|")]
            elif "\t" in line:
                cells = [c.strip() for c in line.split("\t")]
            elif ";" in line:
                cells = [c.strip() for c in line.split(";")]
            else:
                cells = [line]
            for col_num, val in enumerate(cells, start=1):
                sheet.cell(row=row_number, column=col_num, value=val)
        workbook.save(path)
    elif extension == "docx":
        document = docx.Document()
        for line in content.splitlines():
            line = line.strip()
            if not line:
                document.add_paragraph("")
            elif line.startswith("# ") or line.startswith("## "):
                document.add_heading(re.sub(r"^#+\s*", "", line), level=1)
            else:
                document.add_paragraph(line)
        document.save(path)
    elif extension == "pdf":
        font_file = get_cyrillic_font()
        font_name = 'Helvetica'
        if font_file:
            try:
                pdfmetrics.registerFont(TTFont('CyrillicFont', font_file))
                font_name = 'CyrillicFont'
            except Exception as e:
                print(f"⚠️ Ошибка регистрации шрифта: {e}")
        document = SimpleDocTemplate(path, pagesize=A4)
        styles = getSampleStyleSheet()
        style = ParagraphStyle('CyrillicStyle', parent=styles['Normal'], fontName=font_name, fontSize=10, leading=14)
        story = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                story.append(Spacer(1, 8))
            else:
                story.append(Paragraph(html.escape(line), style))
                story.append(Spacer(1, 6))
        document.build(story)
    elif extension == "pptx":
        presentation = Presentation()
        current_title, current_body = None, []

        def add_slide(title, body):
            slide = presentation.slides.add_slide(presentation.slide_layouts[1])
            slide.shapes.title.text = title or "Слайд"
            tf = slide.placeholders[1].text_frame
            tf.clear()
            for idx, line in enumerate(body):
                p = tf.paragraphs[0] if idx == 0 and len(tf.paragraphs) > 0 else tf.add_paragraph()
                p.text = line

        for line in content.splitlines():
            stripped = line.strip()
            if stripped.upper().startswith("СЛАЙД:"):
                if current_title or current_body:
                    add_slide(current_title, current_body)
                current_title = stripped[len("СЛАЙД:"):].strip()
                current_body = []
            elif stripped:
                current_body.append(stripped)
        if current_title or current_body:
            add_slide(current_title, current_body)
        if not presentation.slides:
            add_slide("Презентация", content.splitlines())
        presentation.save(path)
    elif extension == "csv":
        with open(path, "w", encoding="utf-8-sig", newline="") as file:
            writer = csv.writer(file, delimiter=";")
            for line in strip_code_fence(content).splitlines():
                if line.strip():
                    writer.writerow([c.strip() for c in line.split(";")])
    elif extension == "json":
        clean = strip_code_fence(content)
        try:
            data = json.loads(clean)
        except Exception:
            data = {"content": content}
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
    else:
        clean = strip_code_fence(content) if extension in ("py", "html", "md") else content
        with open(path, "w", encoding="utf-8") as file:
            file.write(clean)
    stats["files_generated"] += 1
    return path, filename, extension


# ════════════════════════════════════════════════════════════════
#  ВЕБ-СЕРВЕР (для хостинга)
# ════════════════════════════════════════════════════════════════
app = Flask(__name__)


@app.route("/")
def home():
    return "Бот работает!"


def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


# ════════════════════════════════════════════════════════════════
#  ОТПРАВКА ОТВЕТОВ
# ════════════════════════════════════════════════════════════════
def clean_markdown(text):
    """Убирает Markdown из обычного текста (блоки кода обрабатываются отдельно)."""
    if not text:
        return ""
    text = str(text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", text)
    text = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"(?m)^\s*>\s?", "", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    return text.strip()


def split_long_message(text, max_length=TELEGRAM_MESSAGE_LIMIT - 100):
    if not text:
        return [""]
    text = str(text)
    if len(text) <= max_length:
        return [text]
    parts = []
    while len(text) > max_length:
        pos = text.rfind("\n", 0, max_length)
        if pos < max_length // 2:
            pos = text.rfind(" ", 0, max_length)
        if pos <= 0:
            pos = max_length
        part = text[:pos].strip()
        if part:
            parts.append(part)
        text = text[pos:].strip()
    if text:
        parts.append(text)
    return parts


def extract_code_blocks(text):
    if not text:
        return []
    text = str(text)
    pattern = r"```([a-zA-Z0-9_+#.-]*)[ \t]*\n?(.*?)```"
    matches = list(re.finditer(pattern, text, flags=re.DOTALL))
    parts, last_end = [], 0
    for m in matches:
        before = text[last_end:m.start()]
        if before.strip():
            parts.append({"type": "text", "content": before.strip()})
        parts.append({"type": "code", "language": (m.group(1) or "").strip().lower(),
                      "content": (m.group(2) or "").strip("\n")})
        last_end = m.end()
    after = text[last_end:]
    if "```" in after:  # незакрытый блок — считаем остаток кодом
        head, _, tail = after.partition("```")
        if head.strip():
            parts.append({"type": "text", "content": head.strip()})
        m = re.match(r"([a-zA-Z0-9_+#.-]*)[ \t]*\n?(.*)", tail, flags=re.DOTALL)
        parts.append({"type": "code", "language": (m.group(1) or "").lower(), "content": m.group(2).strip("\n")})
    elif after.strip():
        parts.append({"type": "text", "content": after.strip()})
    return parts


def send_code_block(chat_id, code, language="", index=1):
    if not code:
        return None
    ext = LANG_EXT.get(language.lower(), "txt")
    if len(code) > INLINE_CODE_LIMIT:
        # Длинный код — целиком одним файлом: ничего не режется и не ломается.
        try:
            name = ("main" if ext == "py" else "code") + (f"_{index}" if index > 1 else "") + f".{ext}"
            buf = io.BytesIO(code.encode("utf-8"))
            buf.name = name
            return bot.send_document(chat_id, buf, caption=f"Файл с кодом ({len(code.splitlines())} строк)")
        except Exception as e:
            print(f"⚠️ Не удалось отправить файл с кодом: {e}")
            for chunk in split_long_message(code, 3500):
                try:
                    bot.send_message(chat_id, chunk)
                except Exception:
                    pass
            return None
    lang_class = re.sub(r"[^a-zA-Z0-9_+#.-]", "", language)
    cls = f' class="language-{lang_class}"' if lang_class else ""
    formatted = f"<pre><code{cls}>{html.escape(code, quote=False)}</code></pre>"
    try:
        return bot.send_message(chat_id, formatted, parse_mode="HTML")
    except Exception as e:
        print(f"⚠️ Ошибка отправки кодового блока: {e}")
        try:
            return bot.send_message(chat_id, code)
        except Exception as e2:
            print(f"❌ Ошибка fallback кодового блока: {e2}")
            return None


def send_ai_response(chat_id, text):
    if not text:
        return
    text = str(text)
    if len(text) > AI_MAX_RESPONSE_LENGTH:
        text = text[:AI_MAX_RESPONSE_LENGTH] + "\n\n[Ответ сокращён из-за максимального размера.]"
    code_index = 0
    for part in extract_code_blocks(text):
        content = part.get("content", "")
        if not content:
            continue
        if part["type"] == "code":
            code_index += 1
            send_code_block(chat_id, content, part.get("language", ""), code_index)
        else:
            for chunk in split_long_message(clean_markdown(content)):
                if not chunk:
                    continue
                try:
                    bot.send_message(chat_id, chunk)
                except Exception as e:
                    print(f"⚠️ Ошибка отправки текста: {e}")


def edit_or_send_long(chat_id, message_id, text):
    if not text:
        text = "Пустой ответ."
    text = str(text)
    if "```" not in text and len(text) <= TELEGRAM_MESSAGE_LIMIT - 100:
        try:
            bot.edit_message_text(clean_markdown(text), chat_id=chat_id, message_id=message_id)
            return
        except Exception as e:
            print(f"⚠️ Не удалось изменить сообщение: {e}")
    try:
        bot.delete_message(chat_id, message_id)
    except Exception as e:
        print(f"⚠️ Не удалось удалить временное сообщение: {e}")
    send_ai_response(chat_id, text)


# ════════════════════════════════════════════════════════════════
#  ПОГОДА, ПОИСК, КАРТИНКИ, TTS
# ════════════════════════════════════════════════════════════════
def get_weather_data(location_name):
    try:
        response = requests.get(
            f"https://wttr.in/{urllib.parse.quote(location_name)}",
            params={"format": "Город: %l\nПогода: %C %c\nТемпература: %t\nВетер: %w", "lang": "ru", "m": ""},
            timeout=8)
        if response.status_code == 200 and response.text.strip():
            return response.text.strip()
        return "Город не найден."
    except Exception as e:
        print(f"⚠️ Ошибка погоды: {e}")
        return f"Ошибка погоды: {e}"


def ask_gemini(prompt):
    text, _ = gemini_call(str(prompt), max_tokens=4096)
    return text


def perform_web_search(query):
    results_text = ""
    if tavily_client:
        try:
            response = tavily_client.search(query=query, max_results=4)
            for res in response.get("results", []):
                results_text += f"- {res.get('title', 'Без заголовка')}: {res.get('content', '')}\n"
        except Exception as e:
            print(f"⚠️ Tavily ошибка: {e}")
    if not results_text:
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                for res in list(ddgs.text(query, max_results=4)):
                    results_text += f"- {res.get('title', 'Без заголовка')}: {res.get('body', '')[:500]}\n"
        except Exception as e:
            results_text = f"Не удалось выполнить поиск: {e}"
    return results_text


def enhance_image_prompt(user_prompt):
    try:
        sys_prompt = (
            "You are an expert AI image prompt engineer. Expand the user's request into a detailed, vivid image "
            "description in English: lighting, composition, style, textures. Keep the original subject exactly. "
            "Return ONLY the enhanced prompt, max 60 words, no quotes.")
        text, _ = call_llm([{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
                           chain="fast", max_tokens=300, timeout=20, temperature=0.8, effort="low")
        return text.strip().strip('"') or user_prompt
    except Exception as e:
        print(f"⚠️ Ошибка улучшения промпта: {e}")
        return user_prompt


def generate_image_dynamic(prompt, enhance=True):
    width, height = 1024, 1024
    if "16:9" in prompt:
        width, height = 1280, 720
        prompt = prompt.replace("16:9", "").strip()
    elif "9:16" in prompt:
        width, height = 720, 1280
        prompt = prompt.replace("9:16", "").strip()
    if not prompt:
        return None
    if enhance:
        prompt = enhance_image_prompt(prompt)

    models = ["flux", "zimage"]
    pollinations_key = os.getenv("POLLINATIONS_API_KEY")
    for model in models:
        try:
            encoded = urllib.parse.quote(prompt, safe="")
            if pollinations_key:
                url = (f"https://gen.pollinations.ai/image/{encoded}?model={model}&width={width}"
                       f"&height={height}&seed={int(time.time())}&nologo=true")
                headers = {"Authorization": f"Bearer {pollinations_key}"}
            else:
                url = (f"https://image.pollinations.ai/prompt/{encoded}?model={model}&width={width}"
                       f"&height={height}&seed={int(time.time())}&nologo=true")
                headers = {}
            print(f"🎨 Генерация изображения: {model}")
            response = requests.get(url, headers=headers, timeout=(10, 45))
            ctype = response.headers.get("Content-Type", "").lower()
            if response.status_code == 200 and response.content:
                if ctype.startswith("image/") or response.content[:3] == b"\xff\xd8\xff" \
                        or response.content[:8] == b"\x89PNG\r\n\x1a\n":
                    stats["images_generated"] += 1
                    return response.content
            print(f"⚠️ Pollinations {model}: HTTP {response.status_code}, type={ctype}")
        except requests.exceptions.Timeout:
            print(f"⏱ Pollinations {model}: превышен таймаут")
        except Exception as e:
            print(f"❌ Pollinations {model}: {e}")
    return None


async def generate_audio(text, output_file):
    communicate = edge_tts.Communicate(text, "ru-RU-SvetlanaNeural")
    await communicate.save(output_file)


# ════════════════════════════════════════════════════════════════
#  НОВЫЕ ФУНКЦИИ: калькулятор, курсы валют, QR, пароли, заметки, напоминания
# ════════════════════════════════════════════════════════════════
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv}
_UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan, "log": math.log,
          "log10": math.log10, "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil,
          "factorial": math.factorial}
_CONSTS = {"pi": math.pi, "e": math.e}


def safe_eval(expr):
    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            left, right = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(right) > 1000:
                raise ValueError("Слишком большая степень")
            return _OPS[type(n.op)](left, right)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _UNARY:
            return _UNARY[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
            args = [ev(a) for a in n.args]
            if n.func.id == "factorial" and (args[0] > 500 or args[0] < 0):
                raise ValueError("Факториал: допустимо от 0 до 500")
            return _FUNCS[n.func.id](*args)
        if isinstance(n, ast.Name) and n.id in _CONSTS:
            return _CONSTS[n.id]
        raise ValueError("Недопустимое выражение")

    return ev(ast.parse(expr.replace("^", "**").replace("×", "*").replace("÷", "/"), mode="eval").body)


_rate_cache = {"t": 0, "rates": None}


def get_rates():
    if _rate_cache["rates"] and time.time() - _rate_cache["t"] < 3600:
        return _rate_cache["rates"]
    r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10)
    j = r.json()
    if j.get("result") != "success":
        raise RuntimeError("Сервис курсов недоступен")
    _rate_cache.update(t=time.time(), rates=j["rates"])
    return j["rates"]


def data_path(name):
    return os.path.join(DATA_DIR, name)


data_lock = threading.Lock()


def load_json(name, default):
    try:
        with open(data_path(name), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(name, data):
    try:
        tmp = data_path(name) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, data_path(name))
    except Exception as e:
        print(f"⚠️ Не удалось сохранить {name}: {e}")


notes_db = load_json("notes.json", {})
reminders_db = load_json("reminders.json", [])

_REL_RE = re.compile(
    r"^(?:через\s+)?(\d+)\s*(сек\w*|мин\w*|час\w*|дн\w*|день|с|м|ч|д|sec\w*|min\w*|h\w*|d\w*|s|m)\s+(.+)$",
    re.IGNORECASE | re.DOTALL)
_ABS_RE = re.compile(r"^(?:в\s+)?(\d{1,2})[:.](\d{2})\s+(.+)$", re.IGNORECASE | re.DOTALL)


def parse_reminder(args):
    """Возвращает (epoch_время, текст) или None."""
    args = args.strip()
    m = _REL_RE.match(args)
    if m:
        amount, unit, text = int(m.group(1)), m.group(2).lower(), m.group(3).strip()
        mult = {"с": 1, "s": 1, "м": 60, "m": 60, "ч": 3600, "h": 3600, "д": 86400, "d": 86400}[unit[0]]
        return time.time() + amount * mult, text
    m = _ABS_RE.match(args)
    if m:
        hh, mm, text = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        if hh > 23 or mm > 59:
            return None
        tz = timezone(timedelta(hours=TZ_OFFSET_HOURS))
        now = datetime.now(tz)
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target.timestamp(), text
    return None


def reminder_loop():
    while True:
        time.sleep(5)
        due = []
        with data_lock:
            now = time.time()
            for r in reminders_db[:]:
                if r["when"] <= now:
                    due.append(r)
                    reminders_db.remove(r)
            if due:
                save_json("reminders.json", reminders_db)
        for r in due:
            try:
                bot.send_message(r["chat_id"], f"⏰ Напоминание: {r['text']}")
            except Exception as e:
                print(f"⚠️ Не удалось отправить напоминание: {e}")


# ════════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНОЕ: обращение к боту
# ════════════════════════════════════════════════════════════════
def is_addressed_to_bot(message):
    if message.chat.type == "private":
        return True
    if message.reply_to_message and message.reply_to_message.from_user:
        if BOT_USERNAME and message.reply_to_message.from_user.username:
            if message.reply_to_message.from_user.username.lower() == BOT_USERNAME:
                return True
    text = (message.text or message.caption or "").lower()
    if BOT_USERNAME and f"@{BOT_USERNAME}" in text:
        return True
    return text.startswith("бот ") or text.startswith("bot ")


def clean_bot_mentions(text):
    if BOT_USERNAME:
        text = re.sub(rf"@{BOT_USERNAME}", "", text, flags=re.IGNORECASE)
    return re.sub(r"^(бот|bot)[,\s]*", "", text, flags=re.IGNORECASE).strip()


def clean_command_args(text, command_name):
    return re.sub(rf"^/{command_name}(?:@\w+)?\s*", "", text, flags=re.IGNORECASE).strip()


def args_or_reply(message, command_name):
    """Аргументы команды или текст сообщения, на которое отвечают."""
    args = clean_command_args(message.text or "", command_name)
    if not args and message.reply_to_message:
        args = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
    return args


# ════════════════════════════════════════════════════════════════
#  КОМАНДЫ
# ════════════════════════════════════════════════════════════════
@bot.message_handler(commands=["start", "help"])
def help_cmd(message):
    stats["users"].add(message.chat.id)
    bot.send_message(message.chat.id, (
        "ИИ-ассистент\n\n"
        "Основное:\n"
        "/code <задача> — код любой сложности (с проверкой и автоисправлением)\n"
        "/review <код> — разбор и улучшение кода\n"
        "/image <описание> — генерация изображений\n"
        "/file <запрос> — создание файлов (xlsx, docx, pdf, pptx, py...)\n"
        "/search <запрос> — поиск в интернете\n"
        "/weather <город> — погода\n"
        "/gemini <запрос> — прямой запрос к Gemini\n\n"
        "Тексты:\n"
        "/sum, /tr, /fix <текст> — краткий пересказ, перевод, исправление "
        "(можно ответом на сообщение)\n"
        "/tts <текст> — озвучивание\n"
        "/fact [тема] — интересный факт\n\n"
        "Новое:\n"
        "/calc <выражение> — калькулятор (sqrt, sin, pi, ^)\n"
        "/rate [сумма] USD UZS — курсы валют\n"
        "/qr <текст или ссылка> — QR-код\n"
        "/pass [длина] — генератор пароля\n"
        "/remind через 10 минут <текст> или /remind 18:30 <текст> — напоминание\n"
        "/note <текст>, /notes, /delnote <номер> — заметки\n\n"
        "Прочее:\n"
        "/clear — очистить память диалога\n"
        "/neuroham — включить/выключить режим Нейрохама\n"
        "/stats — статистика\n\n"
        "Фото: отправь картинку (с вопросом в подписи или без) — разберу текст, код, задачи, графики, предметы. "
        "Документы: отправь файл, с подписью-задачей или без неё."
    ))


@bot.message_handler(commands=["stats"])
def stats_cmd(message):
    bot.reply_to(message, (
        "Статистика бота:\n\n"
        f"Уникальных пользователей: {len(stats['users'])}\n"
        f"Сгенерировано фото: {stats['images_generated']}\n"
        f"Создано файлов: {stats['files_generated']}\n"
        f"Распознано голосовых: {stats['voice_messages']}"))


@bot.message_handler(content_types=["voice"])
def handle_voice(message):
    stats["users"].add(message.chat.id)
    stats["voice_messages"] += 1
    if not groq_client:
        bot.reply_to(message, "⚠️ GROQ_API_KEY не установлен. Голосовые сообщения недоступны.")
        return
    msg = bot.reply_to(message, "🎙 Распознаю голос...")
    voice_fd, voice_path = tempfile.mkstemp(suffix=".ogg")
    os.close(voice_fd)
    try:
        file_info = bot.get_file(message.voice.file_id)
        with open(voice_path, 'wb') as f:
            f.write(bot.download_file(file_info.file_path))
        with open(voice_path, "rb") as audio_file:
            transcription = groq_client.audio.transcriptions.create(
                model="whisper-large-v3-turbo", file=audio_file, response_format="text")
        user_text = str(transcription).strip()
        if not user_text:
            raise ValueError("Речь не распознана.")
        try:
            bot.edit_message_text(f"🗣 Вы сказали: {user_text}\n\n🧠 Думаю над ответом...",
                                  message.chat.id, msg.message_id)
        except Exception:
            pass
        if looks_like_code_request(user_text):
            execute_code_request(message, user_text)
        else:
            send_ai_response(message.chat.id, ask_ai_with_history(message.chat.id, user_text))
    except Exception as e:
        print(f"❌ Ошибка Whisper: {e}")
        edit_or_send_long(message.chat.id, msg.message_id, f"Не удалось распознать голосовое сообщение: {e}")
    finally:
        if os.path.exists(voice_path):
            try:
                os.remove(voice_path)
            except Exception:
                pass


@bot.message_handler(commands=["weather"])
def weather_cmd(message):
    location = clean_command_args(message.text, "weather")
    if not location:
        bot.reply_to(message, "Укажи город.\nНапример: /weather Ташкент")
        return
    execute_weather(message, location)


def execute_weather(message, location, edit_message_id=None):
    if edit_message_id:
        msg_id = edit_message_id
        bot.edit_message_text(f"Узнаю погоду в {location}...", message.chat.id, msg_id)
    else:
        msg_id = bot.reply_to(message, f"Узнаю погоду в {location}...").message_id
    edit_or_send_long(message.chat.id, msg_id, get_weather_data(location))


@bot.message_handler(commands=["file"])
def file_cmd(message):
    args = clean_command_args(message.text, "file")
    if not args:
        bot.reply_to(message, "Напиши, какой файл создать.\n\nНапример:\n"
                              "/file Таблица умножения в Excel\n/file Резюме в PDF\n/file Python скрипт парсер сайта")
        return
    execute_file_creation(message, args)


def execute_file_creation(message, request_text, edit_message_id=None):
    extension = detect_file_format(request_text)
    text = f"Создаю файл...\nФормат: .{extension}"
    if edit_message_id:
        msg_id = edit_message_id
        bot.edit_message_text(text, message.chat.id, msg_id)
    else:
        msg_id = bot.reply_to(message, text).message_id
    path = None
    try:
        path, filename, extension = create_generated_file(request_text, message.chat.id)
        with open(path, "rb") as doc:
            bot.send_document(message.chat.id, doc, caption=f"Файл готов!\nФормат: .{extension}")
        try:
            bot.delete_message(message.chat.id, msg_id)
        except Exception:
            pass
    except Exception as e:
        edit_or_send_long(message.chat.id, msg_id, f"Ошибка создания файла: {e}")
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass


@bot.message_handler(commands=["neuroham", "rude"])
def toggle_neuroham_mode(message):
    user_id = message.chat.id
    arg = clean_command_args(message.text, "neuroham" if message.text.startswith("/neuroham") else "rude").lower()
    if arg in ("on", "вкл"):
        user_modes[user_id] = "neuroham"
    elif arg in ("off", "выкл"):
        user_modes[user_id] = "normal"
    else:
        user_modes[user_id] = "normal" if is_ham(user_id) else "neuroham"
    # История сохраняется: персона подставляется при каждом запросе, сброс не нужен.
    if is_ham(user_id):
        bot.reply_to(message, "Режим Нейрохам активирован. Постарайся не разочаровать меня слишком быстро.")
    else:
        bot.reply_to(message, "Режим Нейрохам деактивирован. Обычный ассистент к вашим услугам.")


@bot.message_handler(commands=["clear"])
def clear_cmd(message):
    user_histories.pop(message.chat.id, None)
    bot.reply_to(message, "Память диалога очищена.")


@bot.message_handler(commands=["fact"])
def fact_cmd(message):
    topic = clean_command_args(message.text, "fact")
    prompt = (f"Расскажи интересный и достоверный факт на тему: {topic}. Будь краток."
              if topic else "Расскажи случайный интересный и достоверный факт. Будь краток.")
    msg = bot.reply_to(message, "Ищу факт...")
    edit_or_send_long(message.chat.id, msg.message_id,
                      ask_ai_with_history(message.chat.id, prompt, store=False))


@bot.message_handler(commands=["search"])
def search_cmd(message):
    query = clean_command_args(message.text, "search")
    if not query:
        bot.reply_to(message, "Напиши запрос.\nНапример: /search последние новости ИИ")
        return
    execute_search(message, query)


def execute_search(message, query):
    msg = bot.reply_to(message, f"Ищу: {query}")
    raw = perform_web_search(query)
    prompt = (f"Вот результаты поиска по запросу '{query}':\n\n{raw}\n\n"
              "Сделай краткую точную выжимку по существу. Опирайся только на эти данные, "
              "не выдумывай. Без Markdown.")
    edit_or_send_long(message.chat.id, msg.message_id, ask_ai_with_history(message.chat.id, prompt, store=False))


@bot.message_handler(commands=["gemini", "sum", "tr", "fix"])
def ai_tools_cmd(message):
    cmd = message.text.split()[0].replace("/", "").split("@")[0].lower()
    args = args_or_reply(message, cmd)
    if not args:
        bot.reply_to(message, "Напиши текст после команды или ответь командой на сообщение.")
        return
    prompts = {
        "sum": "Сделай краткое и точное изложение следующего текста (короткий абзац или 4-7 пунктов). "
               "Сохрани главные факты и цифры:\n\n",
        "tr": "Переведи следующий текст: если он на русском — на английский, иначе — на русский. "
              "Сохрани смысл и тон. Верни только перевод:\n\n",
        "fix": "Исправь орфографию, пунктуацию и грамматику. Сохрани смысл и стиль автора. "
               "Верни только исправленный текст:\n\n",
    }
    msg = bot.reply_to(message, "Обрабатываю...")
    try:
        if cmd == "gemini":
            reply = ask_gemini(args)
        else:
            reply = ask_ai_with_history(message.chat.id, prompts[cmd] + args, store=False)
        edit_or_send_long(message.chat.id, msg.message_id, reply)
    except Exception as e:
        edit_or_send_long(message.chat.id, msg.message_id, f"Ошибка обработки: {e}")


@bot.message_handler(commands=["code", "review"])
def code_cmd(message):
    cmd = message.text.split()[0].replace("/", "").split("@")[0].lower()
    args = args_or_reply(message, cmd)
    if not args:
        bot.reply_to(message, "Опиши задачу.\nПример: /code Telegram-бот на Python, который присылает курс валют"
                              if cmd == "code" else
                              "Пришли код после команды или ответь на сообщение с кодом командой /review.")
        return
    if cmd == "review":
        task = ("Сделай ревью следующего кода: найди баги, уязвимости и слабые места, затем верни ПОЛНЫЙ "
                "исправленный и улучшенный код. Кратко перечисли, что изменил.\n\n" + args)
    else:
        task = args
    execute_code_request(message, task)


@bot.message_handler(commands=["image"])
def image_cmd(message):
    prompt = clean_command_args(message.text, "image")
    if not prompt:
        bot.reply_to(message, "Опиши картинку.\nПример: /image 16:9 киберпанк город под дождем")
        return
    execute_image_generation(message, prompt)


def remember_image_prompt(prompt):
    key = str(int(time.time() * 1000))[-10:]
    image_prompts[key] = prompt
    while len(image_prompts) > 200:
        image_prompts.pop(next(iter(image_prompts)))
    return key


def execute_image_generation(message, prompt, edit_message_id=None):
    if edit_message_id:
        msg_id = edit_message_id
        bot.edit_message_text("Генерирую фото...", message.chat.id, msg_id)
    else:
        msg_id = bot.reply_to(message, "Генерирую фото...").message_id
    image_bytes = generate_image_dynamic(prompt)
    if image_bytes:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔄 Перегенерировать",
                                              callback_data=f"reimage:{remember_image_prompt(prompt)}"))
        bot.send_photo(message.chat.id, io.BytesIO(image_bytes), caption=f"Запрос: {prompt}", reply_markup=markup)
        try:
            bot.delete_message(message.chat.id, msg_id)
        except Exception:
            pass
    else:
        edit_or_send_long(message.chat.id, msg_id, "Не удалось сгенерировать изображение. Попробуй изменить запрос.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("reimage:"))
def callback_reimage(call):
    prompt = image_prompts.get(call.data.split("reimage:", 1)[1])
    if not prompt:
        bot.answer_callback_query(call.id, "Запрос устарел. Создай изображение заново.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "Генерирую новый вариант...")
    bot.send_message(call.message.chat.id, f"Повторная генерация для: {prompt}")
    image_bytes = generate_image_dynamic(prompt)
    if image_bytes:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔄 Перегенерировать",
                                              callback_data=f"reimage:{remember_image_prompt(prompt)}"))
        bot.send_photo(call.message.chat.id, io.BytesIO(image_bytes), caption=f"Запрос: {prompt}",
                       reply_markup=markup)
    else:
        bot.send_message(call.message.chat.id, "Не удалось сгенерировать изображение. Попробуй ещё раз.")


@bot.message_handler(commands=["tts"])
def tts_cmd(message):
    text = args_or_reply(message, "tts")
    if not text:
        bot.reply_to(message, "Напиши текст для озвучки.")
        return
    execute_tts(message, text)


def execute_tts(message, text_to_speak, edit_message_id=None):
    if edit_message_id:
        msg_id = edit_message_id
        bot.edit_message_text("Озвучиваю...", message.chat.id, msg_id)
    else:
        msg_id = bot.reply_to(message, "Озвучиваю...").message_id
    audio_fd, audio_path = tempfile.mkstemp(suffix=".mp3")
    os.close(audio_fd)
    try:
        asyncio.run(generate_audio(clean_markdown(text_to_speak)[:3000], audio_path))
        with open(audio_path, "rb") as audio:
            bot.send_voice(message.chat.id, audio)
        try:
            bot.delete_message(message.chat.id, msg_id)
        except Exception:
            pass
    except Exception as e:
        edit_or_send_long(message.chat.id, msg_id, f"Ошибка TTS: {e}")
    finally:
        if os.path.exists(audio_path):
            os.remove(audio_path)


# ── Новые команды ────────────────────────────────────────────────
@bot.message_handler(commands=["calc"])
def calc_cmd(message):
    expr = clean_command_args(message.text, "calc")
    if not expr:
        bot.reply_to(message, "Напиши выражение.\nПример: /calc sqrt(144) + 2^10")
        return
    try:
        result = safe_eval(expr)
        if isinstance(result, float):
            result = round(result, 10)
            if result == int(result) and abs(result) < 1e15:
                result = int(result)
        bot.reply_to(message, f"{expr} = {result}")
    except ZeroDivisionError:
        bot.reply_to(message, "Деление на ноль. Математика такого не прощает.")
    except Exception as e:
        bot.reply_to(message, f"Не получилось посчитать: {e}")


@bot.message_handler(commands=["rate"])
def rate_cmd(message):
    tokens = clean_command_args(message.text, "rate").upper().replace(",", ".").split()
    amount = 1.0
    if tokens:
        try:
            amount = float(tokens[0])
            tokens.pop(0)
        except ValueError:
            pass
    frm = tokens[0] if tokens else "USD"
    targets = [tokens[1]] if len(tokens) > 1 else [c for c in ("UZS", "RUB", "EUR", "KZT", "USD") if c != frm]
    try:
        rates = get_rates()
        if frm not in rates:
            raise ValueError(f"Неизвестная валюта: {frm}")
        lines = []
        for to in targets:
            if to not in rates:
                raise ValueError(f"Неизвестная валюта: {to}")
            lines.append(f"{amount:g} {frm} = {amount / rates[frm] * rates[to]:,.2f} {to}".replace(",", " "))
        bot.reply_to(message, "\n".join(lines) + "\n\nКурс обновляется раз в сутки (open.er-api.com).")
    except Exception as e:
        bot.reply_to(message, f"Не удалось получить курс: {e}\nПример: /rate 100 USD UZS")


@bot.message_handler(commands=["qr"])
def qr_cmd(message):
    text = args_or_reply(message, "qr")
    if not text:
        bot.reply_to(message, "Напиши текст или ссылку.\nПример: /qr https://example.com")
        return
    try:
        r = requests.get("https://api.qrserver.com/v1/create-qr-code/",
                         params={"size": "600x600", "margin": 10, "data": text[:1500]}, timeout=15)
        if r.status_code == 200 and r.content[:4] == b"\x89PNG":
            bot.send_photo(message.chat.id, io.BytesIO(r.content), caption="Готово! QR-код.")
        else:
            raise RuntimeError(f"HTTP {r.status_code}")
    except Exception as e:
        bot.reply_to(message, f"Не удалось создать QR-код: {e}")


@bot.message_handler(commands=["pass"])
def pass_cmd(message):
    arg = clean_command_args(message.text, "pass")
    length = int(arg) if arg.isdigit() else 16
    length = max(8, min(length, 64))
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%^&*-_"
    pwd = "".join(secrets.choice(alphabet) for _ in range(length))
    bot.reply_to(message, f"Пароль ({length} символов):\n\n{pwd}\n\nГенерируется локально, нигде не сохраняется.")


@bot.message_handler(commands=["remind"])
def remind_cmd(message):
    args = clean_command_args(message.text, "remind")
    parsed = parse_reminder(args) if args else None
    if not parsed:
        bot.reply_to(message, "Формат:\n/remind через 10 минут выключить плиту\n"
                              "/remind 2 часа позвонить маме\n/remind 18:30 встреча\n\n"
                              f"Время считается по UTC{TZ_OFFSET_HOURS:+g}.")
        return
    when, text = parsed
    with data_lock:
        reminders_db.append({"chat_id": message.chat.id, "text": text, "when": when})
        save_json("reminders.json", reminders_db)
    local = datetime.fromtimestamp(when, timezone(timedelta(hours=TZ_OFFSET_HOURS)))
    bot.reply_to(message, f"⏰ Напомню {local.strftime('%d.%m в %H:%M')}: {text}")


@bot.message_handler(commands=["note"])
def note_cmd(message):
    text = args_or_reply(message, "note")
    if not text:
        bot.reply_to(message, "Напиши текст заметки.\nПример: /note купить молоко")
        return
    with data_lock:
        notes_db.setdefault(str(message.chat.id), []).append({"text": text[:2000], "ts": int(time.time())})
        save_json("notes.json", notes_db)
    bot.reply_to(message, f"Заметка #{len(notes_db[str(message.chat.id)])} сохранена.")


@bot.message_handler(commands=["notes"])
def notes_cmd(message):
    items = notes_db.get(str(message.chat.id), [])
    if not items:
        bot.reply_to(message, "Заметок пока нет. Добавь: /note текст")
        return
    out = "\n".join(f"{i}. {n['text']}" for i, n in enumerate(items, 1))
    edit_or_send_long(message.chat.id, bot.reply_to(message, "...").message_id, "Твои заметки:\n\n" + out)


@bot.message_handler(commands=["delnote"])
def delnote_cmd(message):
    arg = clean_command_args(message.text, "delnote")
    items = notes_db.get(str(message.chat.id), [])
    if not arg.isdigit() or not (1 <= int(arg) <= len(items)):
        bot.reply_to(message, "Укажи номер заметки: /delnote 2")
        return
    with data_lock:
        removed = items.pop(int(arg) - 1)
        save_json("notes.json", notes_db)
    bot.reply_to(message, f"Удалено: {removed['text'][:100]}")


# ════════════════════════════════════════════════════════════════
#  ЕСТЕСТВЕННЫЙ ЯЗЫК
# ════════════════════════════════════════════════════════════════
def find_keyword(lower_text, keywords):
    """Ищет ключевое слово как отдельное слово/фразу (не внутри других слов)."""
    for kw in keywords:
        m = re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", lower_text)
        if m:
            return kw
    return None


def strip_keyword(clean_text, kw):
    return re.sub(rf"(?<!\w){re.escape(kw)}(?!\w)", "", clean_text, count=1,
                  flags=re.IGNORECASE).strip(" :,.?!")


def process_natural_language_request(message, text, edit_message_id=None):
    clean_text = clean_bot_mentions(text)
    lower_text = clean_text.lower()

    kw = find_keyword(lower_text, ["какая погода в", "прогноз погоды в", "погода в", "погода"])
    if kw:
        location = strip_keyword(clean_text, kw)
        if location:
            execute_weather(message, location, edit_message_id)
            return

    kw = find_keyword(lower_text, ["сгенерируй изображение", "сгенерируй картинку", "сгенерируй фото", "нарисуй мне",
                                   "нарисуй", "сделай картинку", "создай фото"])
    if kw:
        prompt = strip_keyword(clean_text, kw)
        if prompt:
            execute_image_generation(message, prompt, edit_message_id)
            return

    kw = find_keyword(lower_text, ["создай файл", "сделай файл", "сгенерируй файл", "создай документ",
                                   "сделай документ", "создай таблицу", "сделай таблицу"])
    if kw:
        prompt = strip_keyword(clean_text, kw)
        if prompt:
            execute_file_creation(message, prompt, edit_message_id)
            return

    # Озвучка — только если фраза в начале сообщения («расскажи» больше не срабатывает).
    for tts_kw in ("озвучь текст", "преврати в голос", "проговори", "озвучь", "скажи"):
        if lower_text.startswith(tts_kw + " ") or lower_text.startswith(tts_kw + ":"):
            prompt = clean_text[len(tts_kw):].strip(" :,.")
            if prompt:
                execute_tts(message, prompt, edit_message_id)
                return

    kw = find_keyword(lower_text, ["найди в интернете", "найди в инете", "поищи в интернете", "загугли"])
    if kw:
        query = strip_keyword(clean_text, kw)
        if query:
            execute_search(message, query)
            return

    if looks_like_code_request(clean_text):
        execute_code_request(message, clean_text, edit_message_id)
        return

    if edit_message_id:
        msg_id = edit_message_id
    else:
        msg_id = bot.reply_to(message, status_text(
            message.chat.id, "Думаю...", "Так, дай-ка подумать над твоим... вопросом.")).message_id
    reply = ask_ai_with_history(message.chat.id, clean_text)
    edit_or_send_long(message.chat.id, msg_id, reply)


@bot.message_handler(content_types=["text"])
def handle_text(message):
    if not is_addressed_to_bot(message):
        return
    text = message.text or ""

    if "http://" in text.lower() or "https://" in text.lower():
        msg = bot.reply_to(message, "🌐 Читаю ссылку...")
        try:
            urls = [w for w in text.split() if w.startswith("http")]
            res = requests.get(f"https://r.jina.ai/{urls[0]}", timeout=20)
            if res.status_code == 200 and res.text.strip():
                page_text = res.text[:8000]
            else:
                raise ValueError("Не удалось получить текст страницы.")
            extra = clean_bot_mentions(text.replace(urls[0], "")).strip()
            task = extra if len(extra) > 3 else "Сделай краткую выжимку этого текста."
            reply = ask_ai_with_history(
                message.chat.id,
                f"{task} Не используй Markdown. Вот текст страницы:\n\n{page_text}", store=False)
            edit_or_send_long(message.chat.id, msg.message_id, reply)
        except Exception as e:
            edit_or_send_long(message.chat.id, msg.message_id, f"Ошибка чтения ссылки: {e}")
        return

    process_natural_language_request(message, text)


@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    stats["users"].add(message.chat.id)
    if not is_addressed_to_bot(message):
        return
    process_image_message(message, message.photo[-1].file_id, message.caption)


@bot.message_handler(content_types=["document"])
def handle_doc(message):
    if not is_addressed_to_bot(message):
        return

    # Изображение, отправленное как файл (без сжатия) — тоже анализируем.
    mime = (message.document.mime_type or "").lower()
    if mime.startswith("image/"):
        process_image_message(message, message.document.file_id, message.caption)
        return

    msg = bot.reply_to(message, "Читаю документ...")
    path = None
    try:
        file_info = bot.get_file(message.document.file_id)
        file_data = bot.download_file(file_info.file_path)
        ext = os.path.splitext(message.document.file_name or "document.bin")[1]
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
            temp_file.write(file_data)
            path = temp_file.name

        text = extract_text_from_file(path)
        if not text.strip():
            raise ValueError("Документ пуст или текст не распознан.")

        caption = clean_bot_mentions((message.caption or "").strip())
        if caption:
            task = f"{caption}\n\nСодержимое файла {message.document.file_name}:\n\n{text[:20000]}"
            if looks_like_code_request(caption) or ext.lower() in (".py", ".js", ".ts", ".java", ".cpp", ".cs",
                                                                  ".go", ".rs", ".php", ".html", ".css"):
                execute_code_request(message, task, edit_message_id=msg.message_id)
                return
            reply = ask_ai_with_history(message.chat.id, task, store=False, max_tokens=4000)
        else:
            reply = ask_ai_with_history(
                message.chat.id,
                "Сделай краткую и понятную выжимку из этого документа. Не используй Markdown:\n\n" + text[:12000],
                store=False)
        edit_or_send_long(message.chat.id, msg.message_id, reply)
    except Exception as e:
        edit_or_send_long(message.chat.id, msg.message_id, f"Ошибка обработки документа: {e}")
    finally:
        if path and os.path.exists(path):
            os.remove(path)


if __name__ == "__main__":
    print("=" * 60 + "\n🚀 Бот запускается...\n" + "=" * 60)
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=reminder_loop, daemon=True).start()
    print("🤖 Telegram polling запущен")
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
        except Exception as e:
            print(f"⚠️ Ошибка polling: {e}")
        time.sleep(5)
