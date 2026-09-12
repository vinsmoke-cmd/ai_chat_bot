import io
import os
import re
import sys
import glob
import json
import math
import time
import asyncio
import logging
import requests
import urllib.parse
from PIL import Image
import docx
import pptx
import openpyxl
import fitz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
)

logging.basicConfig(level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


# --- ФУНКЦИИ ДЛЯ РАБОТЫ С ИИ И ПОГОДОЙ ---

def query_pollinations_text(prompt: str, system_prompt: str = "Ты полезный ассистент.") -> str:
    try:
        url = "https://text.pollinations.ai"
        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            "model": "openai"
        }
        res = requests.post(url, json=payload, timeout=25)
        if res.status_code == 200:
            return res.text
        return f"Ошибка ИИ (Код {res.status_code})"
    except Exception as e:
        return f"Ошибка при запросе к ИИ: {e}"


def get_weather_data(location_name: str) -> str:
    try:
        headers = {'User-Agent': 'WeatherBot/1.0'}
        geo_url = "https://geocoding-api.open-meteo.com/v1/search"
        
        res = requests.get(
            geo_url, 
            params={'name': location_name, 'count': 1, 'language': 'ru', 'format': 'json'}, 
            headers=headers, 
            timeout=5
        ).json()
        
        if not res.get('results'):
            res = requests.get(
                geo_url, 
                params={'name': location_name, 'count': 1, 'format': 'json'}, 
                headers=headers, 
                timeout=5
            ).json()
            
        if not res.get('results'):
            return f"Не удалось найти город '{location_name}'. Проверьте написание."
            
        city = res['results'][0]
        lat = city['latitude']
        lon = city['longitude']
        name = city.get('name', location_name)
        
        w_url = "https://api.open-meteo.com/v1/forecast"
        w_params = {
            'latitude': lat,
            'longitude': lon,
            'current': [
                'temperature_2m', 
                'relative_humidity_2m', 
                'apparent_temperature', 
                'precipitation', 
                'surface_pressure', 
                'wind_speed_10m'
            ],
            'wind_speed_unit': 'ms',
            'timezone': 'auto'
        }
        
        w_data = requests.get(w_url, params=w_params, headers=headers, timeout=5).json().get('current', {})
        
        temp = round(w_data.get('temperature_2m', 0))
        feels = round(w_data.get('apparent_temperature', 0))
        humidity = w_data.get('relative_humidity_2m', 0)
        wind = round(w_data.get('wind_speed_10m', 0), 1)
        pressure = round(w_data.get('surface_pressure', 0) * 0.750062)
        
        return (
            f"Погода в {name}:\n"
            f"🌡️ Температура: {temp}°C (ощущается как {feels}°C)\n"
            f"💧 Влажность: {humidity}%\n"
            f"💨 Ветер: {wind} м/с\n"
            f"📊 Давление: {pressure} мм рт. ст."
        )
    except Exception as e:
        return f"Ошибка получения погоды: {str(e)}"


# --- ОБРАБОТКА ФАЙЛОВ И ДОКУМЕНТОВ ---

def extract_text_from_file(file_bytes, filename):
    ext = os.path.splitext(filename)[1].lower()
    try:
        if ext == '.pdf':
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            return "\n".join([page.get_text() for page in doc])
        elif ext in ['.docx', '.doc']:
            doc = docx.Document(io.BytesIO(file_bytes))
            return "\n".join([p.text for p in doc.paragraphs if p.text])
        elif ext in ['.xlsx', '.xls']:
            wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
            text = []
            for sheet in wb.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    row_str = " | ".join([str(c) for c in row if c is not None])
                    if row_str:
                        text.append(row_str)
            return "\n".join(text)
        elif ext in ['.pptx', '.ppt']:
            prs = pptx.Presentation(io.BytesIO(file_bytes))
            text = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text"):
                        text.append(shape.text)
            return "\n".join(text)
        elif ext in ['.txt', '.py', '.js', '.html', '.css', '.json', '.csv']:
            return file_bytes.decode('utf-8', errors='ignore')
    except Exception as e:
        return f"Ошибка чтения файла: {e}"
    return ""


def create_docx(text):
    doc = docx.Document()
    for p in text.split('\n'):
        if p.strip():
            doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


def create_pptx(text):
    prs = pptx.Presentation()
    slides_data = text.split('---')
    for s_text in slides_data:
        lines = [l.strip() for l in s_text.split('\n') if l.strip()]
        if not lines:
            continue
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = lines[0]
        if len(lines) > 1:
            tf = slide.placeholders[1].text_frame
            tf.text = lines[1]
            for l in lines[2:]:
                p = tf.add_paragraph()
                p.text = l
    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf


