import os
import telebot
import requests
import asyncio
import threading
import tempfile
from flask import Flask
from groq import Groq
from bs4 import BeautifulSoup
from pypdf import PdfReader
import edge_tts
from duckduckgo_search import DDGS

try:
    from tavily import TavilyClient
except ImportError:
    TavilyClient = None

# ==========================================
# НАСТРОЙКИ КЛЮЧЕЙ
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
HF_TOKEN = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

bot = telebot.TeleBot(BOT_TOKEN)
groq_client = Groq(api_key=GROQ_API_KEY)
tavily_client = TavilyClient(api_key=TAVILY_API_KEY) if (TavilyClient and TAVILY_API_KEY) else None

# Память для диалогов пользователей
user_histories = {}

# ==========================================
# ВЕБ-СЕРВЕР ДЛЯ ХОСТИНГА
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Бот работает!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================

def ask_groq_with_history(user_id, prompt):
    """Общение с учетом истории сообщений (Модель Mixtral)"""
    if user_id not in user_histories:
        user_histories[user_id] = [{"role": "system", "content": "Ты полезный и дружелюбный ИИ-ассистент."}]
    
    user_histories[user_id].append({"role": "user", "content": prompt})
    
    # Ограничиваем историю 10 последними сообщениями
    if len(user_histories[user_id]) > 11:
        user_histories[user_id] = [user_histories[user_id][0]] + user_histories[user_id][-10:]
        
    try:
        response = groq_client.chat.completions.create(
            model="mixtral-8x7b-32768", # Отличная альтернатива Llama
            messages=user_histories[user_id]
        )
        answer = response.choices[0].message.content
        user_histories[user_id].append({"role": "assistant", "content": answer})
        return answer
    except Exception as e:
        return f"Ошибка текстовой нейросети: {e}"

def perform_web_search(query):
    """Улучшенный поиск в интернете"""
    results_text = ""
    # Пробуем Tavily (если ключ установлен)
    if tavily_client:
        try:
            response = tavily_client.search(query=query, max_results=3)
            for res in response.get('results', []):
                results_text += f"- {res.get('title')}: {res.get('content')}\n"
        except Exception:
            pass
            
    # Переключаемся на DuckDuckGo
    if not results_text:
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=3))
                for res in results:
                    title = res.get('title', 'Без заголовка')
                    body = res.get('body', '')[:300]
                    results_text += f"- {title}: {body}...\n"
        except Exception as e:
            results_text = f"Не удалось выполнить автоматический поиск: {e}"
            
    return results_text

def generate_image_hf(prompt):
    """Генерация изображения (Hugging Face)"""
    try:
        API_URL = "https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell"
        headers = {"Authorization": f"Bearer {HF_TOKEN}"}
        response = requests.post(API_URL, headers=headers, json={"inputs": prompt}, timeout=40)
        if response.status_code == 200:
            return response.content
        return None
    except Exception:
        return None

def analyze_image_hf(image_bytes):
    """Распознавание картинки (Hugging Face)"""
    try:
        API_URL = "https://api-inference.huggingface.co/models/Salesforce/blip-image-captioning-large"
        headers = {"Authorization": f"Bearer {HF_TOKEN}"}
        response = requests.post(API_URL, headers=headers, data=image_bytes)
        if response.status_code == 200:
            result = response.json()
            if isinstance(result, list) and 'generated_text' in result[0]:
                desc = result[0]['generated_text']
                return ask_groq_with_history(0, f"Переведи описание картинки на русский и детально расскажи о ней: {desc}")
        return "Не удалось распознать изображение."
    except Exception as e:
        return f"Ошибка анализа фото: {e}"

async def generate_audio(text, output_file):
    communicate = edge_tts.Communicate(text, "ru-RU-SvetlanaNeural")
    await communicate.save(output_file)

# ==========================================
# КОМАНДЫ И ОБРАБОТЧИКИ
# ==========================================

