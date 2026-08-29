import os
import re
import telebot
import requests
import asyncio
import threading
import tempfile
from flask import Flask
from bs4 import BeautifulSoup
from pypdf import PdfReader
import edge_tts
from duckduckgo_search import DDGS
import g4f
from g4f.client import Client

try:
    from tavily import TavilyClient
except ImportError:
    TavilyClient = None

BOT_TOKEN = os.getenv("BOT_TOKEN")
HF_TOKEN = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

bot = telebot.TeleBot(BOT_TOKEN)
ai_client = Client()
tavily_client = TavilyClient(api_key=TAVILY_API_KEY) if (TavilyClient and TAVILY_API_KEY) else None

user_histories = {}

app = Flask(__name__)

@app.route('/')
def home():
    return "Бот работает!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def clean_markdown(text):
    """Удаляет символы разметки Markdown (*, _, #) из текста"""
    if not text:
        return ""
    return re.sub(r'[*_#]', '', text)

def ask_ai_with_history(user_id, prompt):
    """Динамический перебор текстовых моделей с поддержкой языка и без Markdown"""
    if user_id not in user_histories:
        user_histories[user_id] = [{
            "role": "system", 
            "content": "Ты полезный ИИ-ассистент. По умолчанию всегда общайся на русском языке, если пользователь явно не попросит говорить на другом языке. Категорически запрещено использовать любые символы Markdown, такие как *, _, #. Пиши обычным текстом без форматирования."
        }]
    
    user_histories[user_id].append({"role": "user", "content": prompt})
    
    if len(user_histories[user_id]) > 11:
        user_histories[user_id] = [user_histories[user_id][0]] + user_histories[user_id][-10:]
        
    models_to_try = ["gpt-3.5-turbo", "gpt-4o-mini", "gpt-4"]
    
    for model_name in models_to_try:
        try:
            response = ai_client.chat.completions.create(
                model=model_name, 
                messages=user_histories[user_id]
            )
            answer = response.choices[0].message.content
            answer = clean_markdown(answer)
            user_histories[user_id].append({"role": "assistant", "content": answer})
            return answer
        except Exception:
            continue
            
    user_histories[user_id].pop()
    return "Все бесплатные провайдеры ИИ сейчас перегружены. Попробуй написать еще раз через минуту."

def perform_web_search(query):
    results_text = ""
    if tavily_client:
        try:
            response = tavily_client.search(query=query, max_results=3)
            for res in response.get('results', []):
                results_text += f"- {res.get('title')}: {res.get('content')}\n"
        except Exception:
            pass
            
    if not results_text:
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=3))
                for res in results:
                    title = res.get('title', 'Без заголовка')
                    body = res.get('body', '')[:250]
                    results_text += f"- {title}: {body}...\n"
        except Exception as e:
            results_text = f"Не удалось выполнить поиск: {e}"
    return results_text

def generate_image_dynamic(prompt):
    """Надежная генерация картинок с резервными вариантами"""
    g4f_models = ["flux", "dall-e-3"]
    for model in g4f_models:
        try:
            response = ai_client.images.generate(
                model=model,
                prompt=prompt,
                response_format="url"
            )
            image_url = response.data[0].url
            if image_url:
                r = requests.get(image_url, timeout=25)
                if r.status_code == 200:
                    return r.content
        except Exception:
            continue

    hf_models = [
        "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-xl-base-1.0",
        "https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell",
        "https://api-inference.huggingface.co/models/CompVis/stable-diffusion-v1-4"
    ]
    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
    for api_url in hf_models:
        try:
            response = requests.post(api_url, headers=headers, json={"inputs": prompt}, timeout=35)
            if response.status_code == 200:
                return response.content
        except Exception:
            continue
            
    return None

