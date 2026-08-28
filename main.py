import os
import threading
from flask import Flask
from groq import Groq
import telebot

# Получаем ключи и токены из переменных окружения Render
BOT_TOKEN = os.getenv('BOT_TOKEN')
GROQ_KEY = os.getenv('GROQ_KEY')

# Инициализируем бота и клиент Groq
bot = telebot.TeleBot(BOT_TOKEN)
groq_client = Groq(api_key=GROQ_KEY)

# Создаем мини-веб-сервер для Render (чтобы порт был открыт и не было ошибки 409)
app = Flask('')


@app.route('/')
def home():
  return 'Bot is active and running!'


def run_web():
  app.run(host='0.0.0.0', port=8080)


# Команда /start
@bot.message_handler(commands=['start'])
def send_welcome(message):
  bot.reply_to(
      message,
      'Привет! Я твой ИИ-помощник на базе Groq. Спроси меня о чём-нибудь!',
  )


# Обработка текстовых сообщений через Groq AI
@bot.message_handler(func=lambda message: True)
def handle_ai_message(message):
  # Отправляем пользователю статус «печатает...»
  bot.send_chat_action(message.chat.id, 'typing')

  try:
    # Запрос к нейросети Groq (используем быструю модель llama-3.3-70b-versatile)
    chat_completion = groq_client.chat.completions.create(
        messages=[{
            'role': 'user',
            'content': message.text,
        }],
        model='llama-3.3-70b-versatile',
    )
    answer = chat_completion.choices[0].message.content
    bot.reply_to(message, answer)
  except Exception as e:
    bot.reply_to(message, f'Произошла ошибка при обращении к ИИ: {e}')


# Запускаем веб-сервер и бота
if __name__ == '__main__':
  threading.Thread(target=run_web).start()
  print('🤖 ИИ-бот запущен!')
  bot.infinity_polling(none_stop=True)
