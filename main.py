import os
import threading
from flask import Flask
from groq import Groq
import telebot

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


def get_fast_model():
  """Ищет самую быструю и прямую модель без долгих рассуждений."""
  if not groq_client:
    return 'llama-3.1-8b-instant'
    
  try:
    # Получаем список всех моделей
    available_models = [m.id for m in groq_client.models.list().data]
    
    # Наш приоритетный список самых быстрых моделей для прямых ответов
    fast_priority = [
        'llama-3.1-8b-instant',
        'llama3-8b-8192',
        'mixtral-8x7b-32768',
        'gemma2-9b-it'
    ]
    
    # Берем первую доступную из нашего списка
    for model in fast_priority:
      if model in available_models:
        return model
        
    # Если их нет, ищем любую легкую модель (8b, 7b, 9b)
    for model in available_models:
      if any(size in model.lower() for size in ['8b', '7b', '9b']):
        return model
        
    return available_models[0]
  except Exception as e:
    print(f"Ошибка выбора модели: {e}")
    return 'llama-3.1-8b-instant'


@bot.message_handler(commands=['start'])
def send_welcome(message):
  bot.reply_to(message, 'Привет! Я быстрый ИИ-помощник. Жду твой вопрос.')


@bot.message_handler(func=lambda message: True)
def handle_ai_message(message):
  bot.send_chat_action(message.chat.id, 'typing')

  if not groq_client:
    bot.reply_to(message, 'Ошибка: API-ключ Groq не найден!')
    return

  selected_model = get_fast_model()

  try:
    chat_completion = groq_client.chat.completions.create(
        messages=[
            # Системный промпт заставляет ИИ отвечать без лишней воды
            {
                'role': 'system',
                'content': 'Ты ИИ-ассистент в Telegram. Отвечай максимально кратко, по делу, прямо на вопрос. Никаких долгих рассуждений, размышлений вслух и лишних приветствий.'
            },
            {
                'role': 'user',
                'content': message.text,
            }
        ],
        model=selected_model,
        # Немного снижаем температуру, чтобы ответы были более конкретными
        temperature=0.5,
    )
    answer = chat_completion.choices[0].message.content
    bot.reply_to(message, answer)
  except Exception as e:
    bot.reply_to(message, f'Произошла ошибка при обращении к ИИ (Модель: {selected_model}): {e}')


if __name__ == '__main__':
  threading.Thread(target=run_web).start()
  print('🤖 Быстрый ИИ-бот запущен!')
  bot.infinity_polling(none_stop=True)