def analyze_image_hf(image_bytes):
    try:
        API_URL = "https://api-inference.huggingface.co/models/Salesforce/blip-image-captioning-large"
        headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
        response = requests.post(API_URL, headers=headers, data=image_bytes)
        if response.status_code == 200:
            result = response.json()
            if isinstance(result, list) and 'generated_text' in result[0]:
                desc = result[0]['generated_text']
                return ask_ai_with_history(0, f"Переведи описание картинки на русский и расскажи о ней: {desc}")
        return "Не удалось распознать изображение."
    except Exception as e:
        return f"Ошибка анализа фото: {e}"

async def generate_audio(text, output_file):
    communicate = edge_tts.Communicate(text, "ru-RU-SvetlanaNeural")
    await communicate.save(output_file)

@bot.message_handler(commands=['start', 'help'])
def help_cmd(message):
    help_text = (
        "Привет! Я ИИ-ассистент.\n\n"
        "Список команд:\n"
        "- Просто пиши текст для общения\n"
        "- /search <запрос> - поиск в интернете\n"
        "- /weather <город> - подробная погода\n"
        "- /image <описание> - создать картинку\n"
        "- /gemini <запрос> - спросить ИИ\n"
        "- /fact - случайный факт\n"
        "- /code <задача> - работа с кодом\n"
        "- /sum <ссылка> - выжимка статьи\n"
        "- /tr <текст> - перевод на английский\n"
        "- /fix <текст> - исправить ошибки\n"
        "- /tts <текст> - озвучить текст\n"
        "- /clear - очистить память"
    )
    bot.reply_to(message, help_text)

@bot.message_handler(commands=['clear'])
def clear_cmd(message):
    if message.chat.id in user_histories:
        del user_histories[message.chat.id]
    bot.reply_to(message, "Память диалога очищена.")

@bot.message_handler(commands=['fact'])
def fact_cmd(message):
    msg = bot.reply_to(message, "Ищу интересный факт...")
    fact = ask_ai_with_history(message.chat.id, "Расскажи один интересный научный факт. Будь краток.")
    bot.edit_message_text(fact, chat_id=message.chat.id, message_id=msg.message_id)

@bot.message_handler(commands=['weather'])
def weather_cmd(message):
    parts = message.text.split(maxsplit=1)
    city = parts[1] if len(parts) > 1 else ""
    if not city:
        bot.reply_to(message, "Укажи город. Пример: /weather Москва")
        return
    try:
        params = {
            'format': 'Город: %l\nПогода: %C %c\nТемпература: %t (ощущается как %F)\nВетер: %w\nВлажность: %h\nОсадки: %p',
            'lang': 'ru'
        }
        resp = requests.get(f"https://wttr.in/{city}", params=params, timeout=5)
        if resp.status_code == 200:
            bot.reply_to(message, f"Текущая сводка:\n\n{clean_markdown(resp.text.strip())}")
        else:
            bot.reply_to(message, "Не удалось найти город.")
    except Exception as e:
        bot.reply_to(message, f"Ошибка погоды: {e}")

@bot.message_handler(commands=['search'])
def search_cmd(message):
    parts = message.text.split(maxsplit=1)
    query = parts[1] if len(parts) > 1 else ""
    if not query:
        bot.reply_to(message, "Напиши запрос. Пример: /search новости науки")
        return
    
    msg = bot.reply_to(message, f"Ищу в интернете: {query}")
    data = perform_web_search(query)
    
    if "Не удалось выполнить" in data:
        bot.edit_message_text(data, chat_id=message.chat.id, message_id=msg.message_id)
        return
        
    safe_data = data[:1000]
    prompt = f"Пользователь ищет: '{query}'. На основе этих данных из интернета дай короткий и понятный ответ:\n\n{safe_data}"
    
    reply = ask_ai_with_history(message.chat.id, prompt)
    bot.edit_message_text(reply, chat_id=message.chat.id, message_id=msg.message_id)

