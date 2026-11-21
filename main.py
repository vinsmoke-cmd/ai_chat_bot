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
from docx.shared import Pt as DocxPt
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import registerFontFamily
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
HF_TOKEN = os.getenv("HF_TOKEN")            # необязательно: бесплатный токен Hugging Face
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()  # необязательно: ограничить /diag
DATA_DIR = os.getenv("DATA_DIR", ".")
TZ_OFFSET_HOURS = float(os.getenv("BOT_TZ_OFFSET", "5"))  # Ташкент = UTC+5

if not BOT_TOKEN:
    raise RuntimeError("❌ Не задан BOT_TOKEN")

bot = telebot.TeleBot(BOT_TOKEN)
ai_client = Client()

TELEGRAM_MESSAGE_LIMIT = 4096
AI_MAX_RESPONSE_LENGTH = 40000
INLINE_CODE_LIMIT = 3500

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

# ── Gemini: модели определяются автоматически (список доступных для ключа) ──
genai, genai_types, gemini_client = None, None, None
GEMINI_PRO, GEMINI_FLASH = [], []
DEAD_GEMINI = set()
DEAD_GROQ = set()
GEMINI_STATIC_PRO = ["gemini-2.5-pro"]
GEMINI_STATIC_FLASH = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]


def _model_rank(name):
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    ver = float(m.group(1)) if m else 0.0
    preview = 1 if re.search(r"preview|exp", name) else 0
    lite = 1 if "lite" in name else 0
    return (preview, lite, -ver, len(name))


def build_gemini_lists():
    """Запрашивает у API реальный список моделей и сортирует: стабильные и новые — первыми."""
    global GEMINI_PRO, GEMINI_FLASH
    found = []
    try:
        for m in gemini_client.models.list():
            name = (getattr(m, "name", "") or "").replace("models/", "")
            actions = getattr(m, "supported_actions", None) or []
            low = name.lower()
            if not low.startswith("gemini-"):
                continue
            if actions and "generateContent" not in actions:
                continue
            if any(x in low for x in ("image", "tts", "live", "embedding", "audio", "robotics",
                                      "computer", "learnlm", "native", "thinking-exp")):
                continue
            found.append(name)
    except Exception as e:
        print(f"⚠️ Не удалось получить список моделей Gemini: {e}")
    custom = os.getenv("GEMINI_MODEL", "").strip()
    pros = sorted([n for n in found if "pro" in n.lower()], key=_model_rank)[:3]
    flashes = sorted([n for n in found if "pro" not in n.lower()], key=_model_rank)[:4]
    for n in GEMINI_STATIC_PRO:
        if n not in pros:
            pros.append(n)
    for n in GEMINI_STATIC_FLASH:
        if n not in flashes:
            flashes.append(n)
    if custom:
        (pros if "pro" in custom.lower() else flashes).insert(0, custom)
    GEMINI_PRO = list(dict.fromkeys(pros))
    GEMINI_FLASH = list(dict.fromkeys(flashes))


if GEMINI_API_KEY:
    try:
        from google import genai
        from google.genai import types as genai_types

        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        build_gemini_lists()
        print(f"✅ Gemini pro: {GEMINI_PRO} | flash: {GEMINI_FLASH}")
    except Exception as e:
        print(f"⚠️ Gemini недоступен: {e}")
        genai, genai_types, gemini_client = None, None, None

user_histories = {}
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
#  ЕДИНЫЙ ВЫЗОВ LLM (всё бесплатное)
# ════════════════════════════════════════════════════════════════
CHAINS = {
    "chat": [("groq", "openai/gpt-oss-120b"), ("groq", "openai/gpt-oss-20b"), ("gemini", "flash"),
             ("groq", "llama-3.3-70b-versatile"), ("g4f", "auto")],
    # Лучшие модели: сначала Gemini Pro (если доступен на бесплатном ключе), затем GPT-OSS 120B.
    "best": [("gemini", "pro"), ("groq", "openai/gpt-oss-120b"), ("gemini", "flash"),
             ("groq", "qwen/qwen3-32b"), ("groq", "llama-3.3-70b-versatile"), ("g4f", "auto")],
    "fast": [("groq", "openai/gpt-oss-20b"), ("gemini", "flash"), ("g4f", "auto")],
}

TRANSIENT_MARKERS = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500", "INTERNAL",
                     "502", "504", "DEADLINE_EXCEEDED", "TIMEOUT", "TIMED OUT", "OVERLOADED")


def is_transient(err):
    s = str(err).upper()
    return any(m in s for m in TRANSIENT_MARKERS)


def short_err(e, n=140):
    return re.sub(r"\s+", " ", str(e))[:n]


def strip_think(text):
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()


def gemini_models(tier):
    if tier == "pro":
        return GEMINI_PRO
    if tier == "flash":
        return GEMINI_FLASH
    return GEMINI_FLASH + GEMINI_PRO


def gemini_call(contents, system=None, max_tokens=4096, temperature=0.7, tries=2, on_retry=None, tier="flash"):
    if not gemini_client:
        raise RuntimeError("Gemini не подключён (нет GEMINI_API_KEY)")
    errs = []
    for model in gemini_models(tier):
        if model in DEAD_GEMINI:
            continue
        attempts = 1 if "pro" in model.lower() else tries
        for attempt in range(attempts):
            try:
                cfg = genai_types.GenerateContentConfig(
                    system_instruction=system, max_output_tokens=max_tokens, temperature=temperature)
                resp = gemini_client.models.generate_content(model=model, contents=contents, config=cfg)
                text = (getattr(resp, "text", None) or "").strip()
                finish = "stop"
                try:
                    if "MAX_TOKENS" in str(resp.candidates[0].finish_reason).upper():
                        finish = "length"
                except Exception:
                    pass
                if not text:
                    errs.append(f"{model}: пустой ответ ({finish})")
                    break
                return text, finish
            except Exception as e:
                s = str(e).upper()
                errs.append(f"{model}: {short_err(e, 100)}")
                if "404" in s or "NOT_FOUND" in s or "LIMIT: 0" in s or "PERMISSION_DENIED" in s:
                    DEAD_GEMINI.add(model)
                    print(f"⚠️ Gemini {model} недоступна для этого ключа — исключаю")
                    break
                if is_transient(e) and attempt < attempts - 1:
                    delay = 3 * (attempt + 1)
                    print(f"⚠️ Gemini {model} временная ошибка, повтор через {delay}с")
                    if on_retry:
                        try:
                            on_retry(attempt + 2, attempts)
                        except Exception:
                            pass
                    time.sleep(delay)
                    continue
                break
    raise RuntimeError("Gemini: " + "; ".join(errs[-3:]) if errs else "Gemini: нет доступных моделей")


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
    """messages: первым идёт system. Возвращает (text, finish_reason)."""
    errors = []
    system = messages[0]["content"] if messages and messages[0]["role"] == "system" else None
    convo = messages[1:] if system is not None else messages
    gem_tokens = min(max(max_tokens * 2, 4096), 32768)

    for provider, model in CHAINS[chain]:
        try:
            if provider == "groq":
                if not groq_client or model in DEAD_GROQ:
                    continue
                print(f"🔄 Groq → {model}")
                text, finish = _groq_call(model, messages, max_tokens, timeout, temperature, effort)
            elif provider == "gemini":
                if not gemini_client:
                    continue
                print(f"🔄 Gemini ({model})")
                contents = [
                    genai_types.Content(
                        role="model" if m["role"] == "assistant" else "user",
                        parts=[genai_types.Part.from_text(text=m["content"])])
                    for m in convo
                ]
                text, finish = gemini_call(contents, system=system, max_tokens=gem_tokens,
                                           temperature=temperature, tier=model)
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
            print(f"❌ {provider}/{model}: {short_err(e, 300)}")
            errors.append(f"{provider}/{model}: {short_err(e, 100)}")
            low = str(e).lower()
            if provider == "groq" and "model" in low and any(
                    k in low for k in ("decommission", "does not exist", "not_found", "not found")):
                DEAD_GROQ.add(model)
    raise RuntimeError("Все модели недоступны: " + " | ".join(errors[-4:]))


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

