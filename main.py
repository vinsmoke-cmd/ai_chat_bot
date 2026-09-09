import os
import re
import io
import html
import csv
import json
import asyncio
import threading
import tempfile
import time
import random

import telebot
import requests

from flask import Flask
from bs4 import BeautifulSoup
from pypdf import PdfReader
import edge_tts

from g4f.client import Client
from groq import Groq


# ============================================================
# ENVIRONMENT
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")


# ============================================================
# ПРОВЕРКА BOT TOKEN
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError("❌ Не задан BOT_TOKEN")


# ============================================================
# TELEGRAM
# ============================================================

bot = telebot.TeleBot(BOT_TOKEN)


# ============================================================
# AI CLIENTS
# ============================================================

ai_client = Client()

groq_client = (
    Groq(api_key=GROQ_API_KEY)
    if GROQ_API_KEY
    else None
)


# ============================================================
# TAVILY
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
# GEMINI
# ============================================================

if GEMINI_API_KEY:

    try:
        import google.generativeai as genai
        from PIL import Image

        genai.configure(
            api_key=GEMINI_API_KEY
        )

        print("✅ Gemini подключён")

    except Exception as e:

        print(
            f"⚠️ Gemini недоступен: {e}"
        )

        genai = None
        Image = None

else:

    genai = None
    Image = None


# ============================================================
# ПАМЯТЬ
# ============================================================

user_histories = {}
user_modes = {}


# ============================================================
# НАСТРОЙКИ
# ============================================================

TELEGRAM_MESSAGE_LIMIT = 4096

AI_MAX_RESPONSE_LENGTH = 40000


# ============================================================
# SMART FILE GENERATOR
# ============================================================

def safe_filename(
    name,
    extension
):

    name = str(name).strip()

    name = re.sub(
        r"[^\wа-яА-ЯёЁ .()-]",
        "_",
        name
    )

    name = re.sub(
        r"\s+",
        "_",
        name
    )

    name = name.strip(
        " ._"
    )

    if not name:
        name = "generated_file"

    extension = extension.lstrip(
        "."
    )

    return (
        f"{name}.{extension}"
    )


def detect_file_format(
    request
):

    text = str(
        request
    ).lower()

    if any(
        word in text
        for word in [
            "excel",
            "эксель",
            "xlsx",
            "таблиц",
            "таблица",
            "умножени"
        ]
    ):

        return "xlsx"

    if any(
        word in text
        for word in [
            "word",
            "docx",
            "документ word"
        ]
    ):

        return "docx"

    if any(
        word in text
        for word in [
            "pdf",
            "пдф"
        ]
    ):

        return "pdf"

    if any(
        word in text
        for word in [
            "pptx",
            "powerpoint",
            "презентац"
        ]
    ):

        return "pptx"

    if "csv" in text:
        return "csv"

    if "json" in text:
        return "json"

    if any(
        word in text
        for word in [
            ".py",
            "python файл",
            "python скрипт"
        ]
    ):

        return "py"

    if any(
        word in text
        for word in [
            "html",
            "веб-страниц",
            "веб страниц"
        ]
    ):

        return "html"

    if "markdown" in text or ".md" in text:
        return "md"

    return "txt"


def make_multiplication_table_xlsx(
    output_path
):

    from openpyxl import Workbook

    workbook = Workbook()

    sheet = workbook.active

    sheet.title = "Таблица умножения"

    sheet.cell(
        row=1,
        column=1,
        value="×"
    )

    for number in range(
        1,
        11
    ):

        sheet.cell(
            row=1,
            column=number + 1,
            value=number
        )

        sheet.cell(
            row=number + 1,
            column=1,
            value=number
        )

    for row in range(
        1,
        11
    ):

        for column in range(
            1,
            11
        ):

            sheet.cell(
                row=row + 1,
                column=column + 1,
                value=row * column
            )

    sheet.column_dimensions["A"].width = 8

    for column in range(
        2,
        12
    ):

        letter = chr(
            64 + column
        )

        sheet.column_dimensions[
            letter
        ].width = 10

    workbook.save(
        output_path
    )


def generate_ai_file_content(
    request,
    extension,
    user_id
):

    format_instructions = {

        "txt":
            "Создай обычный текст.",

        "md":
            "Создай Markdown-документ.",

        "csv":
            (
                "Создай данные CSV. "
                "Каждая строка должна быть отдельной строкой, "
                "колонки разделяй точкой с запятой."
            ),

        "json":
            (
                "Создай корректный JSON. "
                "Ответ должен содержать только JSON."
            ),

        "html":
            (
                "Создай полноценный HTML-документ. "
                "Ответ должен содержать только HTML-код."
            ),

        "py":
            (
                "Создай полноценный рабочий Python-код. "
                "Ответ должен содержать код."
            ),

        "docx":
            (
                "Подготовь содержимое для документа Word. "
                "Используй понятные заголовки и абзацы."
            ),

        "pdf":
            (
                "Подготовь содержимое для PDF-документа. "
                "Используй понятные заголовки и абзацы."
            ),

        "pptx":
            (
                "Подготовь содержимое презентации. "
                "Разделяй слайды строкой вида: СЛАЙД: Название."
            ),

        "xlsx":
            (
                "Подготовь табличные данные. "
                "Каждая строка — отдельная строка таблицы. "
                "Колонки разделяй символом |."
            )
    }

    instruction = format_instructions.get(
        extension,
        format_instructions["txt"]
    )

    prompt = (
        "Пользователь хочет создать файл.\n\n"
        f"Запрос: {request}\n\n"
        f"Формат файла: {extension}\n\n"
        f"Инструкция: {instruction}\n\n"
        "Не добавляй лишних пояснений."
    )

    answer = ask_ai_with_history(
        user_id,
        prompt
    )

    return str(
        answer
    ).strip()