@bot.message_handler(commands=['start', 'help'])
def help_cmd(message):
    help_text = (
        "Привет! Я ИИ-ассистент 🤖\n\n"
        "**Список команд:**\n"
        "💬 Просто пиши текст для общения\n"
        "🔍 `/search <запрос>` — поиск в интернете\n"
        "🌤 `/weather <город>` — подробная погода\n"
        "🖼 `/image <описание>` — создать картинку\n"
        "🧠 `/gemini <запрос>` — спросить ИИ\n"
        "💡 `/fact` — случайный факт\n"
        "💻 `/code <задача>` — работа с кодом\n"
        "📄 `/sum <ссылка>` — краткая выжимка статьи\n"
        "🌐 `/tr <текст>` — перевод на английский\n"
        "✍️ `/fix <текст>` — исправить ошибки\n"
        "🎙 `/tts <текст>` — озвучить текст\n"
        "🧹 `/clear` — очистить память диалога"
    )
    bot.reply_to(message, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['clear'])
def clear_cmd(message):
    if message.chat.id in user_histories:
        del user_histories[message.chat.id]
    bot.reply_to(message, "🧹 Память диалога успешно очищена!")

@bot.message_handler(commands=['fact'])
def fact_cmd(message):
    msg = bot.reply_to(message, "⏳ Ищу интересный факт...")
    fact = ask_groq_with_history(message.chat.id, "Расскажи один очень интересный, редкий и увлекательный научный факт на русском языке. Будь краток.")
    bot.edit_message_text(fact, chat_id=message.chat.id, message_id=msg.message_id)

@bot.message_handler(commands=['weather'])
def weather_cmd(message):
    city = message.text.replace("/weather", "", 1).strip()
    if not city:
        bot.reply_to(message, "Укажи город. Пример: `/weather Москва`", parse_mode="Markdown")
        return
    try:
        # Улучшенный, структурированный формат погоды
        params = {
            'format': '🌍 Город: %l\n🌤 Погода: %C %c\n🌡 Температура: %t (ощущается как %F)\n💨 Ветер: %w\n💧 Влажность: %h\n☔ Осадки: %p',
            'lang': 'ru'
        }
        resp = requests.get(f"https://wttr.in/{city}", params=params, timeout=5)
        if resp.status_code == 200:
            bot.reply_to(message, f"**Текущая сводка:**\n\n{resp.text.strip()}", parse_mode="Markdown")
        else:
            bot.reply_to(message, "Не удалось найти город. Проверь название.")
    except Exception as e:
        bot.reply_to(message, f"Ошибка сервиса погоды: {e}")

@bot.message_handler(commands=['search'])
def search_cmd(message):
    query = message.text.replace("/search", "", 1).strip()
    if not query:
        bot.reply_to(message, "Напиши запрос. Пример: `/search свежие новости науки`", parse_mode="Markdown")
        return
    
    msg = bot.reply_to(message, f"🔍 Ищу в интернете: *{query}*", parse_mode="Markdown")
    data = perform_web_search(query)
    
    if "Не удалось выполнить" in data:
        bot.edit_message_text(data, chat_id=message.chat.id, message_id=msg.message_id)
        return
        
    prompt = f"Пользователь ищет: '{query}'. На основе свежих данных из интернета составь подробный и понятный ответ:\n\n{data}"
    reply = ask_groq_with_history(message.chat.id, prompt)
    bot.edit_message_text(reply, chat_id=message.chat.id, message_id=msg.message_id)

@bot.message_handler(commands=['gemini', 'code', 'sum', 'tr', 'fix'])
def ai_tools_cmd(message):
    text_parts = message.text.split(" ", 1)
    if len(text_parts) < 2:
        bot.reply_to(message, f"Напиши текст после команды `{text_parts[0]}`", parse_mode="Markdown")
        return
    
    cmd = text_parts[0]
    content = text_parts[1]
    msg = bot.reply_to(message, "⏳ Обрабатываю запрос...")
    
    if cmd == '/gemini':
        prompt = content
    elif cmd == '/code':
        prompt = f"Напиши качественный код и объясни решение для задачи: {content}"
    elif cmd == '/sum':
        prompt = f"Сделай краткую, структурированную выжимку:\n\n{content}"
    elif cmd == '/tr':
        prompt = f"Переведи этот текст на английский язык:\n\n{content}"
    elif cmd == '/fix':
        prompt = f"Исправь грамматические и стилистические ошибки, сделай текст лучше:\n\n{content}"
    else:
        prompt = content
        
    reply = ask_groq_with_history(message.chat.id, prompt)
    bot.edit_message_text(reply, chat_id=message.chat.id, message_id=msg.message_id)