PLAN_SYSTEM = (
    "Ты архитектор программного обеспечения уровня staff engineer. Составь подробный технический план реализации "
    "задачи. Без кода. Структура: 1) цель и ключевые требования (явные и неявные); 2) полный список функций, "
    "включая уместные дополнительные возможности, которых ждёт опытный пользователь; 3) структура: файлы, модули, "
    "классы, функции; 4) зависимости (только проверенные, существующие библиотеки); 5) граничные случаи и обработка "
    "ошибок; 6) UX/интерфейс; 7) что проверить. До 450 слов. Язык — как у пользователя."
)

CODE_SYSTEM = (
    "Ты ведущий программист уровня staff engineer. Пиши код качества production, как лучшие модели мира.\n"
    "ПРАВИЛА:\n"
    "1. Реализуй задачу полностью и с запасом: не минимальный пример, а законченный продукт с продуманной "
    "архитектурой. Не экономь на объёме: небольшая утилита — обычно 150+ строк, серьёзный проект — 400-1000+ строк.\n"
    "2. Запрещены заглушки: TODO, pass вместо логики, 'остальной код аналогично', многоточия вместо кода. "
    "Все импорты, константы и вспомогательные функции на месте.\n"
    "3. Качество: чёткая структура, понятные имена, типизация и docstring там, где это уместно, обработка ошибок и "
    "граничных случаев, валидация входных данных, логирование, конфигурация через константы или переменные окружения.\n"
    "4. Добавляй то, что опытный пользователь ждёт по умолчанию, даже если не просил: удобный интерфейс или CLI, "
    "понятные сообщения об ошибках, безопасные значения по умолчанию.\n"
    "5. Для веб-страниц: современный, красивый, адаптивный дизайн, аккуратная типографика, плавные анимации, "
    "доступность, всё в одном файле, если не сказано иное.\n"
    "6. Если просят исправить или изменить существующий код — верни ПОЛНЫЙ обновлённый файл целиком, не diff.\n"
    "7. Каждый файл — в отдельном блоке из трёх обратных кавычек с языком. Если файлов несколько, перед каждым "
    "блоком напиши строку с именем файла.\n"
    "8. После кода — краткое пояснение (до 8 строк): как запустить, какие библиотеки установить, что умеет. "
    "Пояснение на языке пользователя, без Markdown-разметки вне блоков кода.\n"
    "9. Используй только реально существующие API и библиотеки; не выдумывай функции."
)

REVIEW_PROMPT = (
    "Проведи строгое ревью своего решения как требовательный senior-инженер. Проверь: соответствие ВСЕМ требованиям "
    "задачи, баги, пропущенные граничные случаи, обработку ошибок, безопасность, недоделки и места, где решение "
    "выглядит сырым или слишком коротким, несуществующие API. Затем верни ПОЛНЫЙ улучшенный итоговый ответ целиком: "
    "весь код всех файлов (не фрагменты), доработанный и расширенный там, где нужно, с тем же форматом блоков и "
    "кратким пояснением. Если всё идеально — всё равно верни полный итоговый код."
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
#  ГЕНЕРАЦИЯ КОДА: план → написание → ревью и доработка → проверка
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


def code_size(text):
    return sum(len(p["content"]) for p in extract_code_blocks(text) if p["type"] == "code")


def find_code_errors(text):
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


def _write_full(messages, progress, label):
    """Один полный проход генерации с автодописыванием при обрыве."""
    text, finish = call_llm(messages, chain="best", max_tokens=8000, timeout=170, temperature=0.25, effort="high")
    rounds = 0
    while finish == "length" and rounds < 4:
        rounds += 1
        progress(f"{label} (часть {rounds + 1}, код длинный)...")
        cont = messages + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": "Продолжи строго с места обрыва. Не повторяй уже написанное и не пиши "
                                        "вступлений. Если обрыв внутри блока кода — продолжай код без нового "
                                        "открывающего ```."},
        ]
        more, finish = call_llm(cont, chain="best", max_tokens=8000, timeout=170, temperature=0.25, effort="medium")
        text = merge_continuation(text, more)
    return text, finish


def generate_code(user_id, task, progress=None, use_history=True):
    progress = progress or (lambda t: None)
    system = CODE_SYSTEM
    if is_ham(user_id):
        system += ("\n10. Короткое пояснение после кода напиши в саркастичном стиле Нейрохама (без мата), "
                   "но сам код должен быть безупречным и без шуток внутри.")
    hist = history_tail(user_id, 6) if use_history else []

    # 1) План (пропускаем, если пользователь прислал готовый код на доработку)
    plan = ""
    if "```" not in task and len(task) < 6000:
        progress("🧠 Продумываю архитектуру и план...")
        try:
            plan, _ = call_llm(
                [{"role": "system", "content": PLAN_SYSTEM}, {"role": "user", "content": task}],
                chain="best", max_tokens=1800, timeout=90, temperature=0.3, effort="medium")
        except Exception as e:
            print(f"⚠️ План не получен: {e}")

    user_msg = task
    if plan:
        user_msg = (f"{task}\n\nТЕХНИЧЕСКИЙ ПЛАН (реализуй полностью и без сокращений):\n{plan}\n\n"
                    "Требования к качеству: уровень production, объём — столько, сколько нужно для полной "
                    "реализации плана; не урезай решение ради краткости.")
    base = [{"role": "system", "content": system}] + hist + [{"role": "user", "content": user_msg}]

    # 2) Написание
    progress("💻 Пишу код. Это может занять несколько минут...")
    text, finish = _write_full(base, progress, "💻 Дописываю")

    # 3) Ревью и доработка
    progress("🔍 Делаю ревью и довожу до ума...")
    try:
        review_msgs = base + [{"role": "assistant", "content": text}, {"role": "user", "content": REVIEW_PROMPT}]
        improved, fin2 = _write_full(review_msgs, progress, "🔍 Дописываю доработку")
        if (fin2 == "stop" and not find_code_errors(improved)
                and code_size(improved) >= 0.85 * max(code_size(text), 1)):
            text = improved
        else:
            print("ℹ️ Доработка отклонена (обрыв/ошибка/сокращение) — оставляю первую версию")
    except Exception as e:
        print(f"⚠️ Ревью не удалось: {e}")

    # 4) Проверка синтаксиса и автоисправление
    for attempt in range(2):
        errors = find_code_errors(text)
        if not errors:
            break
        progress(f"🔧 Исправляю ошибки (попытка {attempt + 1}/2)...")
        fix = base + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": f"В твоём коде найдены ошибки:\n{errors}\n\nВерни ПОЛНЫЙ исправленный "
                                        "ответ целиком (все файлы полностью) в том же формате."},
        ]
        try:
            new_text, fin = _write_full(fix, progress, "🔧 Дописываю исправление")
        except Exception as e:
            print(f"⚠️ Автоисправление не удалось: {e}")
            break
        if fin == "length":
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
        edit_or_send_long(chat_id, msg_id, f"Не удалось сгенерировать код: {short_err(e, 300)}\n"
                                           "Попробуй ещё раз через минуту.")