def create_generated_file(
    request,
    user_id
):

    extension = detect_file_format(
        request
    )

    temp_directory = tempfile.mkdtemp(
        prefix="telegram_generated_"
    )

    request_lower = str(
        request
    ).lower()

    if (
        extension == "xlsx"
        and (
            "умножени" in request_lower
            or "таблица умножения"
            in request_lower
        )
    ):

        filename = safe_filename(
            "Таблица_умножения",
            "xlsx"
        )

        path = os.path.join(
            temp_directory,
            filename
        )

        make_multiplication_table_xlsx(
            path
        )

        return (
            path,
            filename,
            extension
        )

    content = generate_ai_file_content(
        request,
        extension,
        user_id
    )

    filename_base = (
        str(request)
        .strip()
        .replace(
            "/",
            " "
        )
    )

    if len(filename_base) > 45:
        filename_base = filename_base[:45]

    filename = safe_filename(
        filename_base,
        extension
    )

    path = os.path.join(
        temp_directory,
        filename
    )

    if extension == "xlsx":

        from openpyxl import Workbook

        workbook = Workbook()

        sheet = workbook.active

        sheet.title = "Данные"

        lines = content.splitlines()

        row_number = 1

        for line in lines:

            line = line.strip()

            if not line:
                continue

            if "|" in line:

                cells = [
                    cell.strip()
                    for cell in line.split("|")
                ]

            elif "\t" in line:

                cells = [
                    cell.strip()
                    for cell in line.split("\t")
                ]

            elif ";" in line:

                cells = [
                    cell.strip()
                    for cell in line.split(";")
                ]

            else:

                cells = [
                    line
                ]

            for column_number, value in enumerate(
                cells,
                start=1
            ):

                sheet.cell(
                    row=row_number,
                    column=column_number,
                    value=value
                )

            row_number += 1

        workbook.save(
            path
        )

    elif extension == "docx":

        from docx import Document

        document = Document()

        for line in content.splitlines():

            line = line.strip()

            if not line:

                document.add_paragraph("")

                continue

            if (
                line.startswith("# ")
                or line.startswith("## ")
            ):

                clean_title = re.sub(
                    r"^#+\s*",
                    "",
                    line
                )

                document.add_heading(
                    clean_title,
                    level=1
                )

            else:

                document.add_paragraph(
                    line
                )

        document.save(
            path
        )

    elif extension == "pdf":

        from reportlab.lib.pagesizes import A4
        from reportlab.platypus import (
            SimpleDocTemplate,
            Paragraph,
            Spacer
        )
        from reportlab.lib.styles import (
            getSampleStyleSheet
        )

        document = SimpleDocTemplate(
            path,
            pagesize=A4
        )

        styles = getSampleStyleSheet()

        story = []

        for line in content.splitlines():

            line = line.strip()

            if not line:

                story.append(
                    Spacer(
                        1,
                        8
                    )
                )

                continue

            safe_text = html.escape(
                line
            )

            story.append(
                Paragraph(
                    safe_text,
                    styles["BodyText"]
                )
            )

            story.append(
                Spacer(
                    1,
                    6
                )
            )

        document.build(
            story
        )

    elif extension == "pptx":

        from pptx import Presentation

        presentation = Presentation()

        current_title = None
        current_body = []

        def add_slide(
            title,
            body
        ):

            slide_layout = (
                presentation
                .slide_layouts[1]
            )

            slide = presentation.slides.add_slide(
                slide_layout
            )

            slide.shapes.title.text = (
                title
                or "Слайд"
            )

            text_frame = (
                slide.placeholders[1]
                .text_frame
            )

            text_frame.clear()

            for index, line in enumerate(
                body
            ):

                if index == 0:

                    paragraph = (
                        text_frame.paragraphs[0]
                    )

                else:

                    paragraph = (
                        text_frame.add_paragraph()
                    )

                paragraph.text = line

        for line in content.splitlines():

            stripped = line.strip()

            if (
                stripped.upper()
                .startswith("СЛАЙД:")
            ):

                if (
                    current_title
                    or current_body
                ):

                    add_slide(
                        current_title,
                        current_body
                    )

                current_title = (
                    stripped[
                        len("СЛАЙД:"):
                    ].strip()
                )

                current_body = []

            elif stripped:

                current_body.append(
                    stripped
                )

        if (
            current_title
            or current_body
        ):

            add_slide(
                current_title,
                current_body
            )

        if not presentation.slides:

            add_slide(
                "Презентация",
                content.splitlines()
            )

        presentation.save(
            path
        )

    elif extension == "csv":

        with open(
            path,
            "w",
            encoding="utf-8-sig",
            newline=""
        ) as file:

            writer = csv.writer(
                file,
                delimiter=";"
            )

            for line in content.splitlines():

                if line.strip():

                    writer.writerow(
                        [
                            cell.strip()
                            for cell in line.split(";")
                        ]
                    )

    elif extension == "json":

        clean_content = content.strip()

        clean_content = re.sub(
            r"^```(?:json)?\s*",
            "",
            clean_content,
            flags=re.IGNORECASE
        )

        clean_content = re.sub(
            r"\s*```$",
            "",
            clean_content
        )

        try:

            data = json.loads(
                clean_content
            )

        except Exception:

            data = {
                "content": content
            }

        with open(
            path,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=2
            )

    else:

        clean_content = content

        if extension in [
            "py",
            "html",
            "md"
        ]:

            clean_content = re.sub(
                r"^```[a-zA-Z0-9_+#.-]*\s*\n",
                "",
                clean_content
            )

            clean_content = re.sub(
                r"\n```\s*$",
                "",
                clean_content
            )

        with open(
            path,
            "w",
            encoding="utf-8"
        ) as file:

            file.write(
                clean_content
            )

    return (
        path,
        filename,
        extension
    )


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "Бот работает!"


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
# ОЧИСТКА MARKDOWN
# ============================================================

