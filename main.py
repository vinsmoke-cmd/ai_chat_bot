import os
import threading
import telebot
from flask import Flask

# Получаем ключи и токены из переменных окружения Render
BOT_TOKEN = os.getenv('BOT_TOKEN')
GROQ_KEY = os.getenv('GROQ_KEY')
GEMINI_KEY = os.getenv('GEMINI_KEY')

# Инициализируем бота
bot = telebot.TeleBot(BOT_TOKEN)

# Создаем мини-веб-сервер для Render, чтобы он видел открытый порт 8080
# и не устраивал ложные перезагрузки, вызывающие ошибку 409
app = Flask('')


@app.route('/')
def home():
  return 'Bot is active and running!'


def run_web():
  app.run(host='0.0.0.0', port=8080)


# Базовый обработчик команды /start
@bot.message_handler(commands=['start'])
def send_welcome(message):
  bot.reply_to(
      message, 'Привет! Я твой ИИ-бот и я успешно запущен на сервере Render.'
  )


# Пример эха или вашего основного функционала (добавьте сюда вашу логику работы с Groq/Gemini)
@bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
  bot.reply_to(message, f'Вы написали: {message.text}')


# Запускаем веб-сервер в отдельном потоке
if __name__ == '__main__':
  # Запуск фонового веб-сервера для Render
  threading.Thread(target=run_web).start()

  print('🤖 Бот запущен!')
  # Запуск бесконечного получения обновлений от Telegram
  bot.infinity_polling(none_stop=True)
    