def main_code_from(text):
    blocks = [p for p in extract_code_blocks(text) if p["type"] == "code"]
    if not blocks:
        return strip_code_fence(text)
    return max(blocks, key=lambda p: len(p["content"]))["content"]


# ════════════════════════════════════════════════════════════════
#  АНАЛИЗ ИЗОБРАЖЕНИЙ (Gemini → Groq vision → Pollinations vision)
# ════════════════════════════════════════════════════════════════
def normalize_image(data):
    """Любое изображение → аккуратный JPEG (EXIF-поворот, до 1920px, <3 МБ)."""
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


_groq_vision_cache = {"models": None}


def groq_vision_models():
    if _groq_vision_cache["models"] is not None:
        return _groq_vision_cache["models"]
    names = [m.strip() for m in os.getenv("GROQ_VISION_MODELS", "").split(",") if m.strip()]
    try:
        ids = [m.id for m in groq_client.models.list().data]
        names += [i for i in ids if re.search(r"llama-4|vision|-vl|scout|maverick", i.lower())]
    except Exception as e:
        print(f"⚠️ Не удалось получить список моделей Groq: {e}")
    names += ["meta-llama/llama-4-scout-17b-16e-instruct", "meta-llama/llama-4-maverick-17b-128e-instruct"]
    _groq_vision_cache["models"] = list(dict.fromkeys(names))
    return _groq_vision_cache["models"]


def vision_gemini(img_bytes, mime, system, prompt, progress=None):
    def on_retry(attempt, total):
        if progress:
            progress(f"Сервер распознавания перегружен. Повторяю запрос... ({attempt}/{total})")
    text, _ = gemini_call([genai_types.Part.from_bytes(data=img_bytes, mime_type=mime), prompt],
                          system=system, max_tokens=8192, temperature=0.4, tries=3,
                          on_retry=on_retry, tier="all")
    return text


def vision_groq(img_bytes, mime, system, prompt, progress=None):
    if not groq_client:
        raise RuntimeError("нет GROQ_API_KEY")
    b64 = base64.b64encode(img_bytes).decode()
    errs = []
    for model in groq_vision_models():
        if model in DEAD_GROQ:
            continue
        try:
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
            errs.append(f"{model}: пусто")
        except Exception as e:
            errs.append(f"{model}: {short_err(e, 90)}")
            low = str(e).lower()
            if "decommission" in low or "does not exist" in low or "not_found" in low:
                DEAD_GROQ.add(model)
    raise RuntimeError("; ".join(errs[-2:]) or "нет моделей")


def vision_pollinations(img_bytes, mime, system, prompt, progress=None):
    b64 = base64.b64encode(img_bytes).decode()
    r = requests.post(
        "https://text.pollinations.ai/openai",
        json={"model": "openai", "max_tokens": 2000, "referrer": "telegram-bot",
              "messages": [
                  {"role": "system", "content": system},
                  {"role": "user", "content": [
                      {"type": "text", "text": prompt},
                      {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}]},
        timeout=80)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:100]}")
    text = (r.json()["choices"][0]["message"]["content"] or "").strip()
    if not text:
        raise RuntimeError("пустой ответ")
    return text


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

    providers = []
    if gemini_client:
        providers.append(("Gemini", vision_gemini))
    if groq_client:
        providers.append(("Groq", vision_groq))
    providers.append(("Pollinations", vision_pollinations))

    errors = []
    for name, fn in providers:
        try:
            if progress and errors:
                progress(f"Пробую запасной вариант распознавания ({name})...")
            return fn(img_bytes, mime, system, prompt, progress)
        except Exception as e:
            print(f"⚠️ Vision {name}: {short_err(e, 300)}")
            errors.append(f"{name}: {short_err(e, 110)}")
    raise RuntimeError(" | ".join(errors))


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
                          f"❌ Не удалось проанализировать изображение.\nПричина: {short_err(e, 400)}\n"
                          "Команда /diag покажет, какой сервис не работает.")


# ════════════════════════════════════════════════════════════════
#  ФАЙЛЫ: чтение
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
    if any(w in text for w in ["word", "ворд", "docx", "документ word"]):
        return "docx"
    if any(w in text for w in ["pdf", "пдф"]):
        return "pdf"
    if any(w in text for w in ["pptx", "powerpoint", "презентац", "слайд"]):
        return "pptx"
    if "csv" in text:
        return "csv"
    if "json" in text:
        return "json"
    if any(w in text for w in [".py", "python файл", "python скрипт", "питон"]):
        return "py"
    if any(w in text for w in ["html", "веб-страниц", "веб страниц", "сайт", "лендинг"]):
        return "html"
    if "markdown" in text or ".md" in text:
        return "md"
    return "txt"


def strip_code_fence(content):
    content = (content or "").strip()
    content = re.sub(r"^```[a-zA-Z0-9_+#.-]*[ \t]*\n", "", content)
    content = re.sub(r"\n```\s*$", "", content)
    return content.strip()


def parse_json_loose(text):
    text = strip_code_fence(text)
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return None


# ── Парсер «Markdown-подобного» текста для DOCX/PDF ──
def parse_blocks(content):
    blocks, lines, i = [], strip_code_fence(content).splitlines(), 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if s.startswith("|") and s.count("|") >= 2:
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                row = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not (row and all(re.fullmatch(r":?-+:?", c) for c in row if c) and any(row)):
                    rows.append(row)
                i += 1
            if rows:
                blocks.append(("table", rows))
            continue
        m = re.match(r"^(#{1,3})\s+(.*)", s)
        if m:
            blocks.append((f"h{len(m.group(1))}", m.group(2).strip()))
        elif re.match(r"^[-*•]\s+", s):
            blocks.append(("bullet", re.sub(r"^[-*•]\s+", "", s)))
        elif re.match(r"^\d+[.)]\s+", s):
            blocks.append(("num", re.sub(r"^\d+[.)]\s+", "", s)))
        elif re.fullmatch(r"[-_=*]{3,}", s):
            pass
        else:
            blocks.append(("p", s))
        i += 1
    return blocks


