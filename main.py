import os
import threading
from flask import Flask
from groq import Groq
import telebot

# Получаем токены из переменных окружения
BOT_TOKEN = os.getenv('BOT_TOKEN')
GROQ_KEY = os.getenv('GROQ_KEY') or os.getenv('GROQ_API_KEY')

bot = telebot.TeleBot(BOT_TOKEN)

# Инициализируем Groq
groq_client = Groq(api_key=GROQ_KEY) if GROQ_KEY else None

app = Flask('')


@app.route('/')
def home():
  return 'Bot is active and running!'


def run_web():
  app.run(host='0.0.0.0', port=8080)


def get_available_model():
  """Запрашивает список доступных моделей у Groq и выбирает рабочую."""
  if not groq_client:
    return 'llama-3.1-8b-instant' # Запасной вариант
  try:
    # Получаем список всех активных моделей от API
    models = groq_client.models.list().data
    
    # Ищем первую попавшуюся модель со словом 'llama'
    for m in models:
      if 'llama' in m.id.lower():
        return m.id
        
    # Если 'llama' почему-то нет, берем самую первую из списка
    if models:
      return models[0].id
  except Exception as e:
    print(f"Ошибка при получении списка моделей: {e}")
    
  return 'llama-3.1-8b-instant' # Фолбэк на случай ошибки


@bot.message_handler(commands=['start'])
def send_welcome(message):
  bot.reply_to(
      message,
      'Привет! Я твой ИИ-помощник на базе Groq. Спроси меня о чём-нибудь!',
  )


@bot.message_handler(func=lambda message: True)
def handle_ai_message(message):
  bot.send_chat_action(message.chat.id, 'typing')

  if not groq_client:
    bot.reply_to(
        message,
        'Ошибка: API-ключ Groq не найден в переменных окружения Render!',
    )
    return

  # Динамически получаем доступную модель перед каждым запросом
  selected_model = get_available_model()

  try:
    chat_completion = groq_client.chat.completions.create(
        messages=[{
            'role': 'user',
            'content': message.text,
        }],
        model=selected_model,
    )
    answer = chat_completion.choices[0].message.content
    bot.reply_to(message, answer)
  except Exception as e:
    bot.reply_to(message, f'Произошла ошибка при обращении к ИИ (Модель: {selected_model}): {e}')


if __name__ == '__main__':
  threading.Thread(target=run_web).start()
  print('🤖 ИИ-бот запущен!')
  bot.infinity_polling(none_stop=True)
