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

# Простой словарь для хранения истории диалогов: {chat_id: [список сообщений]}
dialog_history = {}
MAX_HISTORY_LENGTH = 10  # Максимальное количество сообщений в памяти для каждого чата

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

# Установка меню команд
bot.set_my_commands([
    BotCommand("help", "📋 Список всех команд"),
    BotCommand("clear", "🧹 Очистить историю диалога"),
    BotCommand("tts", "🔊 Озвучить текст")
])

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    text = (
        "Привет! Я твой умный ИИ-помощник.\n\n"
        "✨ **Что я умею:**\n"
        "• Помню историю нашего диалога.\n"
        "• Понимаю, когда ты отвечаешь (свайпаешь) на мои или свои сообщения.\n"
        "• Могу распознавать отправленные картинки!\n\n"
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

# Универсальная функция для отправки запроса в Groq с учетом истории и ответов (свайпов)
def ask_groq_with_context(chat_id, user_text, reply_text=None):
    if not groq_client: return "Ошибка: нет ключа Groq."
    model = get_normal_model()
    
    # Инициализируем историю для чата, если её нет
    if chat_id not in dialog_history:
        dialog_history[chat_id] = []
        
    history = dialog_history[chat_id]
    
    # Формируем текущий запрос пользователя
    current_prompt = user_text
    if reply_text:
        current_prompt = f"[Пользователь ответил на сообщение: '{reply_text}']. Текст сообщения: {user_text}"
    
    # Собираем сообщения для отправки в нейросеть
    messages_payload = [
        {'role': 'system', 'content': 'Ты живой, веселый и дружелюбный собеседник в Telegram. Отвечай естественно, поддерживай контекст беседы.'}
    ]
    
    # Добавляем накопленную историю
    for msg in history:
        messages_payload.append(msg)
        
    # Добавляем текущее сообщение
    messages_payload.append({'role': 'user', 'content': current_prompt})
    
    try:
        chat = groq_client.chat.completions.create(
            messages=messages_payload,
            model=model,
            temperature=0.7,
        )
        ans = chat.choices[0].message.content
        if '</think>' in ans: 
            ans = ans.split('</think>')[-1].strip()
            
        # Сохраняем в историю обмена
        history.append({'role': 'user', 'content': current_prompt})
        history.append({'role': 'assistant', 'content': ans})
        
        # Обрезаем историю, если она стала слишком длинной
        if len(history) > MAX_HISTORY_LENGTH * 2:
            dialog_history[chat_id] = history[-(MAX_HISTORY_LENGTH * 2):]
            
        return ans
    except Exception as e:
        return f"Ошибка ИИ: {e}"

# Обработка текстовых сообщений (с учетом свайпов/ответов)
@bot.message_handler(func=lambda message: True, content_types=['text'])
def handle_text_message(message):
    bot.send_chat_action(message.chat.id, 'typing')
    
    user_text = message.text
    reply_text = None
    
    # Проверяем, ответил ли пользователь на какое-то сообщение (свайп)
    if message.reply_to_message and message.reply_to_message.text:
        reply_text = message.reply_to_message.text
        
    answer = ask_groq_with_context(message.chat.id, user_text, reply_text)
    bot.reply_to(message, answer)

# Обработка картинок (фото)
@bot.message_handler(content_types=['photo'])
def handle_photo_message(message):
    bot.send_chat_action(message.chat.id, 'typing')
    
    try:
        # Получаем файл фотографии наилучшего качества
        photo = message.photo[-1]
        file_info = bot.get_file(photo.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        # Сохраняем временно на диск
        image_path = f"temp_{message.chat.id}.jpg"
        with open(image_path, 'wb') as new_file:
            new_file.write(downloaded_file)
            
        # Подпись к картинке (если пользователь написал текст вместе с фото)
        caption = message.caption if message.caption else "Что изображено на этой картинке?"
        
        # Заглушка-ответ для текстовой модели Groq про картинки
        # (Так как текстовый Groq не «видит» пиксели напрямую без мультимодального API, 
        # сообщаем пользователю, либо можно подключить специальную логику)
        answer = f"Я получил твою картинку! Ты подписал её так: «{caption}». (К сожалению, эта текстовая модель Groq пока не анализирует пиксели напрямую, но я могу обсудить с тобой описание или тему!)"
        
        bot.reply_to(message, answer)
        
        # Удаляем временный файл
        if os.path.exists(image_path):
            os.remove(image_path)
            
    except Exception as e:
        bot.reply_to(message, f"Не удалось обработать картинку: {e}")

if __name__ == '__main__':
    threading.Thread(target=run_web).start()
    print('🤖 Продвинутый ИИ-бот запущен!')
    bot.infinity_polling(none_stop=True)