def _add_inline(paragraph, text):
    for idx, chunk in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
        if chunk:
            run = paragraph.add_run(chunk)
            run.bold = (idx % 2 == 1)


def build_docx(content, path):
    document = docx.Document()
    base = document.styles["Normal"]
    base.font.name = "Calibri"
    base.font.size = DocxPt(11)
    first_h1 = True
    for kind, val in parse_blocks(content):
        if kind in ("h1", "h2", "h3"):
            level = int(kind[1])
            if kind == "h1" and first_h1:
                document.add_heading(re.sub(r"\*\*", "", val), level=0)
                first_h1 = False
            else:
                document.add_heading(re.sub(r"\*\*", "", val), level=level)
        elif kind == "bullet":
            _add_inline(document.add_paragraph(style="List Bullet"), val)
        elif kind == "num":
            _add_inline(document.add_paragraph(style="List Number"), val)
        elif kind == "table":
            cols = max(len(r) for r in val)
            table = document.add_table(rows=len(val), cols=cols)
            try:
                table.style = "Light Grid Accent 1"
            except Exception:
                table.style = "Table Grid"
            for ri, row in enumerate(val):
                for ci in range(cols):
                    cell = table.cell(ri, ci)
                    cell.text = ""
                    para = cell.paragraphs[0]
                    _add_inline(para, row[ci] if ci < len(row) else "")
                    if ri == 0:
                        for run in para.runs:
                            run.bold = True
            document.add_paragraph("")
        else:
            _add_inline(document.add_paragraph(), val)
    document.save(path)


def get_cyrillic_font(bold=False):
    fname = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    if os.path.exists(fname):
        return fname
    candidates = (["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
                   "C:\\Windows\\Fonts\\arialbd.ttf"] if bold else
                  ["C:\\Windows\\Fonts\\arial.ttf", "C:\\Windows\\Fonts\\calibri.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf"])
    for p in candidates:
        if os.path.exists(p):
            return p
    urls = [f"https://raw.githubusercontent.com/dejavu-fonts/dejavu-fonts/master/ttf/{fname}"]
    if not bold:
        urls.append("https://cdnjs.cloudflare.com/ajax/libs/pdfmake/0.1.66/fonts/Roboto/Roboto-Regular.ttf")
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as resp, open(fname, 'wb') as f:
                f.write(resp.read())
            return fname
        except Exception as e:
            print(f"⚠️ Ошибка загрузки шрифта {url}: {e}")
    return None


def build_pdf(content, path):
    regular = get_cyrillic_font()
    bold_file = get_cyrillic_font(bold=True)
    font, font_bold = "Helvetica", "Helvetica-Bold"
    if regular:
        try:
            pdfmetrics.registerFont(TTFont("CyrFont", regular))
            font = font_bold = "CyrFont"
            if bold_file:
                pdfmetrics.registerFont(TTFont("CyrFontBold", bold_file))
                font_bold = "CyrFontBold"
            registerFontFamily("CyrFont", normal="CyrFont", bold=font_bold, italic="CyrFont", boldItalic=font_bold)
        except Exception as e:
            print(f"⚠️ Ошибка регистрации шрифта: {e}")
            font, font_bold = "Helvetica", "Helvetica-Bold"

    styles = getSampleStyleSheet()
    normal = ParagraphStyle("N", parent=styles["Normal"], fontName=font, fontSize=10.5, leading=15, spaceAfter=6)
    h1 = ParagraphStyle("H1", parent=normal, fontName=font_bold, fontSize=22, leading=27, spaceAfter=12,
                        textColor=colors.HexColor("#1F2A44"))
    h2 = ParagraphStyle("H2", parent=normal, fontName=font_bold, fontSize=15, leading=20, spaceBefore=10,
                        spaceAfter=6, textColor=colors.HexColor("#2E5C9A"))
    h3 = ParagraphStyle("H3", parent=normal, fontName=font_bold, fontSize=12, leading=16, spaceBefore=6,
                        spaceAfter=4)
    bullet = ParagraphStyle("B", parent=normal, leftIndent=16, bulletIndent=4, spaceAfter=3)

    def rl(text):
        return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(text, quote=False))

    story, num = [], 0
    for kind, val in parse_blocks(content):
        if kind != "num":
            num = 0
        if kind == "h1":
            story.append(Paragraph(rl(val), h1))
        elif kind == "h2":
            story.append(Paragraph(rl(val), h2))
        elif kind == "h3":
            story.append(Paragraph(rl(val), h3))
        elif kind == "bullet":
            story.append(Paragraph(rl(val), bullet, bulletText="•"))
        elif kind == "num":
            num += 1
            story.append(Paragraph(rl(val), bullet, bulletText=f"{num}."))
        elif kind == "table":
            cols = max(len(r) for r in val)
            cell_style = ParagraphStyle("C", parent=normal, fontSize=9, leading=12, spaceAfter=0)
            head_style = ParagraphStyle("CH", parent=cell_style, fontName=font_bold, textColor=colors.white)
            data = [[Paragraph(rl(r[c] if c < len(r) else ""), head_style if ri == 0 else cell_style)
                     for c in range(cols)] for ri, r in enumerate(val)]
            table = Table(data, repeatRows=1, colWidths=[(A4[0] - 80) / cols] * cols)
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2E5C9A")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B8C4D6")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F5FA")]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.append(table)
            story.append(Spacer(1, 10))
        else:
            story.append(Paragraph(rl(val), normal))
    if not story:
        story.append(Paragraph("Пустой документ", normal))
    SimpleDocTemplate(path, pagesize=A4, leftMargin=40, rightMargin=40, topMargin=40, bottomMargin=40).build(story)


