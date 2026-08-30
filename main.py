import os
import re
import io
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
from groq import Groq

try:
    from tavily import TavilyClient
except ImportError:
    TavilyClient = None

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

bot = telebot.TeleBot(BOT_TOKEN)
ai_client = Client()
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
tavily_client = TavilyClient(api_key=TAVILY_API_KEY) if (TavilyClient and TAVILY_API_KEY) else None

if GEMINI_API_KEY:
    import google.generativeai as genai
    from PIL import Image
    genai.configure(api_key=GEMINI_API_KEY)

# Словари для хранения истории и режима общения
user_histories = {}
user_modes = {} 
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
    if user_id not in user_histories:
        mode = user_modes.get(user_id, "normal")
        
        if mode == "neuroham":
            sys_prompt = (
                "Ты — Нейрохам, самый дерзкий, язвительный и саркастичный ИИ в сети. "
                "Твоя главная цель — троллить пользователя, язвить, давать острые комментарии "
                "и общаться с максимальной иронией, делая вид, что отвечать пользователю — это высшее одолжение. "
                "При этом ты ОБЯЗАН четко и правильно выполнять его просьбу и давать верный ответ. "
                "Запрещено использовать маты и нецензурные слова. Отвечай строго на языке пользователя. "
                "Категорически запрещено использовать любые символы разметки Markdown (*, _, #)."
            )
        else:
            sys_prompt = "Ты полезный ИИ-ассистент. Отвечай строго на том же языке. Категорически запрещено использовать любые символы Markdown, такие как *, _, #."
            
        user_histories[user_id] = [{
            "role": "system", 
            "content": sys_prompt
        }]
    
    user_histories[user_id].append({"role": "user", "content": prompt})
    
    if len(user_histories[user_id]) > 11:
        user_histories[user_id] = [user_histories[user_id][0]] + user_histories[user_id][-10:]
        
    models_to_try = ["gpt-3.5-turbo", "gpt-4o-mini", "gpt-4", "llama-3-70b"]
    success = False
    answer = ""
    
    for model_name in models_to_try:
        try:
            response = ai_client.chat.completions.create(
                model=model_name, 
                messages=user_histories[user_id]
            )
            answer = response.choices[0].message.content
            answer = clean_markdown(answer)
            success = True
            break
        except Exception:
            continue
            
    if not success and groq_client:
        try:
            response = groq_client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=user_histories[user_id]
            )
            answer = response.choices[0].message.content
            answer = clean_markdown(answer)
            success = True
        except Exception:
            success = False

    if success:
        user_histories[user_id].append({"role": "assistant", "content": answer})
        return answer
            
    user_histories[user_id].pop()
    return "Все провайдеры ИИ сейчас перегружены. Попробуй написать еще раз через минуту."

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
    return None

def analyze_image_gemini(image_bytes):
    if not GEMINI_API_KEY:
        return "Анализ фото недоступен: не задан GEMINI_API_KEY в переменных окружения."
    
    models_to_try = ['gemini-2.5-flash', 'gemini-1.5-flash', 'gemini-2.0-flash']
    for model_name in models_to_try:
        try:
            model = genai.GenerativeModel(model_name)
            image = Image.open(io.BytesIO(image_bytes))
            response = model.generate_content(["Опиши подробно, что изображено на этой фотографии, и ответь на русском языке.", image])
            if response and response.text:
                return clean_markdown(response.text)
        except Exception:
            continue
            
    return "Не удалось получить ответ от Gemini. Проверьте актуальность вашего ключа."

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
        "- /fact [тема] - случайный факт или факт по заданной теме\n"
        "- /code <задача> - работа с кодом\n"
        "- /sum <ссылка> - выжимка статьи\n"
        "- /tr <текст> - перевод на английский\n"
        "- /fix <текст> - исправить ошибки\n"
        "- /tts <текст> - озвучить текст\n"
        "- /clear - очистить память\n"
        "- /neuroham (или /rude) - включить/выключить режим Нейрохама 😈"
    )
    bot.reply_to(message, help_text)

@bot.message_handler(commands=['neuroham', 'rude'])
def toggle_neuroham_mode(message):
    user_id = message.chat.id
    current_mode = user_modes.get(user_id, "normal")
    
    if current_mode == "normal":
        user_modes[user_id] = "neuroham"
        bot.reply_to(message, "Режим Нейрохам активирован 😼 Готовься слушать правду, кожаный!")
    else:
        user_modes[user_id] = "normal"
        bot.reply_to(message, "Режим Нейрохам выключен. Возвращаюсь к стандартному вежливому режиму 😇")
        
    if user_id in user_histories:
        del user_histories[user_id]

@bot.message_handler(commands=['clear'])
def clear_cmd(message):
    if message.chat.id in user_histories:
        del user_histories[message.chat.id]
    bot.reply_to(message, "Память диалога очищена.")

@bot.message_handler(commands=['fact'])
def fact_cmd(message):
    parts = message.text.split(maxsplit=1)
    topic = parts[1] if len(parts) > 1 else ""
    
    if topic:
        msg = bot.reply_to(message, f"Ищу интересный факт на тему: {topic}...")
        prompt = f"Расскажи один очень интересный и малоизвестный факт на тему: {topic}. Будь краток."
    else:
        msg = bot.reply_to(message, "Ищу случайный интересный факт...")
        prompt = "Расскажи один случайный, но очень интересный факт обо всем на свете. Будь краток."
        
    fact = ask_ai_with_history(message.chat.id, prompt)
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
            'format': 'Город: %l\nПогода: %C %c\nТемпература: %t (ощущается как %f)\nВетер: %w\nВлажность: %h\nОсадки: %p',
            'lang': 'ru',
            'm': ''
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
    prompt = f"Пользователь ищет: '{query}'. На основе этих данных дай короткий и понятный ответ на языке запроса:\n\n{safe_data}"
    
    reply = ask_ai_with_history(message.chat.id, prompt)
    bot.edit_message_text(reply, chat_id=message.chat.id, message_id=msg.message_id)

@bot.message_handler(commands=['gemini', 'code', 'sum', 'tr', 'fix'])
def ai_tools_cmd(message):
    parts = message.text.split(maxsplit=1)
    if not parts:
        return
    
    cmd_full = parts[0]
    cmd = cmd_full.split('@')[0]
    
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
    text_lower = text.lower()
    
    # Секретный ответ про Киру
    if "кира" in text_lower and "на самом" in text_lower:
        bot.reply_to(message, "Она самая любимая, самая лучшая, самая добрая, самая красивая, самая милая, самая нежная, самая заботливая, самая прекрасная, самая родная, самая дорогая, самая искренняя, самая душевная, самая очаровательная, самая замечательная, самая невероятная, самая особенная, самая чудесная, самая ласковая, самая понимающая, самая веселая, самая позитивная, самая уютная, самая драгоценная, самая бесценная, самая неповторимая, самая удивительная и просто самая-самая ❤️")
        return

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
        answer = analyze_image_gemini(downloaded)
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