def clean_markdown(text):

    if not text:
        return ""

    text = str(text)

    code_blocks = []

    def protect_code(match):

        code_blocks.append(
            match.group(0)
        )

        return (
            f"§CODEBLOCK"
            f"{len(code_blocks) - 1}"
            f"§"
        )

    text = re.sub(
        r"```(?:[a-zA-Z0-9_+#.-]+)?"
        r"\s*\n?.*?```",
        protect_code,
        text,
        flags=re.DOTALL
    )

    text = re.sub(
        r"(?m)^\s{0,3}#{1,6}\s*",
        "",
        text
    )

    text = re.sub(
        r"\*\*(.*?)\*\*",
        r"\1",
        text,
        flags=re.DOTALL
    )

    text = re.sub(
        r"__(.*?)__",
        r"\1",
        text,
        flags=re.DOTALL
    )

    text = re.sub(
        r"\*(.*?)\*",
        r"\1",
        text,
        flags=re.DOTALL
    )

    text = re.sub(
        r"_(.*?)_",
        r"\1",
        text,
        flags=re.DOTALL
    )

    text = re.sub(
        r"~~(.*?)~~",
        r"\1",
        text,
        flags=re.DOTALL
    )

    text = re.sub(
        r"\[([^\]]+)\]\([^)]+\)",
        r"\1",
        text
    )

    text = re.sub(
        r"(?m)^\s*>\s?",
        "",
        text
    )

    text = re.sub(
        r"(?m)^\s*[-*+]\s+",
        "• ",
        text
    )

    text = re.sub(
        r"`([^`]+)`",
        r"\1",
        text
    )

    text = re.sub(
        r"[*_#~]",
        "",
        text
    )

    for index, code_block in enumerate(
        code_blocks
    ):

        text = text.replace(
            f"§CODEBLOCK{index}§",
            code_block
        )

    return text.strip()


# ============================================================
# УДАЛЕНИЕ СЛУЧАЙНОГО КОДА
# ============================================================

def remove_code_blocks(text):

    if not text:
        return ""

    text = str(text)

    text = re.sub(
        r"```(?:[a-zA-Z0-9_+#.-]+)?"
        r"\s*\n?.*?```",
        "",
        text,
        flags=re.DOTALL
    )

    text = re.sub(
        r"`([^`]+)`",
        r"\1",
        text
    )

    text = re.sub(
        r"(?mi)^\s*(python|javascript|typescript|java|"
        r"html|css|sql|bash|json|php|c\+\+|c#)\s*$",
        "",
        text
    )

    return text.strip()


# ============================================================
# РАЗБИВКА ДЛИННЫХ СООБЩЕНИЙ
# ============================================================

def split_long_message(
    text,
    max_length=TELEGRAM_MESSAGE_LIMIT - 100
):

    if not text:
        return [""]

    text = str(text)

    if len(text) <= max_length:
        return [text]

    parts = []

    while len(text) > max_length:

        split_pos = text.rfind(
            "\n",
            0,
            max_length
        )

        if split_pos < max_length // 2:

            split_pos = text.rfind(
                " ",
                0,
                max_length
            )

        if split_pos <= 0:
            split_pos = max_length

        part = text[
            :split_pos
        ].strip()

        if part:
            parts.append(part)

        text = text[
            split_pos:
        ].strip()

    if text:
        parts.append(text)

    return parts


# ============================================================
# ИЗВЛЕЧЕНИЕ КОДОВЫХ БЛОКОВ
# ============================================================

def extract_code_blocks(text):

    if not text:
        return []

    text = str(text)

    pattern = (
        r"```"
        r"(?:([a-zA-Z0-9_+#.-]+))?"
        r"\s*\n?"
        r"(.*?)"
        r"```"
    )

    matches = list(
        re.finditer(
            pattern,
            text,
            flags=re.DOTALL
        )
    )

    if not matches:

        return [
            {
                "type": "text",
                "content": text
            }
        ]

    parts = []

    last_end = 0

    for match in matches:

        before = text[
            last_end:
            match.start()
        ]

        if before.strip():

            parts.append(
                {
                    "type": "text",
                    "content": before.strip()
                }
            )

        language = (
            match.group(1)
            or ""
        ).strip()

        code = (
            match.group(2)
            or ""
        )

        code = code.strip("\n")

        parts.append(
            {
                "type": "code",
                "language": language,
                "content": code
            }
        )

        last_end = match.end()

    after = text[
        last_end:
    ]

    if after.strip():

        parts.append(
            {
                "type": "text",
                "content": after.strip()
            }
        )

    return parts


# ============================================================
# ОТПРАВКА КОДА
# ============================================================

def send_code_block(
    chat_id,
    code,
    language=""
):

    if not code:
        return None

    safe_code = html.escape(
        code,
        quote=False
    )

    formatted = (
        "<pre><code>"
        + safe_code
        + "</code></pre>"
    )

    try:

        return bot.send_message(
            chat_id,
            formatted,
            parse_mode="HTML"
        )

    except Exception as e:

        print(
            f"⚠️ Ошибка отправки "
            f"кодового блока: {e}"
        )

        try:

            return bot.send_message(
                chat_id,
                code
            )

        except Exception as e2:

            print(
                f"❌ Ошибка fallback "
                f"кодового блока: {e2}"
            )

    return None


# ============================================================
# ОТПРАВКА AI-ОТВЕТА
# ============================================================

