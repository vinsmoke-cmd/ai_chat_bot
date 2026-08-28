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

@app.route('/')
def home():
    return 'Bot is active and running!'

def run_web():
    app.run(host='0.0.0.0', port=8080)

def get_normal_model():
    if not groq_client: return 'llama-3.1-8b-instant'
    try:
        available_models = [m.id for m in groq_client.models.list().data]
        valid_models = [m for m in available_models if 'deepseek' not in m.lower() and 'r1' not in m.lower()]
        for model in ['llama-3.1-8b-instant', 'llama3-8b-8192', 'mixtral-8x7b-32768']:
            if model in valid_models: return model
        return valid_models[0] if valid_models else 'llama-3.1-8b-instant'
    except:
        return 'llama-3.1-8b-instant'

# Установка меню команд в Telegram
bot.set_my_commands([
    BotCommand("help", "📋 Список всех команд"),
    BotCommand("code", "💻 Написать или разобрать код"),
    BotCommand("sum", "📝 Краткая выжимка текста"),
    BotCommand("tr", "🌐 Быстрый перевод"),
    BotCommand("fix", "✏️ Исправить ошибки"),
    BotCommand("tts", "🔊 Озвучить текст")
])

def ask_groq(prompt, system_instruction):
    if not groq_client: return "Ошибка: нет ключа Groq."
    model = get_normal_model()
    try:
        chat = groq_client.chat.completions.create(
            messages=[
                {'role': 'system', 'content': system_instruction},
                {'role': 'user', 'content': prompt}
            ],
            model=model,
            temperature=0.7,
        )
        ans = chat.choices[0].message.content
        if '</think>' in ans: ans = ans.split('</think>')[-1].strip()
        return ans
    except Exception as e:
        return f"Ошибка ИИ: {e}"

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    text = (
        "Привет! Вот что я умею:\n\n"
        "/tts [текст] - 🔊 Озвучить текст\n"
        "/code [задача] - 💻 Помощь с кодом\n"
        "/sum [текст] - 📝 Сделать краткую выжимку\n"
        "/tr [текст] - 🌐 Перевести на русский (или на английский)\n"
        "/fix [текст] - ✏️ Исправить ошибки в тексте\n\n"
        "Или просто напиши мне сообщение для обычного общения!"
    )
    bot.reply_to(message, text)

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
def handle_text_commands(message):
    command = message.text.split()[0]
    text = message.text.replace(command, '').strip()
    
    if not text:
        bot.reply_to(message, f"Напиши текст после команды. Пример: {command} текст")
        return
        
    bot.send_chat_action(message.chat.id, 'typing')
    
    instructions = {
        '/code': 'Ты опытный программист. Отвечай только кодом с краткими комментариями.',
        '/sum': 'Сделай максимально краткую и понятную выжимку из этого текста, выдели главное.',
        '/tr': 'Ты профессиональный переводчик. Переведи текст на русский (если он на другом языке) или на английский (если он на русском). Ничего не добавляй от себя.',
        '/fix': 'Исправь грамматические, пунктуационные и стилистические ошибки в этом тексте. Верни только исправленный текст.'
    }
    
    answer = ask_groq(text, instructions[command])
    bot.reply_to(message, answer)

@bot.message_handler(func=lambda message: True)
def handle_ai_message(message):
    bot.send_chat_action(message.chat.id, 'typing')
    instruction = 'Ты живой, веселый и дружелюбный собеседник в Telegram. Пиши по делу, но не будь слишком сухим.'
    answer = ask_groq(message.text, instruction)
    bot.reply_to(message, answer)

if __name__ == '__main__':
    threading.Thread(target=run_web).start()
    print('🤖 Мульти-бот запущен!')
    bot.infinity_polling(none_stop=True)
