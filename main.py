import os
import threading
import asyncio
import edge_tts
from flask import Flask
from groq import Groq
import telebot
from telebot.types import BotCommand

# Получаем токены
BOT_TOKEN = os.getenv('BOT_TOKEN')
GROQ_KEY = os.getenv('GROQ_KEY') or os.getenv('GROQ_API_KEY')

bot = telebot.TeleBot(BOT_TOKEN)
groq_client = Groq(api_key=GROQ_KEY) if GROQ_KEY else None
app = Flask('')

# Хранилище истории диалогов
dialog_history = {}
MAX_HISTORY_LENGTH = 10

# Фиксированная отличная модель, которая общается нормально и без лишних «мыслей»
FIXED_MODEL = 'llama-3.1-8b-instant'

@app.route('/')
def home():
    return 'Bot is active and running!'

def run_web():
    app.run(host='0.0.0.0', port=8080)

# Установка меню команд
bot.set_my_commands([
    BotCommand("help", "📋 Список всех команд"),
    BotCommand("clear", "🧹 Очистить историю диалога"),
    BotCommand("tts", "🔊 Озвучить текст")
])

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    text = (
        "Привет! Я твой постоянный ИИ-помощник.\n\n"
        "✨ **Что я умею:**\n"
        "• Помню историю нашего диалога.\n"
        "• Понимаю твои ответы на конкретные сообщения (свайпы).\n"
        "• Общаюсь на одной стабильной и быстрой модели без лишней воды!\n\n"
        "📌 **Команды:**\n"
        "/clear — сбросить память диалога\n"
        "/tts [текст] — озвучить текст голосом"
    )
    bot.reply_to(message, text)

@bot.message_handler(commands=['clear'])
def clear_history(message):
    chat_id = message.chat.id
    if chat_id in dialog_history:
        dialog_history[chat_id] = []
    bot.reply_to(message, "🧹 История диалога очищена!")

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

def ask_groq_with_context(chat_id, user_text, reply_text=None):
    if not groq_client: return "Ошибка: нет ключа Groq."
    
    if chat_id not in dialog_history:
        dialog_history[chat_id] = []
        
    history = dialog_history[chat_id]
    
    current_prompt = user_text
    if reply_text:
        current_prompt = f"[Пользователь ответил на сообщение: '{reply_text}']. Текст сообщения: {user_text}"
    
    messages_payload = [
        {'role': 'system', 'content': 'Ты живой, веселый и дружелюбный собеседник в Telegram. Отвечай естественно, поддерживай контекст беседы, пиши понятно и без лишних робо-формулировок.'}
    ]
    
    for msg in history:
        messages_payload.append(msg)
        
    messages_payload.append({'role': 'user', 'content': current_prompt})
    
    try:
        chat = groq_client.chat.completions.create(
            messages=messages_payload,
            model=FIXED_MODEL,
            temperature=0.7,
        )
        ans = chat.choices[0].message.content
        if '</think>' in ans: 
            ans = ans.split('</think>')[-1].strip()
            
        history.append({'role': 'user', 'content': current_prompt})
        history.append({'role': 'assistant', 'content': ans})
        
        if len(history) > MAX_HISTORY_LENGTH * 2:
            dialog_history[chat_id] = history[-(MAX_HISTORY_LENGTH * 2):]
            
        return ans
    except Exception as e:
        return f"Ошибка ИИ: {e}"

@bot.message_handler(func=lambda message: True, content_types=['text'])
def handle_text_message(message):
    bot.send_chat_action(message.chat.id, 'typing')
    
    user_text = message.text
    reply_text = None
    
    if message.reply_to_message and message.reply_to_message.text:
        reply_text = message.reply_to_message.text
        
    answer = ask_groq_with_context(message.chat.id, user_text, reply_text)
    bot.reply_to(message, answer)

@bot.message_handler(content_types=['photo'])
def handle_photo_message(message):
    bot.send_chat_action(message.chat.id, 'typing')
    caption = message.caption if message.caption else "без подписи"
    bot.reply_to(message, f"Я получил твою картинку с подписью: «{caption}». Картинки храню во внимании, но эта текстовая модель общается словами!")

if __name__ == '__main__':
    threading.Thread(target=run_web).start()
    print('🤖 Бот с фиксированной моделью запущен!')
    bot.infinity_polling(none_stop=True)