# ── PPTX с дизайном ──
def build_pptx(spec, path):
    DARK = RGBColor(0x1B, 0x26, 0x3B)
    ACCENT = RGBColor(0x2E, 0x86, 0xDE)
    LIGHT = RGBColor(0xF4, 0xF6, 0xFA)
    TEXT = RGBColor(0x22, 0x2B, 0x3A)
    WHITE = RGBColor(0xFF, 0xFF, 0xFF)

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]

    def rect(slide, l, t, w, h, color):
        shp = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(l), Inches(t), Inches(w), Inches(h))
        shp.fill.solid()
        shp.fill.fore_color.rgb = color
        shp.line.fill.background()
        return shp

    def textbox(slide, l, t, w, h, anchor=MSO_ANCHOR.TOP):
        tb = slide.shapes.add_textbox(Inches(l), Inches(t), Inches(w), Inches(h))
        tf = tb.text_frame
        tf.word_wrap = True
        tf.vertical_anchor = anchor
        return tf

    title = str(spec.get("title") or "Презентация")
    subtitle = str(spec.get("subtitle") or "")
    s = prs.slides.add_slide(blank)
    rect(s, 0, 0, 13.333, 7.5, DARK)
    rect(s, 0.8, 3.05, 1.6, 0.09, ACCENT)
    tf = textbox(s, 0.8, 1.3, 11.5, 1.7, MSO_ANCHOR.BOTTOM)
    tf.text = title
    tf.paragraphs[0].runs[0].font.size = Pt(46)
    tf.paragraphs[0].runs[0].font.bold = True
    tf.paragraphs[0].runs[0].font.color.rgb = WHITE
    if subtitle:
        tf2 = textbox(s, 0.8, 3.3, 11.5, 1.5)
        tf2.text = subtitle
        tf2.paragraphs[0].runs[0].font.size = Pt(22)
        tf2.paragraphs[0].runs[0].font.color.rgb = RGBColor(0xB8, 0xC7, 0xE0)

    for idx, sl in enumerate(spec.get("slides", []), 1):
        s = prs.slides.add_slide(blank)
        rect(s, 0, 0, 13.333, 7.5, LIGHT)
        rect(s, 0, 0, 13.333, 1.35, DARK)
        rect(s, 0, 1.35, 13.333, 0.07, ACCENT)
        tft = textbox(s, 0.7, 0.15, 11.4, 1.1, MSO_ANCHOR.MIDDLE)
        tft.text = str(sl.get("title", ""))[:90]
        tft.paragraphs[0].runs[0].font.size = Pt(32)
        tft.paragraphs[0].runs[0].font.bold = True
        tft.paragraphs[0].runs[0].font.color.rgb = WHITE
        bullets = [str(b) for b in (sl.get("bullets") or [])][:8]
        size = 24 if len(bullets) <= 4 else 20 if len(bullets) <= 6 else 17
        tfb = textbox(s, 0.9, 1.9, 11.6, 5.0)
        for bi, b in enumerate(bullets):
            p = tfb.paragraphs[0] if bi == 0 else tfb.add_paragraph()
            run = p.add_run()
            run.text = "•  " + b
            run.font.size = Pt(size)
            run.font.color.rgb = TEXT
            p.space_after = Pt(12)
        tfn = textbox(s, 12.2, 6.9, 0.9, 0.4)
        tfn.text = str(idx)
        tfn.paragraphs[0].runs[0].font.size = Pt(12)
        tfn.paragraphs[0].runs[0].font.color.rgb = ACCENT
        if sl.get("notes"):
            s.notes_slide.notes_text_frame.text = str(sl["notes"])
    prs.save(path)


def spec_from_text(content, request):
    """Запасной вариант: превращает обычный текст в структуру слайдов."""
    spec = {"title": str(request)[:80], "subtitle": "", "slides": []}
    cur = None
    for line in content.splitlines():
        st = line.strip()
        if not st:
            continue
        m = re.match(r"^(?:СЛАЙД\s*\d*\s*:|#{1,3}\s+)\s*(.*)", st, flags=re.IGNORECASE)
        if m:
            cur = {"title": m.group(1) or "Слайд", "bullets": []}
            spec["slides"].append(cur)
        else:
            if cur is None:
                cur = {"title": "Обзор", "bullets": []}
                spec["slides"].append(cur)
            cur["bullets"].append(re.sub(r"^[-*•\d.)\s]+", "", st))
    return spec


# ── XLSX с оформлением ──
def _conv_cell(v):
    v = v.strip()
    if v.startswith("="):
        return v
    if re.fullmatch(r"-?(0|[1-9]\d*)([.,]\d+)?", v.replace(" ", "")):
        n = v.replace(" ", "").replace(",", ".")
        return float(n) if "." in n else int(n)
    return v


def build_xlsx(content, path):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    sections, cur_title, cur_rows = [], "Данные", []
    for line in strip_code_fence(content).splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^#{1,3}\s*(?:Лист:?\s*)?(.+)$", s)
        if m:
            if cur_rows:
                sections.append((cur_title, cur_rows))
            cur_title, cur_rows = m.group(1).strip(), []
            continue
        if re.fullmatch(r"[|\-:\s]+", s):
            continue
        if "|" in s:
            cells = [c.strip() for c in s.strip("|").split("|")]
        elif "\t" in s:
            cells = [c.strip() for c in s.split("\t")]
        elif ";" in s:
            cells = [c.strip() for c in s.split(";")]
        else:
            cells = [s]
        cur_rows.append(cells)
    if cur_rows:
        sections.append((cur_title, cur_rows))
    if not sections:
        sections = [("Данные", [["Пусто"]])]

    thin = Side(style="thin", color="B8C4D6")
    for title, rows in sections:
        ws = wb.create_sheet(re.sub(r"[\[\]:*?/\\]", "", title)[:30] or "Лист")
        for r_i, row in enumerate(rows, 1):
            for c_i, val in enumerate(row, 1):
                cell = ws.cell(row=r_i, column=c_i, value=_conv_cell(val))
                cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)
                cell.alignment = Alignment(vertical="center", wrap_text=True)
                if r_i == 1:
                    cell.font = Font(bold=True, color="FFFFFF")
                    cell.fill = PatternFill("solid", fgColor="2E5C9A")
        for c_i in range(1, max(len(r) for r in rows) + 1):
            longest = max((len(str(r[c_i - 1])) for r in rows if len(r) >= c_i), default=8)
            ws.column_dimensions[get_column_letter(c_i)].width = min(60, max(10, longest + 3))
        ws.freeze_panes = "A2"
        if len(rows) > 1:
            ws.auto_filter.ref = ws.dimensions
    wb.save(path)


def make_multiplication_table_xlsx(output_path):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Таблица умножения"
    sheet.cell(row=1, column=1, value="×")
    head_fill = PatternFill("solid", fgColor="2E5C9A")
    for number in range(1, 11):
        for cell in (sheet.cell(row=1, column=number + 1, value=number),
                     sheet.cell(row=number + 1, column=1, value=number)):
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = head_fill
    sheet["A1"].fill = head_fill
    sheet["A1"].font = Font(bold=True, color="FFFFFF")
    for row in range(1, 11):
        for column in range(1, 11):
            sheet.cell(row=row + 1, column=column + 1, value=row * column)
    for column in range(1, 12):
        sheet.column_dimensions[get_column_letter(column)].width = 8
    workbook.save(output_path)


# ── Генерация содержимого ──
DOC_SYSTEM = (
    "Ты профессиональный автор, аналитик и редактор уровня лучших консультантов. Создаёшь глубокие, структурные, "
    "конкретные документы без воды: чёткие заголовки, логика, примеры, цифры и факты там, где они уместны. "
    "Выводи только содержимое документа, без вступлений и комментариев."
)