@bot.message_handler(commands=['image'])
def image_cmd(message):
    prompt = message.text.replace("/image", "", 1).strip()
    if not prompt:
        bot.reply_to(message, "Опиши картинку. Пример: `/image киберпанк город`", parse_mode="Markdown")
        return
    msg = bot.reply_to(message, "🎨 Генерирую изображение...")
    img_bytes = generate_image_hf(prompt)
    if img_bytes:
        bot.send_photo(message.chat.id, img_bytes, caption=f"🖼 По запросу: {prompt}")
        bot.delete_message(message.chat.id, msg.message_id)
    else:
        bot.edit_message_text("Не удалось сгенерировать картинку. Сервер временно перегружен.", chat_id=message.chat.id, message_id=msg.message_id)

@bot.message_handler(commands=['tts'])
def tts_cmd(message):
    text_to_speak = message.text.replace("/tts", "", 1).strip()
    if not text_to_speak:
        bot.reply_to(message, "Напиши текст. Пример: `/tts Привет мир`", parse_mode="Markdown")
        return
    msg = bot.reply_to(message, "⏳ Создаю аудио...")
    audio_path = tempfile.mktemp(suffix=".mp3")
    asyncio.run(generate_audio(text_to_speak, audio_path))
    with open(audio_path, 'rb') as audio:
        bot.send_voice(message.chat.id, audio)
    bot.delete_message(message.chat.id, msg.message_id)
    os.remove(audio_path)

@bot.message_handler(content_types=['text'])
def handle_text(message):
    text = message.text
    if "http://" in text or "https://" in text:
        msg = bot.reply_to(message, "⏳ Читаю веб-страницу...")
        try:
            url = [w for w in text.split() if w.startswith("http")][0]
            resp = requests.get(url, timeout=10)
            soup = BeautifulSoup(resp.text, 'html.parser')
            page_text = soup.get_text(separator=' ', strip=True)[:3000]
            reply = ask_groq_with_history(message.chat.id, f"Сделай выжимку статьи по ссылке:\n\n{page_text}")
            bot.edit_message_text(reply, chat_id=message.chat.id, message_id=msg.message_id)
            return
        except Exception as e:
            bot.edit_message_text(f"Ошибка чтения ссылки: {e}", chat_id=message.chat.id, message_id=msg.message_id)
            return

    msg = bot.reply_to(message, "⏳ Думаю...")
    reply = ask_groq_with_history(message.chat.id, text)
    bot.edit_message_text(reply, chat_id=message.chat.id, message_id=msg.message_id)

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    msg = bot.reply_to(message, "⏳ Изучаю фото...")
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded = bot.download_file(file_info.file_path)
        answer = analyze_image_hf(downloaded)
        bot.edit_message_text(answer, chat_id=message.chat.id, message_id=msg.message_id)
    except Exception as e:
        bot.edit_message_text(f"Ошибка: {e}", chat_id=message.chat.id, message_id=msg.message_id)

@bot.message_handler(content_types=['document'])
def handle_doc(message):
    if message.document.mime_type == 'application/pdf':
        msg = bot.reply_to(message, "⏳ Читаю PDF...")
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded = bot.download_file(file_info.file_path)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
                f.write(downloaded)
                path = f.name
            reader = PdfReader(path)
            text = "".join([p.extract_text() for p in reader.pages[:5]])
            os.remove(path)
            reply = ask_groq_with_history(message.chat.id, f"Сделай выжимку из PDF:\n\n{text[:3000]}")
            bot.edit_message_text(reply, chat_id=message.chat.id, message_id=msg.message_id)
        except Exception as e:
            bot.edit_message_text(f"Ошибка PDF: {e}", chat_id=message.chat.id, message_id=msg.message_id)
    else:
        bot.reply_to(message, "Отправь документ в формате .pdf!")

# ==========================================
# ЗАПУСК
# ==========================================
if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    bot.infinity_polling()