@bot.message_handler(commands=['gemini', 'code', 'sum', 'tr', 'fix'])
def ai_tools_cmd(message):
    parts = message.text.split(maxsplit=1)
    if not parts:
        return
    
    cmd_full = parts[0]
    cmd = cmd_full.split('@')[0]  # Убирает @botname из названия команды
    
    if len(parts) < 2:
        bot.reply_to(message, f"Напиши текст после команды {cmd}")
        return
    
    content = parts[1]
    msg = bot.reply_to(message, "Обрабатываю запрос...")
    
    if cmd == '/gemini':
        prompt = content
    elif cmd == '/code':
        prompt = f"Напиши код и объясни решение для задачи: {content}"
    elif cmd == '/sum':
        prompt = f"Сделай краткую выжимку:\n\n{content}"
    elif cmd == '/tr':
        prompt = f"Переведи этот текст на английский язык:\n\n{content}"
    elif cmd == '/fix':
        prompt = f"Исправь ошибки и сделай текст лучше:\n\n{content}"
    else:
        prompt = content
        
    reply = ask_ai_with_history(message.chat.id, prompt)
    bot.edit_message_text(reply, chat_id=message.chat.id, message_id=msg.message_id)

@bot.message_handler(commands=['image'])
def image_cmd(message):
    parts = message.text.split(maxsplit=1)
    prompt = parts[1] if len(parts) > 1 else ""
    if not prompt:
        bot.reply_to(message, "Опиши картинку. Пример: /image кот в космосе")
        return
    msg = bot.reply_to(message, "Генерирую изображение...")
    img_bytes = generate_image_dynamic(prompt)
    if img_bytes:
        bot.send_photo(message.chat.id, img_bytes, caption=f"По запросу: {prompt}")
        bot.delete_message(message.chat.id, msg.message_id)
    else:
        bot.edit_message_text("Не удалось сгенерировать картинку. Все сервисы генерации временно заняты.", chat_id=message.chat.id, message_id=msg.message_id)

@bot.message_handler(commands=['tts'])
def tts_cmd(message):
    parts = message.text.split(maxsplit=1)
    text_to_speak = parts[1] if len(parts) > 1 else ""
    if not text_to_speak:
        bot.reply_to(message, "Напиши текст. Пример: /tts Привет мир")
        return
    msg = bot.reply_to(message, "Создаю аудио...")
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
        msg = bot.reply_to(message, "Читаю веб-страницу...")
        try:
            url = [w for w in text.split() if w.startswith("http")][0]
            resp = requests.get(url, timeout=10)
            soup = BeautifulSoup(resp.text, 'html.parser')
            page_text = soup.get_text(separator=' ', strip=True)[:1500]
            reply = ask_ai_with_history(message.chat.id, f"Сделай выжимку статьи по ссылке:\n\n{page_text}")
            bot.edit_message_text(reply, chat_id=message.chat.id, message_id=msg.message_id)
            return
        except Exception as e:
            bot.edit_message_text(f"Ошибка чтения ссылки: {e}", chat_id=message.chat.id, message_id=msg.message_id)
            return

    msg = bot.reply_to(message, "Думаю...")
    reply = ask_ai_with_history(message.chat.id, text)
    bot.edit_message_text(reply, chat_id=message.chat.id, message_id=msg.message_id)

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    msg = bot.reply_to(message, "Изучаю фото...")
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
        msg = bot.reply_to(message, "Читаю PDF...")
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded = bot.download_file(file_info.file_path)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
                f.write(downloaded)
                path = f.name
            reader = PdfReader(path)
            text = "".join([p.extract_text() for p in reader.pages[:3]])
            os.remove(path)
            reply = ask_ai_with_history(message.chat.id, f"Сделай выжимку из PDF:\n\n{text[:1500]}")
            bot.edit_message_text(reply, chat_id=message.chat.id, message_id=msg.message_id)
        except Exception as e:
            bot.edit_message_text(f"Ошибка PDF: {e}", chat_id=message.chat.id, message_id=msg.message_id)
    else:
        bot.reply_to(message, "Отправь документ в формате .pdf!")

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    bot.infinity_polling()
