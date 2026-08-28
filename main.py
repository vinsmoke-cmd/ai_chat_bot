import os
import telebot
import re
import io
import requests
import urllib.parse
from PIL import Image
from groq import Groq
import google.generativeai as genai
import asyncio
import edge_tts
from duckduckgo_search import DDGS
from telebot.types import BotCommand
from dotenv import load_dotenv

# Загружаем переменные из локального файла .env (если он есть)
load_dotenv()

# Ключи и токены берутся из защищенного окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_KEY = os.getenv("GROQ_KEY")
GEMINI_KEY = os.getenv("GEMINI_KEY")

bot = telebot.TeleBot(BOT_TOKEN)
groq_client = Groq(api_key=GROQ_KEY)

# Автоматическая настройка выпадающего меню команд в Telegram
try:
    bot.set_my_commands([
        BotCommand("help", "📋 Список всех команд"),
        BotCommand("code", "💻 Написать или разобрать код"),
        BotCommand("draw", "🎨 Сгенерировать картинку"),
        BotCommand("sum", "📝 Краткая выжимка текста"),
        BotCommand("tr", "🌐 Быстрый перевод"),
        BotCommand("fix", "✏️ Исправить ошибки в тексте"),
        BotCommand("tts", "🔊 Озвучить текст"),
        BotCommand("search", "🔎 Поиск в интернете"),
        BotCommand("role", "🎭 Изменить режим/роль ИИ"),
        BotCommand("clear", "🧹 Сбросить контекст диалога")
    ])
except Exception as e:
    print(f"⚠️ Не удалось настроить меню команд: {e}")

# Базовая системная инструкция
BASE_SYSTEM_PROMPT = (
    "Ты полезный ИИ-ассистент. Отвечай максимально кратко, прямо и по делу. "
    "Категорически запрещено упоминать слово Groq, утверждать, что тебя создала компания Groq, "
    "а также писать свои мысли, вводные рассуждения, лекции "
    "или таблицы, если пользователь сам об этом не просил."
)

ROLES = {
    "coder": "Ты Senior Developer. Пиши только чистый, рабочий код с минимальными комментариями.",
    "teacher": "Ты терпеливый преподаватель. Объясняй темы простыми словами, на понятных жизненных примерах.",
    "english": "You are a friendly English conversation partner. Answer exclusively in English to help practice.",
    "normal": BASE_SYSTEM_PROMPT
}

user_roles = {}

# ==========================================
# ⚙️ НАСТРОЙКА GEMINI
# ==========================================
genai.configure(api_key=GEMINI_KEY)
gemini_model = genai.GenerativeModel(
    model_name="models/gemini-1.5-flash-latest",
    system_instruction=BASE_SYSTEM_PROMPT,
    generation_config={"max_output_tokens": 1500, "temperature": 0.3}
)

# ==========================================
# ⚙️ НАСТРОЙКА GROQ И ПАМЯТИ
# ==========================================
user_memory = {}
MAX_HISTORY = 8 
active_groq_model = "llama-3.3-70b-versatile"

def send_long_message(message, text):
    MAX_LENGTH = 4000
    if len(text) <= MAX_LENGTH:
        bot.reply_to(message, text, parse_mode="Markdown")
        return
    chunks = [text[i:i + MAX_LENGTH] for i in range(0, len(text), MAX_LENGTH)]
    bot.reply_to(message, chunks[0], parse_mode="Markdown")
    for chunk in chunks[1:]:
        bot.send_message(message.chat.id, chunk, parse_mode="Markdown")

