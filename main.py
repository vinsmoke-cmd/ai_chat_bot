import os
import threading
import asyncio
import edge_tts
from flask import Flask
from groq import Groq
import telebot
from telebot.types import BotCommand
from duckduckgo_search import DDGS
import google.generativeai as genai
import requests

BOT_TOKEN = os.getenv('BOT_TOKEN')
GROQ_KEY = os.getenv('GROQ_KEY') or os.getenv('GROQ_API_KEY')
GEMINI_KEY = os.getenv('GEMINI_API_KEY')

bot = telebot.TeleBot(BOT_TOKEN)
groq_client = Groq(api_key=GROQ_KEY) if GROQ_KEY else None

if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)
    gemini_vision_model = genai.GenerativeModel('gemini-1.5-flash')
    gemini_text_model = genai.GenerativeModel('gemini-1.5-flash')

app = Flask('')

dialog_history = {}
# Увеличили длину памяти до 100 сообщений (50 пар вопрос-ответ)
MAX_HISTORY_LENGTH = 1000
FIXED_MODEL = 'openai/gpt-oss-120b'

@app.route('/')
def home():
    return 'Bot is active and running!'

def run_web():
    app.run(host='0.0.0.0', port=8080)

bot.set_my_commands([
    BotCommand("help", "Список всех команд"),
    BotCommand("image", "Сгенерировать картинку"),
    BotCommand("gemini", "Спросить у Gemini"),
    BotCommand("search", "Поиск в интернете"),
    BotCommand("code", "Написать или разобрать код"),
    BotCommand("sum", "Краткая выжимка"),
    BotCommand("tr", "Перевод"),
    BotCommand("fix", "Исправить ошибки"),
    BotCommand("tts", "Озвучить текст"),
    BotCommand("clear", "Сбросить контекст")
])

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    text = (
        "Привет! Твой мультимодальный ИИ-помощник с огромной памятью.\n\n"
        "Возможности:\n"
        "• Помнит до 100 сообщений нашего диалога.\n"
        "• Распознаю отправленные картинки через Gemini.\n"
        "• Команда /gemini — текстовый запрос к Gemini.\n"
        "• Команда /image — генерация изображений.\n"
        "• Поиск в сети и текстовый чат через GPT-OSS 120B."
    )
    bot.reply_to(message, text)

@bot.message_handler(commands=['clear'])
def clear_history(message):
    chat_id = message.chat.id
    if chat_id in dialog_history:
        dialog_history[chat_id] = []
    bot.reply_to(message, "Контекст и память диалога полностью сброшены!")

@bot.message_handler(commands=['image'])
def handle_image_generation(message):
    prompt = message.text.replace('/image', '').strip()
    if not prompt:
        bot.reply_to(message, "Напиши, что нарисовать. Пример: /image sports car")
        return

    bot.send_chat_action(message.chat.id, 'upload_photo')
    try:
        if prompt.lower() in ['машина', 'авто', 'автомобиль']:
            prompt = 'modern sports car driving on a scenic highway, highly detailed, photorealistic'
            
        encoded_prompt = requests.utils.quote(prompt)
        image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}"
        bot.send_photo(message.chat.id, image_url, caption=f"Запрос: {prompt}")
    except Exception as e:
        bot.reply_to(message, f"Ошибка генерации: {e}")

@bot.message_handler(commands=['gemini'])
def handle_gemini(message):
    if not GEMINI_KEY:
        bot.reply_to(message, "Ошибка: GEMINI_API_KEY не задан в переменных окружения!")
        return
    query = message.text.replace('/gemini', '').strip()
    if not query:
        bot.reply_to(message, "Напиши запрос для Gemini.")
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        response = gemini_text_model.generate_content(query)
        bot.reply_to(message, response.text)
    except Exception as e:
        bot.reply_to(message, f"Ошибка Gemini: {e}")

@bot.message_handler(commands=['tts'])
def handle_tts(message):
    text = message.text.replace('/tts', '').strip()
    if not text:
        bot.reply_to(message, 'Напиши текст для озвучки.')
        return
    bot.send_chat_action(message.chat.id, 'record_voice')
    try:
        filename = f"voice_{message.from_user.id}.mp3"
        communicate = edge_tts.Communicate(text, 'ru-RU-SvetlanaNeural')
        asyncio.run(communicate.save(filename))
        with open(filename, 'rb') as voice:
            bot.send_voice(message.chat.id, voice)
        os.remove(filename)
    except Exception as e:
        bot.reply_to(message, f'Ошибка аудио: {e}')