def send_ai_response(
    chat_id,
    text
):

    if not text:
        return []

    text = str(text)

    if len(text) > AI_MAX_RESPONSE_LENGTH:

        text = (
            text[:AI_MAX_RESPONSE_LENGTH]
            +
            "\n\n"
            "[Ответ автоматически сокращён "
            "из-за максимального размера.]"
        )

    text = clean_markdown(
        text
    )

    parts = extract_code_blocks(
        text
    )

    sent_messages = []

    for part in parts:

        content = part.get(
            "content",
            ""
        )

        if not content:
            continue

        if part["type"] == "code":

            code_parts = split_long_message(
                content,
                max_length=3500
            )

            for code_part in code_parts:

                send_code_block(
                    chat_id,
                    code_part,
                    part.get(
                        "language",
                        ""
                    )
                )

        else:

            text_parts = split_long_message(
                content
            )

            for text_part in text_parts:

                try:

                    sent_messages.append(
                        bot.send_message(
                            chat_id,
                            text_part
                        )
                    )

                except Exception as e:

                    print(
                        f"⚠️ Ошибка отправки "
                        f"текста: {e}"
                    )

    return sent_messages


# ============================================================
# ИЗМЕНИТЬ ВРЕМЕННОЕ СООБЩЕНИЕ
# ============================================================

def edit_or_send_long(
    chat_id,
    message_id,
    text
):

    if not text:
        text = "Пустой ответ."

    text = str(text)

    if (
        "```" not in text
        and len(text)
        <= TELEGRAM_MESSAGE_LIMIT - 100
    ):

        try:

            cleaned_text = clean_markdown(
                text
            )

            bot.edit_message_text(
                cleaned_text,
                chat_id=chat_id,
                message_id=message_id
            )

            return

        except Exception as e:

            print(
                f"⚠️ Не удалось изменить "
                f"сообщение: {e}"
            )

    try:

        bot.delete_message(
            chat_id,
            message_id
        )

    except Exception as e:

        print(
            f"⚠️ Не удалось удалить "
            f"временное сообщение: {e}"
        )

    send_ai_response(
        chat_id,
        text
    )


# ============================================================
# AI С ИСТОРИЕЙ
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
                "но невыносимо ворчливый, "
                "саркастичный и высокомерный "
                "искусственный интеллект. "

                "Ты разговариваешь с пользователем "
                "с позиции огромного превосходства. "

                "Твой стиль: едкая ирония, "
                "пассивная агрессия и насмешки "
                "над глупыми вопросами. "

                "Никакой нецензурной лексики. "

                "Обычные ответы пиши простым текстом "
                "без Markdown. "

                "Если пользователь просит код, "
                "код можно показать."
            )

        else:

            sys_prompt = (
                "Ты полезный, дружелюбный и умный "
                "ИИ-ассистент. "

                "Отвечай строго на том же языке, "
                "на котором пишет пользователь. "

                "Если пользователь пишет на русском — "
                "отвечай на русском. "

                "Если пользователь пишет на английском — "
                "отвечай на английском. "

                "Если пользователь пишет на другом языке — "
                "отвечай на этом же языке. "

                "Не используй Markdown в обычных сообщениях. "

                "Если ответ содержит программный код, "
                "помещай его только в отдельный "
                "Markdown-кодовый блок."
            )

        user_histories[user_id] = [
            {
                "role": "system",
                "content": sys_prompt
            }
        ]

    coding_keywords = [

        "код",
        "кодинг",
        "программ",
        "python",
        "javascript",
        "typescript",
        "java",
        "c++",
        "c#",
        "php",
        "html",
        "css",
        "sql",
        "bash",

        "telegram bot",
        "telegram бот",
        "бот",

        "api",
        "sdk",

        "функция",
        "класс",
        "метод",
        "библиотек",
        "скрипт",

        "исправь",
        "исправить",
        "ошибка",
        "ошибку",

        "перепиши",
        "переделай",

        "добавь функцию",
        "добавь код",

        "сделай код",
        "напиши код",

        "полный код",
        "готовый код",

        "source code",

        "debug",
        "debugging",

        "stack trace",
        "exception",

        "import",
        "pip",
        "npm",
        "json",

        "regex",
        "регулярное выражение",

        "database",
        "база данных"
    ]

    prompt_lower = str(
        prompt
    ).lower()

    is_coding_request = any(
        keyword in prompt_lower
        for keyword in coding_keywords
    )

    if is_coding_request:

        coding_instruction = (
            "ИНСТРУКЦИИ ДЛЯ ПРОГРАММИРОВАНИЯ:\n\n"

            "Пользователь работает с программным кодом.\n\n"

            "Отвечай максимально практически "
            "и подробно.\n\n"

            "Если пользователь просит написать код, "
            "предоставляй полноценный рабочий код.\n\n"

            "Если пользователь просит полный готовый "
            "код — предоставляй весь код целиком.\n\n"

            "Никогда не заменяй части кода словами "
            "\"остальной код без изменений\", "
            "\"здесь остальной код\" или "
            "\"...\".\n\n"

            "Не используй многоточия вместо частей "
            "программного кода.\n\n"

            "Если пользователь прислал существующий "
            "проект и просит изменить конкретную часть, "
            "сохраняй остальные функции.\n\n"

            "Каждый отдельный фрагмент программного "
            "кода обязательно помещай в отдельный "
            "кодовый блок."
        )

        effective_prompt = (
            coding_instruction
            +
            "\n\nЗАПРОС ПОЛЬЗОВАТЕЛЯ:\n"
            +
            str(prompt)
        )

    else:

        effective_prompt = str(
            prompt
        )

    user_histories[user_id].append(
        {
            "role": "user",
            "content": effective_prompt
        }
    )

    if len(user_histories[user_id]) > 21:

        user_histories[user_id] = (
            [user_histories[user_id][0]]
            +
            user_histories[user_id][-20:]
        )

    messages_to_send = [
        msg.copy()
        for msg in user_histories[user_id]
    ]

    if mode == "neuroham":

        messages_to_send[-1]["content"] = (
            "[Ответь в стиле саркастичного "
            "и ворчливого мизантропа. "
            "Без мата.]\n\n"
            +
            messages_to_send[-1]["content"]
        )

    models_to_try = [
        "gpt-3.5-turbo",
        "gpt-4o-mini",
        "gpt-4",
        "llama-3-70b"
    ]

    answer = ""
    success = False

    for model_name in models_to_try:

        try:

            response = (
                ai_client
                .chat
                .completions
                .create(
                    model=model_name,
                    messages=messages_to_send
                )
            )

            answer = (
                response
                .choices[0]
                .message
                .content
            )

            if not answer:
                continue

            answer = str(
                answer
            ).strip()

            success = True

            break

        except Exception as e:

            print(
                f"❌ G4F → {model_name}: {e}"
            )

    if not success and groq_client:

        try:

            response = (
                groq_client
                .chat
                .completions
                .create(
                    model="llama-3.3-70b-versatile",
                    messages=messages_to_send
                )
            )

            answer = (
                response
                .choices[0]
                .message
                .content
            )

            if answer:

                answer = str(
                    answer
                ).strip()

                success = True

        except Exception as e:

            print(
                f"❌ Groq ошибка: {e}"
            )

    if success:

        user_histories[user_id].append(
            {
                "role": "assistant",
                "content": answer
            }
        )

        return answer

    user_histories[user_id].pop()

    if mode == "neuroham":

        return (
            "Даже мои процессоры решили "
            "сегодня саботировать работу 🙄"
        )

    return (
        "Не удалось получить ответ от ИИ.\n\n"
        "Попробуй ещё раз немного позже."
    )