def process_ai_request(chat_id, user_text, custom_system_prompt=None):
    sys_prompt = custom_system_prompt or user_roles.get(chat_id, BASE_SYSTEM_PROMPT)
    if chat_id not in user_memory or user_memory[chat_id][0]["content"] != sys_prompt:
        user_memory[chat_id] = [{"role": "system", "content": sys_prompt}]

    user_memory[chat_id].append({"role": "user", "content": user_text})
    if len(user_memory[chat_id]) > MAX_HISTORY + 1:
        user_memory[chat_id] = [user_memory[chat_id][0]] + user_memory[chat_id][-MAX_HISTORY:]

    try:
        chat_completion = groq_client.chat.completions.create(
            messages=user_memory[chat_id],
            model=active_groq_model,
            max_tokens=1500,
            temperature=0.3
        )
        response_text = chat_completion.choices[0].message.content
        clean_text = re.sub(r'<think>.*?</think>', '', response_text, flags=re.DOTALL).strip()
        if clean_text:
            user_memory[chat_id].append({"role": "assistant", "content": clean_text})
            return clean_text
    except Exception:
        pass

    try:
        prompt_history = "".join([f"{'Пользователь' if m['role'] == 'user' else 'Ассистент'}: {m['content']}\n" for m in user_memory[chat_id][1:]])
        gemini_response = gemini_model.generate_content(prompt_history)
        clean_text = re.sub(r'<think>.*?</think>', '', gemini_response.text, flags=re.DOTALL).strip()
        if clean_text:
            user_memory[chat_id].append({"role": "assistant", "content": clean_text})
            return clean_text
    except Exception as e:
        return f"❌ Ошибка ИИ: {str(e)}"

    return "⚠️ Не удалось получить ответ."

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    help_text = (
        "🤖 **Команды бота:**\n\n"
        "💻 `/code [задача]` — код\n"
        "📝 `/sum [текст]` — выжимка\n"
        "🌐 `/tr [текст]` — перевод\n"
        "✏️ `/fix [текст]` — исправить ошибки\n"
        "🔎 `/search [запрос]` — поиск в интернете\n"
        "🔊 `/tts [текст]` — озвучить\n"
        "🎭 `/role [coder/teacher/english/normal]` — роль\n"
        "🎨 `/draw [описание]` — картинка\n"
        "🧹 `/clear` — сбросить контекст\n"
        "📷 Отправь фото для анализа"
    )
    bot.reply_to(message, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['clear', 'reset'])
def clear_history(message):
    chat_id = message.chat.id
    current_prompt = user_roles.get(chat_id, BASE_SYSTEM_PROMPT)
    user_memory[chat_id] = [{"role": "system", "content": current_prompt}]
    bot.reply_to(message, "🧹 **Контекст очищен!**", parse_mode="Markdown")

@bot.message_handler(commands=['code'])
def handle_code_command(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "⚠️ Напишите задачу: `/code парсер на python`", parse_mode="Markdown")
        return
    bot.send_chat_action(message.chat.id, 'typing')
    send_long_message(message, process_ai_request(message.chat.id, f"Напиши чистый код:\n\n{parts[1]}"))

@bot.message_handler(commands=['sum'])
def handle_summarize(message):
    parts = message.text.split(maxsplit=1)
    text = parts[1] if len(parts) > 1 else (message.reply_to_message.text if message.reply_to_message else "")
    if not text:
        bot.reply_to(message, "⚠️ Укажите текст или ответьте на сообщение.", parse_mode="Markdown")
        return
    bot.send_chat_action(message.chat.id, 'typing')
    send_long_message(message, process_ai_request(message.chat.id, f"Сделай краткую выжимку:\n\n{text}"))

@bot.message_handler(commands=['tr'])
def handle_translate(message):
    parts = message.text.split(maxsplit=1)
    text = parts[1] if len(parts) > 1 else (message.reply_to_message.text if message.reply_to_message else "")
    if not text:
        bot.reply_to(message, "⚠️ Укажите текст для перевода.", parse_mode="Markdown")
        return
    bot.send_chat_action(message.chat.id, 'typing')
    send_long_message(message, process_ai_request(message.chat.id, f"Переведи (если русский - на английский, иначе на русский): \n\n{text}"))