@bot.message_handler(commands=['search'])
def handle_search(message):
    query = message.text.replace('/search', '').strip()
    if not query:
        bot.reply_to(message, "Напиши запрос для поиска.")
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        with DDGS() as ddgs:
            results = [r for r in ddgs.text(query, max_results=3)]
        if not results:
            data = "Ничего не найдено."
        else:
            search_text = "\n\n".join([f"{r.get('title')}: {r.get('body')}" for r in results])
            prompt = f"Запрос: '{query}'. Данные из сети:\n\n{search_text}\n\nДай ответ простым текстом."
            chat = groq_client.chat.completions.create(
                messages=[{'role': 'system', 'content': 'Отвечай только простым текстом.'}, {'role': 'user', 'content': prompt}],
                model=FIXED_MODEL,
                temperature=0.3,
            )
            data = chat.choices[0].message.content
            if '</think>' in data:
                data = data.split('</think>')[-1].strip()
        bot.reply_to(message, data)
    except Exception as e:
        bot.reply_to(message, f"Ошибка поиска: {e}")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    if not GEMINI_KEY:
        bot.reply_to(message, "Ошибка: GEMINI_API_KEY не задан!")
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        image_part = {
            'mime_type': 'image/jpeg',
            'data': downloaded_file
        }
        user_caption = message.caption or "Опиши, что изображено на картинке."
        
        response = gemini_vision_model.generate_content([user_caption, image_part])
        bot.reply_to(message, response.text)
    except Exception as e:
        bot.reply_to(message, f"Ошибка обработки фото через Gemini: {e}")

@bot.message_handler(commands=['code', 'sum', 'tr', 'fix'])
def handle_special_commands(message):
    if not groq_client:
        bot.reply_to(message, "Ошибка Groq API ключа!")
        return
    command = message.text.split()[0].replace('@' + bot.get_me().username, '')
    user_text = message.text.replace(command, '').strip()
    if not user_text:
        bot.reply_to(message, f"Напиши текст после команды {command}")
        return
    bot.send_chat_action(message.chat.id, 'typing')
    instructions = {
        '/code': 'Напиши или разбери код простым текстом.',
        '/sum': 'Сделай краткую выжимку простым текстом.',
        '/tr': 'Переведи текст на русский язык.',
        '/fix': 'Исправь ошибки в тексте.'
    }
    try:
        chat = groq_client.chat.completions.create(
            messages=[{'role': 'system', 'content': instructions.get(command, '')}, {'role': 'user', 'content': user_text}],
            model=FIXED_MODEL,
            temperature=0.3,
        )
        answer = chat.choices[0].message.content
        if '</think>' in answer:
            answer = answer.split('</think>')[-1].strip()
        bot.reply_to(message, answer)
    except Exception as e:
        bot.reply_to(message, f'Ошибка: {e}')

@bot.message_handler(func=lambda message: True, content_types=['text'])
def handle_text_message(message):
    if not groq_client:
        bot.reply_to(message, "Ошибка Groq API ключа!")
        return
    bot.send_chat_action(message.chat.id, 'typing')
    chat_id = message.chat.id
    if chat_id not in dialog_history:
        dialog_history[chat_id] = []
    history = dialog_history[chat_id]
    user_text = message.text
    if message.reply_to_message and message.reply_to_message.text:
        user_text = f"[Ответ на: '{message.reply_to_message.text}']. Текст: {user_text}"
    messages_payload = [{'role': 'system', 'content': 'Ты живой собеседник. Отвечай только простым текстом.'}]
    for msg in history:
        messages_payload.append(msg)
    messages_payload.append({'role': 'user', 'content': user_text})
    try:
        chat_response = groq_client.chat.completions.create(
            messages=messages_payload,
            model=FIXED_MODEL,
            temperature=0.7,
        )
        answer = chat_response.choices[0].message.content
        if '</think>' in answer: 
            answer = answer.split('</think>')[-1].strip()
        history.append({'role': 'user', 'content': user_text})
        history.append({'role': 'assistant', 'content': answer})
        
        if len(history) > MAX_HISTORY_LENGTH * 2:
            dialog_history[chat_id] = history[-(MAX_HISTORY_LENGTH * 2):]
            
        bot.reply_to(message, answer)
    except Exception as e:
        bot.reply_to(message, f'Ошибка: {e}')

if __name__ == '__main__':
    threading.Thread(target=run_web).start()
    print('Бот с огромной памятью (100 сообщений) запущен!')
    bot.infinity_polling(none_stop=True)