def generate_ai_file_content(request, extension, user_id):
    # Код и веб-страницы — через полный конвейер (план → написание → ревью → проверка).
    if extension in ("py", "html"):
        task = (f"{request}\n\nВерни ОДИН полный файл в одном блоке кода (расширение .{extension}). "
                "Решение должно быть большим, продуманным и законченным.")
        return main_code_from(generate_code(user_id, task, use_history=False))

    if extension == "pptx":
        prompt = (
            f"Запрос пользователя: {request}\n\nСоздай содержательную презентацию на 9-12 слайдов: титульный, "
            "структурные слайды с конкретными фактами, цифрами и примерами, итоговый слайд. "
            "Верни ТОЛЬКО JSON без пояснений такого вида:\n"
            '{"title": "...", "subtitle": "...", "slides": [{"title": "...", "bullets": ["3-6 ёмких тезисов"], '
            '"notes": "текст выступления 2-4 предложения"}]}')
        text, _ = call_llm([{"role": "system", "content": DOC_SYSTEM}, {"role": "user", "content": prompt}],
                           chain="best", max_tokens=6000, timeout=120, temperature=0.5, effort="medium")
        return text

    if extension == "xlsx":
        prompt = (
            f"Запрос пользователя: {request}\n\nСоздай полезную, продуманную таблицу. Первая строка — заголовки. "
            "Колонки разделяй символом |. Если нужно несколько листов — начинай каждый со строки '## Лист: Название'. "
            "Числа пиши без единиц измерения в ячейке (единицы — в заголовке). Где уместно, используй формулы "
            "Excel вида =SUM(B2:B10) (строка, начинающаяся с =). Данные реалистичные и достаточно подробные "
            "(не менее 10-15 строк, если уместно). Без пояснений.")
        text, _ = call_llm([{"role": "system", "content": DOC_SYSTEM}, {"role": "user", "content": prompt}],
                           chain="best", max_tokens=5000, timeout=120, temperature=0.4, effort="medium")
        return text

    if extension in ("docx", "pdf", "md", "txt"):
        prompt = (
            f"Запрос пользователя: {request}\n\nНапиши полноценный документ. Используй Markdown-структуру: "
            "# Заголовок документа, ## разделы, ### подразделы, списки (- пункт), нумерованные списки, таблицы в "
            "формате Markdown (| a | b |) где уместно, **жирный** для ключевого. Объём соответствует задаче и "
            "не поверхностный: для отчёта, статьи, реферата, плана, инструкции — не менее 1000 слов; "
            "для резюме, письма, договора — по стандартам жанра, но полно и профессионально.")
        text, finish = call_llm([{"role": "system", "content": DOC_SYSTEM}, {"role": "user", "content": prompt}],
                                chain="best", max_tokens=8000, timeout=170, temperature=0.5, effort="medium")
        rounds = 0
        while finish == "length" and rounds < 2:
            rounds += 1
            more, finish = call_llm(
                [{"role": "system", "content": DOC_SYSTEM}, {"role": "user", "content": prompt},
                 {"role": "assistant", "content": text},
                 {"role": "user", "content": "Продолжи строго с места обрыва, не повторяя написанное. "
                                             "Заверши документ."}],
                chain="best", max_tokens=8000, timeout=170, temperature=0.5, effort="medium")
            text += "\n" + more
        return text

    instructions = {
        "csv": "Создай данные CSV. Колонки разделяй точкой с запятой. Первая строка — заголовки. Только данные.",
        "json": "Создай корректный JSON. Ответ должен содержать только JSON.",
    }
    prompt = f"Запрос: {request}\n\n{instructions.get(extension, 'Создай текст.')} Без пояснений."
    text, _ = call_llm([{"role": "system", "content": DOC_SYSTEM}, {"role": "user", "content": prompt}],
                       chain="chat", max_tokens=4000, timeout=90, temperature=0.4)
    return text


def create_generated_file(request, user_id):
    extension = detect_file_format(request)
    temp_directory = tempfile.mkdtemp(prefix="telegram_generated_")
    if extension == "xlsx" and "умножени" in str(request).lower():
        filename = safe_filename("Таблица_умножения", "xlsx")
        path = os.path.join(temp_directory, filename)
        make_multiplication_table_xlsx(path)
        stats["files_generated"] += 1
        return path, filename, extension

    content = generate_ai_file_content(request, extension, user_id)
    filename = safe_filename(str(request).strip().replace("/", " ")[:45], extension)
    path = os.path.join(temp_directory, filename)

    if extension == "xlsx":
        build_xlsx(content, path)
    elif extension == "docx":
        build_docx(content, path)
    elif extension == "pdf":
        build_pdf(content, path)
    elif extension == "pptx":
        spec = parse_json_loose(content)
        if not isinstance(spec, dict) or not spec.get("slides"):
            spec = spec_from_text(content, request)
        build_pptx(spec, path)
    elif extension == "csv":
        with open(path, "w", encoding="utf-8-sig", newline="") as file:
            writer = csv.writer(file, delimiter=";")
            for line in strip_code_fence(content).splitlines():
                if line.strip():
                    writer.writerow([c.strip() for c in line.split(";")])
    elif extension == "json":
        data = parse_json_loose(content)
        if data is None:
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
    if "```" in after:
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
#  ПОГОДА, ПОИСК
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
    text, _ = gemini_call(str(prompt), max_tokens=8192, tier="all")
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