def create_xlsx(text):
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in text.split('\n'):
        if row.strip():
            cols = row.split('|') if '|' in row else row.split(',')
            ws.append([c.strip() for c in cols])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# --- ОБРАБОТЧИКИ КОМАНД ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я ваш ИИ-помощник.\n\n"
        "Основные команды:\n"
        "/weather <город> — прогноз погоды 🌤️\n"
        "/kira — режим Киры\n"
        "/gen_image <запрос> — генерация изображений\n"
        "/doc, /pptx, /excel — создание документов\n"
        "Или просто отправьте мне файл, текст или задайте вопрос!"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)


async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Укажите город. Пример: /weather Ташкент")
        return
    city = " ".join(context.args)
    res = get_weather_data(city)
    await update.message.reply_text(res)


async def handle_kira(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['mode'] = 'kira'
    await update.message.reply_text("Режим Киры активирован 🩸")


async def handle_gen_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args)
    if not prompt:
        return await update.message.reply_text("Укажите тему документа.")
    
    msg = await update.message.reply_text("Генерирую документ...")
    res_text = query_pollinations_text(f"Напиши подробный документ на тему: {prompt}")
    buf = create_docx(res_text)
    
    await update.message.reply_document(document=buf, filename="Document.docx")
    await msg.delete()


async def handle_gen_pptx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args)
    if not prompt:
        return await update.message.reply_text("Укажите тему презентации.")
    
    msg = await update.message.reply_text("Генерирую презентацию...")
    res_text = query_pollinations_text(
        f"Создай структуру слайдов PPTX на тему: {prompt}. Разделяй слайды символом '---'. Первый заголовок слайда, дальше пункты."
    )
    buf = create_pptx(res_text)
    
    await update.message.reply_document(document=buf, filename="Presentation.pptx")
    await msg.delete()


async def handle_gen_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args)
    if not prompt:
        return await update.message.reply_text("Укажите описание таблицы.")
    
    msg = await update.message.reply_text("Генерирую таблицу...")
    res_text = query_pollinations_text(
        f"Сформируй данные для таблицы Excel на тему: {prompt}. Разделяй колонки символом '|', строки переносом."
    )
    buf = create_xlsx(res_text)
    
    await update.message.reply_document(document=buf, filename="Table.xlsx")
    await msg.delete()


async def handle_gen_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args)
    if not prompt:
        return await update.message.reply_text("Укажите промпт для генерации картинки.")
    
    msg = await update.message.reply_text("Рисую...")
    try:
        url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}"
        res = requests.get(url, timeout=15)
        if res.status_code == 200:
            await update.message.reply_photo(photo=io.BytesIO(res.content))
        else:
            await update.message.reply_text("Не удалось сгенерировать изображение.")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")
    await msg.delete()


# --- ОБРАБОТКА ТЕКСТОВЫХ СООБЩЕНИЙ И ФАЙЛОВ ---

async def process_natural_language_request(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    text_lower = text.lower()
    
    if any(w in text_lower for w in ["погода", "погоду", "прогноз"]):
        match = re.search(r'(?:погода|прогноз|погоду)\s+(?:в|во|для|на)?\s*([a-яА-Яa-zA-Z\s\-]+)', text, re.IGNORECASE)
        city = match.group(1).strip() if match else "Ташкент"
        city = re.sub(r'^(в|во|на|сегодня|завтра)\s+', '', city, flags=re.IGNORECASE).strip()
        res = get_weather_data(city if city else "Ташкент")
        return await update.message.reply_text(res)

    sys_instruction = "Ты Кира из Death Note." if context.user_data.get('mode') == 'kira' else "Ты полезный ассистент."
    answer = query_pollinations_text(text, system_prompt=sys_instruction)
    await update.message.reply_text(answer)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text:
        await process_natural_language_request(update, context, text)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    file = await context.bot.get_file(doc.file_id)
    content = await file.download_as_bytearray()
    text = extract_text_from_file(bytes(content), doc.file_name)
    
    if not text:
        return await update.message.reply_text("Не удалось прочитать файл.")
    
    prompt = update.message.caption or "Сделай краткую выжимку из этого файла."
    res_text = query_pollinations_text(f"Содержимое файла {doc.file_name}:\n{text[:10000]}\n\nЗадание: {prompt}")
    await update.message.reply_text(res_text)


# --- ТОЧКА ВХОДА ---

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("Ошибка: TELEGRAM_BOT_TOKEN не задан в переменных окружения.")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("weather", weather_command))
    app.add_handler(CommandHandler("kira", handle_kira))
    app.add_handler(CommandHandler("doc", handle_gen_doc))
    app.add_handler(CommandHandler("pptx", handle_gen_pptx))
    app.add_handler(CommandHandler("excel", handle_gen_excel))
    app.add_handler(CommandHandler("gen_image", handle_gen_image))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    print("Бот запущен...")
    app.run_polling()


if __name__ == '__main__':
    main()
