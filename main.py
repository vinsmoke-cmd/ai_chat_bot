import os
import threading
import asyncio
import edge_tts
from flask import Flask
from groq import Groq
import telebot
from telebot.types import BotCommand
from googlesearch import search
import google.generativeai as genai
import requests
from bs4 import BeautifulSoup
import random

BOT_TOKEN = os.getenv('BOT_TOKEN')
GROQ_KEY = os.getenv('GROQ_KEY') or os.getenv('GROQ_API_KEY')
GEMINI_KEY = os.getenv('GEMINI_API_KEY')

bot = telebot.TeleBot(BOT_TOKEN)
groq_client = Groq(api_key=GROQ_KEY) if GROQ_KEY else None

if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)
    gemini_vision_model = genai.GenerativeModel('gemini-3.6-flash')
    gemini_text_model = genai.GenerativeModel('gemini-3.6-flash')

app = Flask('')

dialog_history = {}
MAX_HISTORY_LENGTH = 100
FIXED_MODEL = 'openai/gpt-oss-120b'

SYSTEM_INSTRUCTION = (
    'Ты русскоязычный помощник. Отвечай строго на русском языке. '
    'Запрещено использовать любые символы форматирования текста, такие как звездочки, решетки, подчеркивания и другие знаки разметки.'
)

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
    BotCommand("weather", "Узнать погоду"),
    BotCommand("fact", "Случайный факт"),
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
        "Привет! Твой ИИ-помощник.\n\n"
        "Команды:\n"
        "• /weather [город] - погода\n"
        "• /fact - случайный факт\n"
        "• /image [описание] - генерация картинки\n"
        "• /gemini [запрос] - текстовый Gemini\n"
        "• /search [запрос] - поиск в интернете\n"
        "• /tts [текст] - озвучка голосом\n"
        "• /clear - сбросить диалог"
    )
    bot.reply_to(message, text)

@bot.message_handler(commands=['clear'])
def clear_history(message):
    chat_id = message.chat.id
    if chat_id in dialog_history:
        dialog_history[chat_id] = []
    bot.reply_to(message, "Память диалога полностью сброшена.")

@bot.message_handler(commands=['weather'])
def handle_weather(message):
    city = message.text.replace('/weather', '').strip()
    if not city:
        bot.reply_to(message, "Укажи город. Пример: /weather Москва")
        return
    try:
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={requests.utils.quote(city)}&count=1&language=ru"
        geo_res = requests.get(geo_url, timeout=5).json()
        if not geo_res.get('results'):
            bot.reply_to(message, "Город не найден.")
            return
        lat = geo_res['results'][0]['latitude']
        lon = geo_res['results'][0]['longitude']
        name = geo_res['results'][0]['name']
        
        weather_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
        w_res = requests.get(weather_url, timeout=5).json()
        current = w_res.get('current_weather', {})
        temp = current.get('temperature')
        wind = current.get('windspeed')
        
        bot.reply_to(message, f"Погода в городе {name}: температура {temp} градусов, ветер {wind} м/с.")
    except Exception as e:
        bot.reply_to(message, f"Ошибка получения погоды: {e}")

@bot.message_handler(commands=['fact'])
def handle_fact(message):
    facts = [
        "Мед может храниться тысячами лет и не испортиться.",
        "У осьминогов три сердца и голубая кровь.",
        "Шампунь изначально делали из мыла и яичного порошка.",
        "Молния ударяет в одно и то же место более ста раз в секунду на Земле.",
        "Бананы технически являются ягодами, а клубника — нет."
    ]
    bot.reply_to(message, random.choice(facts))

@bot.message_handler(commands=['image'])
def handle_image_generation(message):
    prompt = message.text.replace('/image', '').strip()
    if not prompt:
        bot.reply_to(message, "Напиши, что нарисовать. Пример: /image космический кот")
        return
    bot.send_chat_action(message.chat.id, 'upload_photo')
    try:
        if groq_client:
            chat = groq_client.chat.completions.create(
                messages=[{
                    'role': 'system', 
                    'content': 'Translate the user prompt to detailed English for an AI image generator. Output ONLY the English prompt, nothing else.'
                }, {
                    'role': 'user', 
                    'content': prompt
                }],
                model=FIXED_MODEL,
                temperature=0.7,
            )
            english_prompt = chat.choices[0].message.content.strip()
            if '</think>' in english_prompt:
                english_prompt = english_prompt.split('</think>')[-1].strip()
        else:
            english_prompt = prompt

        encoded_prompt = requests.utils.quote(english_prompt)
        image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}"
        bot.send_photo(message.chat.id, image_url, caption=f"Запрос: {prompt}")
    except Exception as e:
        bot.reply_to(message, f"Ошибка генерации: {e}")

@bot.message_handler(commands=['gemini'])
def handle_gemini(message):
    if not GEMINI_KEY:
        bot.reply_to(message, "Ошибка: GEMINI_API_KEY не задан!")
        return
    query = message.text.replace('/gemini', '').strip()
    if not query:
        bot.reply_to(message, "Напиши запрос для Gemini.")
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        full_query = f"{SYSTEM_INSTRUCTION}\n\nЗапрос пользователя: {query}"
        response = gemini_text_model.generate_content(full_query)
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
        urls = list(search(query, num_results=3))
        if not urls:
            bot.reply_to(message, "Ничего не найдено в интернете.")
            return
        
        search_snippets = []
        headers = {'User-Agent': 'Mozilla/5.0'}
        for url in urls:
            try:
                r = requests.get(url, headers=headers, timeout=5)
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, 'html.parser')
                    for script in soup(["script", "style"]):
                        script.decompose()
                    text = soup.get_text(separator=' ', strip=True)
                    search_snippets.append(f"Источник ({url}):\n{text[:400]}...")
            except:
                continue

        if not search_snippets:
            bot.reply_to(message, "Не удалось прочитать найденные сайты.")
            return

        search_text = "\n\n".join(search_snippets)
        prompt = f"Запрос: '{query}'. Данные из интернета:\n\n{search_text}\n\nДай связный и понятный ответ."
        
        chat = groq_client.chat.completions.create(
            messages=[{'role': 'system', 'content': SYSTEM_INSTRUCTION}, {'role': 'user', 'content': prompt}],
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
        image_part = {'mime_type': 'image/jpeg', 'data': downloaded_file}
        user_caption = message.caption or "Опиши фото."
        full_prompt = f"{SYSTEM_INSTRUCTION}\n\n{user_caption}"
        response = gemini_vision_model.generate_content([full_prompt, image_part])
        bot.reply_to(message, response.text)
    except Exception as e:
        bot.reply_to(message, f"Ошибка при обработке фото: {e}")

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
        '/code': 'Напиши или разбери код.',
        '/sum': 'Сделай краткую выжимку.',
        '/tr': 'Переведи текст на русский язык.',
        '/fix': 'Исправь ошибки в тексте.'
    }
    specific_instruction = f"{SYSTEM_INSTRUCTION} Задача: {instructions.get(command, '')}"
    try:
        chat = groq_client.chat.completions.create(
            messages=[{'role': 'system', 'content': specific_instruction}, {'role': 'user', 'content': user_text}],
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
    messages_payload = [{'role': 'system', 'content': SYSTEM_INSTRUCTION}]
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
    print('Бот успешно запущен!')
    bot.infinity_polling(none_stop=True, timeout=60, long_polling_timeout=30)