# ════════════════════════════════════════════════════════════════
#  ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ: Pollinations → Hugging Face (если есть токен) → AI Horde
# ════════════════════════════════════════════════════════════════
def enhance_image_prompt(user_prompt):
    try:
        sys_prompt = (
            "You are an expert AI image prompt engineer. Translate the request to English if needed and expand it "
            "into a detailed, vivid image description: subject, lighting, composition, style, textures. Keep the "
            "original subject exactly. Return ONLY the prompt as one line, max 60 words, no quotes.")
        text, _ = call_llm([{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
                           chain="fast", max_tokens=300, timeout=20, temperature=0.8, effort="low")
        text = " ".join(text.split()).strip('"')
        return text[:500] if text else user_prompt
    except Exception as e:
        print(f"⚠️ Ошибка улучшения промпта: {e}")
        return user_prompt


def is_image_bytes(b):
    return bool(b) and (b[:3] == b"\xff\xd8\xff" or b[:8] == b"\x89PNG\r\n\x1a\n"
                        or (b[:4] == b"RIFF" and b[8:12] == b"WEBP") or b[:6] in (b"GIF87a", b"GIF89a"))


def to_sendable_image(b):
    """WEBP → PNG для Telegram."""
    if b[:4] == b"RIFF" and Image:
        try:
            img = Image.open(io.BytesIO(b)).convert("RGB")
            out = io.BytesIO()
            img.save(out, "PNG")
            return out.getvalue()
        except Exception:
            pass
    return b


def gen_pollinations(prompt, w, h, budget=100):
    errs, start = [], time.time()
    endpoints = ["https://image.pollinations.ai/prompt/", "https://pollinations.ai/p/"]
    key = os.getenv("POLLINATIONS_API_KEY")
    encoded = urllib.parse.quote(prompt, safe="")
    for base in endpoints:
        for model in ("flux", "turbo", None):
            if time.time() - start > budget:
                raise RuntimeError("Pollinations: " + "; ".join(errs[-3:]))
            params = {"width": w, "height": h, "seed": secrets.randbelow(10**7), "nologo": "true",
                      "private": "true", "referrer": "telegram-bot"}
            if model:
                params["model"] = model
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            for attempt in range(2):
                try:
                    r = requests.get(base + encoded, params=params, headers=headers, timeout=(10, 55))
                    if r.status_code == 200 and is_image_bytes(r.content):
                        return r.content
                    errs.append(f"{model or 'default'}: HTTP {r.status_code}")
                    if r.status_code in (429, 502, 503) and attempt == 0:
                        time.sleep(4)
                        continue
                except requests.exceptions.Timeout:
                    errs.append(f"{model or 'default'}: таймаут")
                except Exception as e:
                    errs.append(f"{model or 'default'}: {short_err(e, 60)}")
                break
    raise RuntimeError("Pollinations: " + "; ".join(errs[-3:]))


def gen_huggingface(prompt, w, h):
    if not HF_TOKEN:
        raise RuntimeError("HF_TOKEN не задан")
    errs = []
    for model in ("black-forest-labs/FLUX.1-schnell", "stabilityai/stable-diffusion-xl-base-1.0"):
        try:
            r = requests.post(f"https://router.huggingface.co/hf-inference/models/{model}",
                              headers={"Authorization": f"Bearer {HF_TOKEN}"},
                              json={"inputs": prompt, "parameters": {"width": min(w, 1024), "height": min(h, 1024)}},
                              timeout=90)
            if r.status_code == 200 and is_image_bytes(r.content):
                return r.content
            errs.append(f"{model.split('/')[-1]}: HTTP {r.status_code}")
        except Exception as e:
            errs.append(f"{model.split('/')[-1]}: {short_err(e, 60)}")
    raise RuntimeError("HuggingFace: " + "; ".join(errs))


def gen_horde(prompt, w, h, progress=None, budget=200):
    """AI Horde — бесплатно и без регистрации (анонимный ключ), но может занять 30-120 секунд."""
    hw, hh = (640, 384) if w > h else (384, 640) if h > w else (512, 512)
    headers = {"apikey": os.getenv("HORDE_API_KEY", "0000000000"), "Client-Agent": "tgbot:3.0:none"}
    r = requests.post("https://aihorde.net/api/v2/generate/async", headers=headers, timeout=25, json={
        "prompt": prompt[:900], "nsfw": False, "censor_nsfw": True, "r2": True,
        "params": {"width": hw, "height": hh, "steps": 25, "n": 1, "cfg_scale": 7, "sampler_name": "k_euler_a"}})
    if r.status_code not in (200, 202):
        raise RuntimeError(f"Horde: HTTP {r.status_code} {r.text[:100]}")
    job = r.json().get("id")
    if not job:
        raise RuntimeError("Horde: нет id задачи")
    start = time.time()
    while time.time() - start < budget:
        time.sleep(4)
        c = requests.get(f"https://aihorde.net/api/v2/generate/check/{job}", timeout=15).json()
        if c.get("faulted"):
            raise RuntimeError("Horde: задача завершилась ошибкой")
        if progress and c.get("queue_position") is not None:
            progress(f"Очередь AI Horde: позиция {c.get('queue_position')}...")
        if c.get("done"):
            st = requests.get(f"https://aihorde.net/api/v2/generate/status/{job}", timeout=20).json()
            gens = st.get("generations") or []
            if not gens:
                raise RuntimeError("Horde: пустой результат")
            img = gens[0].get("img", "")
            if img.startswith("http"):
                data = requests.get(img, timeout=30).content
            else:
                data = base64.b64decode(img)
            if is_image_bytes(data):
                return data
            raise RuntimeError("Horde: неверный формат результата")
    try:
        requests.delete(f"https://aihorde.net/api/v2/generate/status/{job}", timeout=10)
    except Exception:
        pass
    raise RuntimeError("Horde: превышено время ожидания")


def generate_image_dynamic(prompt, enhance=True, progress=None):
    """Возвращает (bytes | None, отчёт_об_ошибках)."""
    w, h = 1024, 1024
    if "16:9" in prompt:
        w, h = 1280, 720
        prompt = prompt.replace("16:9", "").strip()
    elif "9:16" in prompt:
        w, h = 720, 1280
        prompt = prompt.replace("9:16", "").strip()
    if not prompt:
        return None, "пустое описание"
    final_prompt = enhance_image_prompt(prompt) if enhance else prompt
    report = []
    steps = [("Pollinations", lambda: gen_pollinations(final_prompt, w, h))]
    if HF_TOKEN:
        steps.append(("HuggingFace", lambda: gen_huggingface(final_prompt, w, h)))
    steps.append(("AI Horde", lambda: gen_horde(final_prompt, w, h, progress)))
    for i, (name, fn) in enumerate(steps):
        try:
            if progress and i > 0:
                progress(f"Основной генератор не ответил. Пробую запасной ({name})...")
            data = fn()
            stats["images_generated"] += 1
            return to_sendable_image(data), ""
        except Exception as e:
            print(f"❌ Генерация изображения [{name}]: {short_err(e, 300)}")
            report.append(f"{name}: {short_err(e, 110)}")
    return None, "\n".join(report)


async def generate_audio(text, output_file):
    communicate = edge_tts.Communicate(text, "ru-RU-SvetlanaNeural")
    await communicate.save(output_file)


# ════════════════════════════════════════════════════════════════
#  КАЛЬКУЛЯТОР, КУРСЫ, ЗАМЕТКИ, НАПОМИНАНИЯ
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
    j = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10).json()
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
        now = datetime.now(timezone(timedelta(hours=TZ_OFFSET_HOURS)))
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
#  ОБРАЩЕНИЕ К БОТУ
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
        "/code <задача> — большой продуманный код (план, написание, ревью, проверка)\n"
        "/review <код> — разбор и улучшение кода\n"
        "/image <описание> — генерация изображений\n"
        "/file <запрос> — файлы (xlsx, docx, pdf, pptx, py, html...)\n"
        "/search <запрос> — поиск в интернете\n"
        "/weather <город> — погода\n"
        "/gemini <запрос> — прямой запрос к Gemini\n\n"
        "Тексты:\n"
        "/sum, /tr, /fix <текст> — пересказ, перевод, исправление (можно ответом на сообщение)\n"
        "/tts <текст> — озвучивание\n"
        "/fact [тема] — интересный факт\n\n"
        "Утилиты:\n"
        "/calc <выражение> — калькулятор\n"
        "/rate [сумма] USD UZS — курсы валют\n"
        "/qr <текст> — QR-код\n"
        "/pass [длина] — генератор пароля\n"
        "/remind через 10 минут <текст> | /remind 18:30 <текст>\n"
        "/note <текст>, /notes, /delnote <номер> — заметки\n\n"
        "Прочее:\n"
        "/diag — проверка работы всех сервисов\n"
        "/clear — очистить память диалога\n"
        "/neuroham — режим Нейрохама (on/off)\n"
        "/stats — статистика\n\n"
        "Фото: отправь картинку с вопросом в подписи или без. Документы: отправь файл, с подписью-задачей или без."
    ))


@bot.message_handler(commands=["stats"])
def stats_cmd(message):
    bot.reply_to(message, (
        "Статистика бота:\n\n"
        f"Уникальных пользователей: {len(stats['users'])}\n"
        f"Сгенерировано фото: {stats['images_generated']}\n"
        f"Создано файлов: {stats['files_generated']}\n"
        f"Распознано голосовых: {stats['voice_messages']}"))


