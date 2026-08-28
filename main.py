import os
import threading
import asyncio
import edge_tts
from flask import Flask
from groq import Groq
import telebot
from telebot.types import BotCommand

# Получаем токены из окружения
BOT_TOKEN = os.getenv('BOT_TOKEN')
GROQ_KEY = os.getenv('GROQ_KEY') or os.getenv('GROQ_API_KEY')

bot = telebot.TeleBot(BOT_TOKEN)
groq_client = Groq(api_key=GROQ_KEY) if GROQ_KEY else None
app = Flask('')

# Хранилище истории диалогов и фиксированная рабочая модель
dialog_history = {}
MAX_HISTORY_LENGTH = 10
FIXED_MODEL = 'llama-3.1-8b-instant'

@app.route('/')
def home():
    return 'Bot is active and running!'

def run_web():
    app.run(host='0.0.0.0', port=8080)

# Настройка меню команд в Telegram
bot.set_my_commands([
    BotCommand("help", "📋 Список всех команд"),
    BotCommand("code", "💻 Написать или разобрать код"),
    BotCommand("sum", "📝 Краткая выжимка текста"),
    BotCommand("tr", "🌐 Быстрый перевод"),
    BotCommand("fix", "✏️ Исправить ошибки в тексте"),
    BotCommand("tts", "🔊 Озвучить текст"),
    BotCommand("clear", "🧹 Сбросить контекст")
])

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    text = (
        "Привет! Я твой ИИ-помощник на базе Groq.\n\n"
        "✨ **Возможности:**\n"
        "• Помню контекст нашего разговора.\n"
        "• Понимаю, когда ты отвечаешь на сообщения (свайпы).\n"
        "• Работаю на стабильной и быстрой модели.\n\n"
        "📌 **Основные команды:**\n"
        "/code [задача] — написать/разобрать код\n"
        "/sum [текст] — сделать краткую выжимку\n"
        "/tr [текст] — быстрый перевод\n"
        "/fix [текст] — исправить ошибки\n"
        "/tts [текст] — озвучить текст голосом\n"
        "/clear — сбросить контекст диалога"
    )
    bot.reply_to(message, text)

@bot.message_handler(commands=['clear'])
def clear_history(message):
    chat_id = message.chat.id
    if chat_id in dialog_history:
        dialog_history[chat_id] = []
    bot.reply_to(message, "🧹 Контекст и память диалога сброшены!")

@bot.message_handler(commands=['tts'])
def handle_tts(message):
    text = message.text.replace('/tts', '').strip()
    if not text:
        bot.reply_to(message, 'Напиши текст для озвучки. Пример: /tts Привет!')
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

@bot.message_handler(commands=['code', 'sum', 'tr', 'fix'])
def handle_special_commands(message):
    if not groq_client:
        bot.reply_to(message, "Ошибка: API-ключ Groq не найден в настройках Render!")
        return

    command = message.text.split()[0].replace('@' + bot.get_me().username, '')
    user_text = message.text.replace(command, '').strip()

    if not user_text:
        bot.reply_to(message, f"Напиши текст после команды. Пример: {command} текст")
        return

    bot.send_chat_action(message.chat.id, 'typing')

    instructions = {
        '/code': 'Ты опытный программист. Напиши или разбери код четко и с минимальными пояснениями.',
        '/sum': 'Сделай краткую и емкую выжимку из этого текста, выделив главные мысли.',
        '/tr': 'Ты переводчик. Переведи этот текст на русский язык (или на английский, если он на русском). Выдай только перевод.',
        '/fix': 'Исправь все орфографические, пунктуационные и стилистические ошибки в тексте. Верни исправленный вариант.'
    }

    try:
        chat = groq_client.chat.completions.create(
            messages=[
                {'role': 'system', 'content': instructions.get(command, 'Помоги пользователю.')},
                {'role': 'user', 'content': user_text}
            ],
            model=FIXED_MODEL,
            temperature=0.3,
        )
        answer = chat.choices[0].message.content
        if '</think>' in answer:
            answer = answer.split('</think>')[-1].strip()
        bot.reply_to(message, answer)
    except Exception as e:
        bot.reply_to(message, f'Ошибка при обработке команды: {e}')

@bot.message_handler(func=lambda message: True, content_types=['text'])
def handle_text_message(message):
    if not groq_client:
        bot.reply_to(message, "Ошибка: API-ключ Groq не найден в настройках Render!")
        return

    bot.send_chat_action(message.chat.id, 'typing')
    
    chat_id = message.chat.id
    if chat_id not in dialog_history:
        dialog_history[chat_id] = []
        
    history = dialog_history[chat_id]
    user_text = message.text
    
    # Учитываем свайп (ответ на конкретное сообщение)
    if message.reply_to_message and message.reply_to_message.text:
        user_text = f"[Ответ на сообщение: '{message.reply_to_message.text}']. Текст: {user_text}"
    
    messages_payload = [
        {'role': 'system', 'content': 'Ты живой, адекватный и дружелюбный собеседник. Отвечай понятно, емко и по существу.'}
    ]
    
    for msg in history:
        messages_payload.append(msg)
        
    messages_payload.append({'role': 'user', 'content': user_text})
    
    try:
        chat = groq_client.chat.completions.create(
            messages=messages_payload,
            model=FIXED_MODEL,
            temperature=0.7,
        )
        answer = chat.choices[0].message.content
        
        if '</think>' in answer: 
            answer = answer.split('</think>')[-1].strip()
            
        history.append({'role': 'user', 'content': user_text})
        history.append({'role': 'assistant', 'content': answer})
        
        if len(history) > MAX_HISTORY_LENGTH * 2:
            dialog_history[chat_id] = history[-(MAX_HISTORY_LENGTH * 2):]
            
        bot.reply_to(message, answer)
    except Exception as e:
        bot.reply_to(message, f'Ошибка при обращении к ИИ: {e}')

if __name__ == '__main__':
    threading.Thread(target=run_web).start()
    print('🤖 Бот успешно запущен!')
    bot.infinity_polling(none_stop=True)