@bot.message_handler(commands=['fix'])
def handle_fix_text(message):
    parts = message.text.split(maxsplit=1)
    text = parts[1] if len(parts) > 1 else (message.reply_to_message.text if message.reply_to_message else "")
    if not text:
        bot.reply_to(message, "⚠️ Укажите текст.", parse_mode="Markdown")
        return
    bot.send_chat_action(message.chat.id, 'typing')
    send_long_message(message, process_ai_request(message.chat.id, f"Исправь ошибки:\n\n{text}"))

@bot.message_handler(commands=['role'])
def handle_role(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or parts[1].lower() not in ROLES:
        bot.reply_to(message, "⚠️ Роли: coder, teacher, english, normal", parse_mode="Markdown")
        return
    role = parts[1].lower()
    user_roles[message.chat.id] = ROLES[role]
    user_memory[message.chat.id] = [{"role": "system", "content": ROLES[role]}]
    bot.reply_to(message, f"🎭 Роль изменена на: `{role}`", parse_mode="Markdown")

async def generate_voice_file(text, output_path):
    communicate = edge_tts.Communicate(text, "ru-RU-DmitryNeural")
    await communicate.save(output_path)

@bot.message_handler(commands=['tts'])
def handle_tts(message):
    parts = message.text.split(maxsplit=1)
    text = parts[1] if len(parts) > 1 else (message.reply_to_message.text if message.reply_to_message else "")
    if not text:
        bot.reply_to(message, "⚠️ Напишите текст для озвучки.", parse_mode="Markdown")
        return
    bot.send_chat_action(message.chat.id, 'record_audio')
    file_path = f"voice_{message.chat.id}.ogg"
    try:
        asyncio.run(generate_voice_file(text[:1000], file_path))
        with open(file_path, 'rb') as f:
            bot.send_voice(message.chat.id, f, reply_to_message_id=message.message_id)
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка озвучки: {str(e)}")

@bot.message_handler(commands=['search'])
def handle_search(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "⚠️ Укажите запрос: `/search курс доллара`", parse_mode="Markdown")
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        results = list(DDGS().text(parts[1], max_results=3))
        if not results:
            bot.reply_to(message, "🔍 Ничего не найдено.")
            return
        context = "\n".join([f"- {r['title']}: {r['body']}" for r in results])
        send_long_message(message, process_ai_request(message.chat.id, f"Ответь на основе поиска:\n{context}"))
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка поиска: {str(e)}")

@bot.message_handler(commands=['draw'])
def generate_image(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "⚠️ Напишите описание: `/draw кот`", parse_mode="Markdown")
        return
    bot.send_chat_action(message.chat.id, 'upload_photo')
    try:
        url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(parts[1])}?width=1024&height=1024&nologo=true"
        res = requests.get(url, timeout=30)
        if res.status_code == 200:
            bot.send_photo(message.chat.id, res.content, caption=f"🎨 {parts[1]}", parse_mode="Markdown")
        else:
            bot.reply_to(message, "❌ Не удалось создать картинку.")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {str(e)}")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        image = Image.open(io.BytesIO(bot.download_file(file_info.file_path)))
        image.thumbnail((1024, 1024))
        caption = message.caption or "Опиши картинку."
        response = gemini_model.generate_content([caption, image])
        if response.text:
            clean = re.sub(r'<think>.*?</think>', '', response.text, flags=re.DOTALL).strip()
            send_long_message(message, clean)
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка фото: {str(e)}")

@bot.message_handler(func=lambda message: True)
def answer_ai(message):
    bot.send_chat_action(message.chat.id, 'typing')
    text = message.text
    if message.reply_to_message and message.reply_to_message.text:
        text = f'[В ответ на: "{message.reply_to_message.text}"]\n{message.text}'
    send_long_message(message, process_ai_request(message.chat.id, text))

if __name__ == "__main__":
    print("🤖 Бот запущен!")
    bot.infinity_polling()