@bot.message_handler(commands=["diag"])
def diag_cmd(message):
    if ADMIN_ID and str(message.from_user.id) != ADMIN_ID:
        return
    msg = bot.reply_to(message, "Проверяю сервисы, это займёт около минуты...")
    lines = []

    def check(name, fn):
        t0 = time.time()
        try:
            res = fn()
            lines.append(f"✅ {name} ({time.time() - t0:.1f}с){(' — ' + res) if res else ''}")
        except Exception as e:
            lines.append(f"❌ {name}: {short_err(e, 200)}")
        try:
            bot.edit_message_text("Проверяю...\n\n" + "\n".join(lines), message.chat.id, msg.message_id)
        except Exception:
            pass

    lines.append(f"Gemini pro: {', '.join(GEMINI_PRO) or '—'}\nGemini flash: {', '.join(GEMINI_FLASH) or '—'}")
    lines.append(f"Groq: {'ключ есть' if groq_client else 'НЕТ ключа'} | Gemini: "
                 f"{'ключ есть' if gemini_client else 'НЕТ ключа'} | HF: {'есть' if HF_TOKEN else 'нет'}")
    if gemini_client:
        check("Gemini текст", lambda: gemini_call("Ответь одним словом: ок", max_tokens=200, tier="all")[0][:30])
    if groq_client:
        check("Groq текст", lambda: call_llm([{"role": "user", "content": "Ответь одним словом: ок"}],
                                             chain="fast", max_tokens=200)[0][:30])
    if Image:
        buf = io.BytesIO()
        Image.new("RGB", (128, 128), (220, 30, 30)).save(buf, "JPEG")
        test = buf.getvalue()
        q = "Какого цвета изображение? Ответь одним словом."
        if gemini_client:
            check("Gemini фото", lambda: vision_gemini(test, "image/jpeg", "Отвечай кратко.", q)[:40])
        if groq_client:
            check("Groq фото", lambda: vision_groq(test, "image/jpeg", "Отвечай кратко.", q)[:40])
        check("Pollinations фото", lambda: vision_pollinations(test, "image/jpeg", "Отвечай кратко.", q)[:40])
    else:
        lines.append("⚠️ Pillow не установлен (pip install pillow) — фото обрабатываются без сжатия")
    check("Pollinations картинки", lambda: f"{len(gen_pollinations('red apple', 256, 256, budget=60))} байт")
    if HF_TOKEN:
        check("HuggingFace картинки", lambda: f"{len(gen_huggingface('red apple', 256, 256))} байт")
    check("AI Horde (доступность)", lambda: str(requests.get(
        "https://aihorde.net/api/v2/status/heartbeat", timeout=10).status_code))
    edit_or_send_long(message.chat.id, msg.message_id, "Диагностика:\n\n" + "\n".join(lines))


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
                              "/file Таблица умножения в Excel\n/file Бизнес-план кофейни в Word\n"
                              "/file Презентация про космос\n/file Python скрипт парсер сайта")
        return
    execute_file_creation(message, args)


def execute_file_creation(message, request_text, edit_message_id=None):
    extension = detect_file_format(request_text)
    long_job = extension in ("py", "html", "docx", "pdf", "pptx")
    text = f"Создаю файл (.{extension})..." + ("\nКачественные файлы делаются дольше, до пары минут." if long_job else "")
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
        print(f"❌ Ошибка создания файла: {e}")
        edit_or_send_long(message.chat.id, msg_id, f"Ошибка создания файла: {short_err(e, 300)}")
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass


@bot.message_handler(commands=["neuroham", "rude"])
def toggle_neuroham_mode(message):
    user_id = message.chat.id
    cmd = "neuroham" if message.text.startswith("/neuroham") else "rude"
    arg = clean_command_args(message.text, cmd).lower()
    if arg in ("on", "вкл"):
        user_modes[user_id] = "neuroham"
    elif arg in ("off", "выкл"):
        user_modes[user_id] = "normal"
    else:
        user_modes[user_id] = "normal" if is_ham(user_id) else "neuroham"
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
    edit_or_send_long(message.chat.id, msg.message_id, ask_ai_with_history(message.chat.id, prompt, store=False))


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
        edit_or_send_long(message.chat.id, msg.message_id, f"Ошибка обработки: {short_err(e, 300)}")


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


def _send_generated_image(chat_id, prompt, image_bytes):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔄 Перегенерировать",
                                          callback_data=f"reimage:{remember_image_prompt(prompt)}"))
    bot.send_photo(chat_id, io.BytesIO(image_bytes), caption=f"Запрос: {prompt}"[:1000], reply_markup=markup)


def execute_image_generation(message, prompt, edit_message_id=None):
    chat_id = message.chat.id
    if edit_message_id:
        msg_id = edit_message_id
        bot.edit_message_text("Генерирую фото...", chat_id, msg_id)
    else:
        msg_id = bot.reply_to(message, "Генерирую фото...").message_id
    last = {"text": ""}

    def progress(t):
        if t == last["text"]:
            return
        last["text"] = t
        try:
            bot.edit_message_text(t, chat_id, msg_id)
        except Exception:
            pass

    image_bytes, report = generate_image_dynamic(prompt, progress=progress)
    if image_bytes:
        _send_generated_image(chat_id, prompt, image_bytes)
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
    else:
        edit_or_send_long(chat_id, msg_id, "Не удалось сгенерировать изображение ни одним из бесплатных сервисов.\n"
                                           f"{report}\n\nПопробуй позже или упрости запрос. /diag покажет состояние.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("reimage:"))
def callback_reimage(call):
    prompt = image_prompts.get(call.data.split("reimage:", 1)[1])
    if not prompt:
        bot.answer_callback_query(call.id, "Запрос устарел. Создай изображение заново.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "Генерирую новый вариант...")
    status = bot.send_message(call.message.chat.id, f"Повторная генерация для: {prompt}")
    image_bytes, report = generate_image_dynamic(prompt)
    if image_bytes:
        _send_generated_image(call.message.chat.id, prompt, image_bytes)
        try:
            bot.delete_message(call.message.chat.id, status.message_id)
        except Exception:
            pass
    else:
        bot.edit_message_text(f"Не удалось сгенерировать изображение.\n{report}",
                              call.message.chat.id, status.message_id)


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
    length = max(8, min(int(arg) if arg.isdigit() else 16, 64))
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
    for kw in keywords:
        if re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", lower_text):
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
                                   "сделай документ", "создай таблицу", "сделай таблицу", "создай презентацию",
                                   "сделай презентацию"])
    if kw:
        prompt = strip_keyword(clean_text, kw)
        if kw.endswith("презентацию"):
            prompt = "презентация " + prompt
        if prompt:
            execute_file_creation(message, prompt, edit_message_id)
            return

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
    edit_or_send_long(message.chat.id, msg_id, ask_ai_with_history(message.chat.id, clean_text))


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
                message.chat.id, f"{task} Не используй Markdown. Вот текст страницы:\n\n{page_text}", store=False)
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
        code_exts = (".py", ".js", ".ts", ".java", ".cpp", ".cs", ".go", ".rs", ".php", ".html", ".css")
        if caption:
            task = f"{caption}\n\nСодержимое файла {message.document.file_name}:\n\n{text[:20000]}"
            if looks_like_code_request(caption) or ext.lower() in code_exts:
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