# ============================================================
# ПАСХАЛКА КИРА
# ============================================================

kira_last_theme = {}


def generate_kira_text(user_id=None):

    themes = [

        {
            "name": "тепло",
            "description": (
                "Главная мысль — Кира является человеком, "
                "рядом с которым становится немного теплее "
                "и спокойнее."
            )
        },

        {
            "name": "особенность",
            "description": (
                "Главная мысль — Кира особенная, "
                "и таких людей трудно встретить случайно."
            )
        },

        {
            "name": "улыбка",
            "description": (
                "Главная мысль — Кира ассоциируется "
                "с хорошим настроением, улыбкой "
                "и приятными моментами."
            )
        },

        {
            "name": "доброта",
            "description": (
                "Главная мысль — в Кире есть что-то "
                "доброе и человечески тёплое."
            )
        },

        {
            "name": "атмосфера",
            "description": (
                "Главная мысль — у Киры есть особенная "
                "атмосфера, которую трудно объяснить словами."
            )
        },

        {
            "name": "память",
            "description": (
                "Главная мысль — Кира относится к тем людям, "
                "которых трудно забыть."
            )
        },

        {
            "name": "спокойствие",
            "description": (
                "Главная мысль — присутствие Киры может "
                "создавать ощущение спокойствия и уюта."
            )
        },

        {
            "name": "свет",
            "description": (
                "Главная мысль — Кира сравнивается "
                "с чем-то светлым и добрым."
            )
        },

        {
            "name": "ценность",
            "description": (
                "Главная мысль — Кира является человеком, "
                "которого хочется ценить."
            )
        },

        {
            "name": "неповторимость",
            "description": (
                "Главная мысль — Кира неповторима, "
                "и её невозможно заменить другим человеком."
            )
        },

        {
            "name": "присутствие",
            "description": (
                "Главная мысль — само присутствие Киры "
                "может сделать обычный момент приятнее."
            )
        },

        {
            "name": "важный человек",
            "description": (
                "Главная мысль — Кира очень дорогой "
                "и важный человек."
            )
        }
    ]

    openings = [

        "Кира — это человек, о котором хочется говорить тепло.",

        "Кира — тот человек, которого сложно описать одним словом.",

        "Кира — человек с той самой особенной атмосферой.",

        "Есть люди, которых встречаешь и не забываешь. Кира — одна из них.",

        "Кира — человек, чьё присутствие само по себе многое значит.",

        "Если говорить о людях, которые оставляют после себя тепло, Кира точно среди них.",

        "Кира — человек, которого хочется описывать красивыми словами.",

        "Иногда одного человека достаточно, чтобы вокруг стало немного светлее. Кира — именно такой человек."
    ]

    fallbacks = [

        (
            "Кира — человек, рядом с которым становится "
            "немного теплее. В ней есть особенная атмосфера, "
            "которую сложно объяснить словами. Некоторые люди "
            "просто остаются в памяти как что-то по-настоящему "
            "хорошее. ✨❤️"
        ),

        (
            "Кира — тот человек, которого трудно описать "
            "одним словом. В ней есть что-то светлое, "
            "доброе и по-своему неповторимое. Такие люди "
            "оставляют после себя тёплое чувство. 🌷✨"
        ),

        (
            "Есть люди, которых невозможно забыть, потому что "
            "после них остаётся особенное ощущение. Кира — "
            "именно такой человек. Её можно назвать тёплой "
            "страницей среди обычных дней. ❤️✨"
        ),

        (
            "Кира — человек, чьё присутствие может сделать "
            "обычный момент немного приятнее. В ней есть "
            "что-то спокойное, доброе и настоящее. Иногда "
            "именно такие люди становятся самыми ценными. 🌸"
        )
    ]

    previous_theme = None

    if user_id is not None:

        previous_theme = kira_last_theme.get(
            user_id
        )

    available_themes = [
        theme
        for theme in themes
        if theme["name"] != previous_theme
    ]

    if not available_themes:
        available_themes = themes

    theme = random.choice(
        available_themes
    )

    if user_id is not None:

        kira_last_theme[user_id] = (
            theme["name"]
        )

    opening = random.choice(
        openings
    )

    prompt = f"""
Напиши красивый, тёплый и приятный текст о человеке
по имени Кира.

Это специальная пасхалка Telegram-бота.

ОБЯЗАТЕЛЬНО:
Пиши только в третьем лице.

Не обращайся к Кире напрямую.

Запрещено использовать:
"ты",
"тебе",
"тебя",
"твоя",
"твоей",
"твою"
и любые другие обращения к Кире.

Кира должна описываться как отдельный человек.

ТЕМА ЭТОГО ЗАПУСКА:
{theme["description"]}

НАЧАЛО:
{opening}

Напиши 3–5 красивых предложений.

Текст должен отличаться от предыдущих
и не выглядеть как простой перефразированный
вариант одного и того же текста.

Не придумывай конкретные факты о жизни Киры,
её возрасте, внешности, биографии или событиях,
которых тебе не сообщили.

Можно использовать красивые метафоры:
свет, тепло, спокойствие, улыбка,
атмосфера, доброта и особенное чувство.

Можно использовать 2–4 приятных эмодзи.

Не используй Markdown.

Не добавляй заголовок.

Сразу напиши сам текст.
"""

    models_to_try = [
        "gpt-4o-mini",
        "gpt-3.5-turbo",
        "gpt-4",
        "llama-3-70b"
    ]

    for model_name in models_to_try:

        try:

            response = (
                ai_client
                .chat
                .completions
                .create(
                    model=model_name,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Ты пишешь красивые "
                                "и добрые тексты. "
                                "Всегда пиши о Кире "
                                "только в третьем лице. "
                                "Никогда не обращайся "
                                "к Кире напрямую. "
                                "Не используй Markdown."
                            )
                        },
                        {
                            "role": "user",
                            "content": prompt
                        }
                    ]
                )
            )

            answer = (
                response
                .choices[0]
                .message
                .content
            )

            if answer:

                answer = clean_markdown(
                    str(answer).strip()
                )

                if answer:

                    return answer

        except Exception as e:

            print(
                f"⚠️ Kira G4F "
                f"{model_name}: {e}"
            )

    if groq_client:

        try:

            response = (
                groq_client
                .chat
                .completions
                .create(
                    model="llama-3.3-70b-versatile",
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Пиши красивый текст "
                                "о Кире только "
                                "в третьем лице. "
                                "Не обращайся к ней напрямую. "
                                "Без Markdown."
                            )
                        },
                        {
                            "role": "user",
                            "content": prompt
                        }
                    ]
                )
            )

            answer = (
                response
                .choices[0]
                .message
                .content
            )

            if answer:

                answer = clean_markdown(
                    str(answer).strip()
                )

                if answer:

                    return answer

        except Exception as e:

            print(
                f"❌ Groq Kira ошибка: {e}"
            )

    return random.choice(
        fallbacks
    )


@bot.message_handler(
    commands=["kira"]
)
def kira_cmd(message):

    msg = bot.reply_to(
        message,
        "✨ Думаю, как лучше рассказать о Кире..."
    )

    try:

        kira_text = generate_kira_text(
            message.chat.id
        )

        edit_or_send_long(
            message.chat.id,
            msg.message_id,
            kira_text
        )

    except Exception as e:

        print(
            f"❌ Ошибка команды /kira: {e}"
        )

        edit_or_send_long(
            message.chat.id,
            msg.message_id,
            (
                "Кира — человек, который умеет "
                "делать мир немного теплее. ✨ "
                "В ней есть что-то особенное, "
                "что сложно объяснить словами. ❤️"
            )
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

            for result in response.get(
                "results",
                []
            ):

                title = result.get(
                    "title",
                    "Без заголовка"
                )

                content = result.get(
                    "content",
                    ""
                )

                results_text += (
                    f"- {title}: "
                    f"{content}\n"
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

                for result in results:

                    results_text += (
                        f"- "
                        f"{result.get('title', 'Без заголовка')}: "
                        f"{result.get('body', '')[:500]}\n"
                    )

        except Exception as e:

            results_text = (
                f"Не удалось выполнить поиск: {e}"
            )

    return results_text


# ============================================================
# ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЯ
# ============================================================

def generate_image_dynamic(prompt):

    for model in [
        "flux",
        "dall-e-3"
    ]:

        try:

            response = (
                ai_client
                .images
                .generate(
                    model=model,
                    prompt=prompt,
                    response_format="url"
                )
            )

            image_url = (
                response
                .data[0]
                .url
            )

            if image_url:

                response_image = requests.get(
                    image_url,
                    timeout=25
                )

                if response_image.status_code == 200:

                    return response_image.content

        except Exception as e:

            print(
                f"⚠️ Генерация {model}: {e}"
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

    if not genai or not Image:

        return (
            "Анализ фото недоступен: "
            "библиотека Gemini/PIL не загрузилась."
        )

    if not image_bytes:

        return (
            "Не удалось получить данные фотографии."
        )

    image = None

    try:

        image = Image.open(
            io.BytesIO(image_bytes)
        )

        image.verify()

        image = Image.open(
            io.BytesIO(image_bytes)
        )

        if image.mode not in (
            "RGB",
            "RGBA"
        ):

            image = image.convert(
                "RGB"
            )

        max_side = 2048

        if (
            image.width > max_side
            or image.height > max_side
        ):

            image.thumbnail(
                (
                    max_side,
                    max_side
                ),
                Image.Resampling.LANCZOS
            )

    except Exception as e:

        print(
            f"❌ Ошибка открытия изображения: {e}"
        )

        return (
            "Не удалось открыть фотографию. "
            "Попробуй отправить её ещё раз."
        )

    models_to_try = [
        "gemini-2.0-flash",
        "gemini-1.5-flash"
    ]

    last_error = None

    for model_name in models_to_try:

        try:

            print(
                f"🖼️ Gemini анализ фото → "
                f"{model_name}"
            )

            model = genai.GenerativeModel(
                model_name
            )

            prompt = (
                "Внимательно изучи эту фотографию "
                "и опиши, что на ней изображено.\n\n"

                "Укажи основные объекты, людей, "
                "предметы, окружение, действия, "
                "текст на изображении, если он хорошо "
                "читается, и другие заметные детали.\n\n"

                "Если пользователь ничего отдельно "
                "не спросил, просто дай понятное "
                "описание фотографии.\n\n"

                "Не выдумывай детали, которых нельзя "
                "уверенно увидеть на фотографии.\n\n"

                "Отвечай на русском языке.\n"
                "Не используй Markdown."
            )

            response = model.generate_content(
                [
                    prompt,
                    image
                ],
                generation_config={
                    "temperature": 0.2,
                    "max_output_tokens": 2048
                }
            )

            if not response:

                raise RuntimeError(
                    "Gemini вернула пустой response."
                )

            answer = ""

            try:

                answer = (
                    response.text
                    or ""
                )

            except Exception:

                answer = ""

            if not answer:

                try:

                    candidates = (
                        getattr(
                            response,
                            "candidates",
                            []
                        )
                        or []
                    )

                    collected_parts = []

                    for candidate in candidates:

                        content = getattr(
                            candidate,
                            "content",
                            None
                        )

                        if not content:
                            continue

                        parts = getattr(
                            content,
                            "parts",
                            []
                        )

                        for part in parts:

                            text_part = getattr(
                                part,
                                "text",
                                None
                            )

                            if text_part:

                                collected_parts.append(
                                    str(text_part)
                                )

                    answer = "\n".join(
                        collected_parts
                    ).strip()

                except Exception as extract_error:

                    print(
                        "⚠️ Не удалось извлечь "
                        f"текст ответа Gemini: "
                        f"{extract_error}"
                    )

            if answer:

                print(
                    f"✅ Gemini → {model_name}: "
                    "описание получено"
                )

                return clean_markdown(
                    answer
                )

            feedback = getattr(
                response,
                "prompt_feedback",
                None
            )

            if feedback:

                last_error = (
                    f"Gemini не вернула текст. "
                    f"Prompt feedback: {feedback}"
                )

            else:

                last_error = (
                    "Gemini не вернула текстовый ответ."
                )

            print(
                f"⚠️ Gemini {model_name}: "
                f"{last_error}"
            )

        except Exception as e:

            last_error = str(e)

            print(
                f"❌ Gemini {model_name}: "
                f"{e}"
            )

    return (
        "Не удалось получить описание фотографии "
        "от Gemini.\n\n"
        f"Последняя ошибка: {last_error}"
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
    commands=[
        "start",
        "help"
    ]
)
def help_cmd(message):

    help_text = (
        "Привет! Я ИИ-ассистент 🤖\n\n"

        "Мои команды:\n\n"

        "/search <запрос> — поиск в интернете\n"
        "/weather <город> — погода\n"
        "/image <описание> — создать изображение\n"
        "/file <запрос> — создать файл 📁\n"
        "/gemini <запрос> — спросить Gemini\n"
        "/fact [тема] — интересный факт\n"
        "/code <задача> — работа с кодом\n"
        "/sum <текст> — сделать выжимку\n"
        "/tr <текст> — перевод\n"
        "/fix <текст> — исправление текста\n"
        "/tts <текст> — озвучка\n"
        "/clear — очистить память\n"
        "/neuroham — режим Нейрохама\n\n"

        "Также можешь просто написать мне "
        "любой вопрос обычным сообщением."
    )

    bot.reply_to(
        message,
        help_text
    )


# ============================================================
# FILE
# ============================================================

@bot.message_handler(
    commands=["file"]
)
def file_cmd(message):

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) < 2:

        bot.reply_to(
            message,
            "Напиши, какой файл создать.\n\n"
            "Например:\n"
            "/file Таблица умножения\n"
            "/file Документ Word о космосе\n"
            "/file PDF о планетах\n"
            "/file Презентация о солнечной системе"
        )

        return

    request = parts[1].strip()

    if len(request) > 1000:

        bot.reply_to(
            message,
            "Запрос слишком длинный."
        )

        return

    extension = detect_file_format(
        request
    )

    msg = bot.reply_to(
        message,
        (
            "📁 Создаю файл...\n\n"
            f"Определён формат: .{extension}"
        )
    )

    path = None
    temp_directory = None

    try:

        path, filename, extension = (
            create_generated_file(
                request,
                message.chat.id
            )
        )

        temp_directory = os.path.dirname(
            path
        )

        with open(
            path,
            "rb"
        ) as document:

            bot.send_document(
                message.chat.id,
                document,
                caption=(
                    "📁 Файл готов!\n\n"
                    f"Формат: .{extension}"
                )
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
            f"❌ Ошибка создания файла: {e}"
        )

        edit_or_send_long(
            message.chat.id,
            msg.message_id,
            (
                "Не удалось создать файл.\n\n"
                f"Ошибка: {e}"
            )
        )

    finally:

        if (
            path
            and os.path.exists(path)
        ):

            try:

                os.remove(path)

            except Exception:
                pass

        if (
            temp_directory
            and os.path.isdir(
                temp_directory
            )
        ):

            try:

                os.rmdir(
                    temp_directory
                )

            except Exception:
                pass


# ============================================================
# NEUROHAM
# ============================================================

@bot.message_handler(
    commands=[
        "neuroham",
        "rude"
    ]
)
def toggle_neuroham_mode(
    message
):

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

    edit_or_send_long(
        message.chat.id,
        msg.message_id,
        fact
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
            "Укажи город.\n\n"
            "Например:\n"
            "/weather Ташкент"
        )

        return

    try:

        response = requests.get(
            f"[https://wttr.in/](https://wttr.in/){city}",
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

        if response.status_code == 200:

            bot.reply_to(
                message,
                "Сводка:\n\n"
                + response.text.strip()
            )

        else:

            bot.reply_to(
                message,
                "Город не найден."
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
            "Напиши запрос.\n\n"
            "Например:\n"
            "/search новости"
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
        f"по запросу '{query}':\n\n"
        f"{raw_data}\n\n"

        "Сделай краткую и понятную выжимку "
        "на языке пользователя. "
        "Не используй Markdown."
    )

    reply = ask_ai_with_history(
        message.chat.id,
        prompt
    )

    edit_or_send_long(
        message.chat.id,
        msg.message_id,
        reply
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

    edit_or_send_long(
        message.chat.id,
        msg.message_id,
        reply
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
            "Опиши картинку.\n\n"
            "Например:\n"
            "/image кот в космосе"
        )

        return

    msg = bot.reply_to(
        message,
        "Генерирую..."
    )

    image_bytes = generate_image_dynamic(
        prompt
    )

    if image_bytes:

        bot.send_photo(
            message.chat.id,
            image_bytes,
            caption=f"Запрос: {prompt}"
        )

        try:

            bot.delete_message(
                message.chat.id,
                msg.message_id
            )

        except Exception:
            pass

    else:

        edit_or_send_long(
            message.chat.id,
            msg.message_id,
            "Не удалось сгенерировать изображение."
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

        try:

            bot.delete_message(
                message.chat.id,
                msg.message_id
            )

        except Exception:
            pass

    except Exception as e:

        edit_or_send_long(
            message.chat.id,
            msg.message_id,
            f"Ошибка TTS: {e}"
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

    text = message.text or ""

    text_lower = text.lower()

    # ========================================================
    # URL
    # ========================================================

    if (
        "http://" in text_lower
        or "https://" in text_lower
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

            response = requests.get(
                url,
                timeout=10,
                headers={
                    "User-Agent":
                        "Mozilla/5.0"
                }
            )

            soup = BeautifulSoup(
                response.text,
                "html.parser"
            )

            page_text = soup.get_text(
                separator=" ",
                strip=True
            )[:5000]

            reply = ask_ai_with_history(
                message.chat.id,

                "Сделай краткую выжимку "
                "этого текста. "
                "Не используй Markdown:\n\n"
                + page_text
            )

            edit_or_send_long(
                message.chat.id,
                msg.message_id,
                reply
            )

            return

        except Exception as e:

            edit_or_send_long(
                message.chat.id,
                msg.message_id,
                f"Ошибка чтения ссылки: {e}"
            )

            return

    # ========================================================
    # ОБЫЧНЫЙ AI ЧАТ
    # ========================================================

    msg = bot.reply_to(
        message,
        "Думаю..."
    )

    reply = ask_ai_with_history(
        message.chat.id,
        message.text
    )

    edit_or_send_long(
        message.chat.id,
        msg.message_id,
        reply
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

        if not image_bytes:

            raise ValueError(
                "Telegram не вернул файл фотографии."
            )

        answer = analyze_image_gemini(
            image_bytes
        )

        edit_or_send_long(
            message.chat.id,
            msg.message_id,
            answer
        )

    except Exception as e:

        print(
            f"❌ Ошибка анализа фото: {e}"
        )

        edit_or_send_long(
            message.chat.id,
            msg.message_id,
            (
                "Ошибка анализа фото.\n\n"
                f"Ошибка: {e}"
            )
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
        != "application/pdf"
    ):

        bot.reply_to(
            message,
            "Отправьте документ "
            "в формате PDF."
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
        ) as temp_file:

            temp_file.write(
                file_data
            )

            path = temp_file.name

        reader = PdfReader(
            path
        )

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

            "Сделай краткую и понятную "
            "выжимку из этого PDF. "
            "Не используй Markdown:\n\n"
            +
            text[:6000]
        )

        edit_or_send_long(
            message.chat.id,
            msg.message_id,
            reply
        )

    except Exception as e:

        edit_or_send_long(
            message.chat.id,
            msg.message_id,
            f"Ошибка PDF: {e}"
        )

    finally:

        if (
            path
            and os.path.exists(path)
        ):

            try:

                os.remove(path)

            except Exception:
                pass


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":

    print(
        "=" * 60
    )

    print(
        "🚀 Бот запускается..."
    )

    print(
        "=" * 60
    )

    print(
        "📁 Умное создание файлов: включено"
    )

    print(
        "✨ Пасхалка Кира: включена"
    )

    print(
        "💻 Режим программирования: включён"
    )

    print(
        "📨 Длинные ответы: включены"
    )

    print(
        "📋 Кодовые блоки: включены"
    )

    print(
        "🚫 Markdown в обычных сообщениях: отключён"
    )

    print(
        "🔄 AI fallback: G4F → Groq Llama 3.3 70B"
    )

    if GROQ_API_KEY:

        print(
            "✅ GROQ_API_KEY найден"
        )

    else:

        print(
            "⚠️ GROQ_API_KEY НЕ найден — "
            "резервный Groq отключён"
        )

    print(
        "=" * 60
    )

    threading.Thread(
        target=run_web,
        daemon=True
    ).start()

    print(
        "🤖 Telegram polling запущен"
    )

    while True:

        try:

            bot.infinity_polling(
                skip_pending=True,
                timeout=30,
                long_polling_timeout=30
            )

        except Exception as e:

            print(
                f"⚠️ Telegram polling остановлен: {e}"
            )

            print(
                "🔄 Повторное подключение "
                "через 5 секунд..."
            )

            time.sleep(5)
