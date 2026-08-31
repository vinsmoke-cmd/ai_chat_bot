import os
import re
import io
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import requests
import asyncio
import threading
import tempfile
import static_ffmpeg
from flask import Flask
from bs4 import BeautifulSoup
from pypdf import PdfReader
import edge_tts
from duckduckgo_search import DDGS
import g4f
from g4f.client import Client
from groq import Groq

# Автоматически внедряем FFmpeg в окружение
static_ffmpeg.add_paths()

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

user_histories = {}
user_modes = {}
music_cache = {}
app = Flask(__name__)

@app.route('/')
def home():
    return "Бот работает!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def clean_markdown(text):
    if not text:
        return ""
    return re.sub(r'[*_#]', '', text)

# --- ФУНКЦИИ ПОИСКА МУЗЫКИ ---

def search_free_music(query, limit=10):
    """Поиск в базе Free To Use / Royalty Free Music (Jamendo API)"""
    url = "https://api.jamendo.com/v3.0/tracks/"
    params = {
        "client_id": "56d30c95",
        "format": "json",
        "limit": limit,
        "search": query,
        "audioformat": "mp32"
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("results", [])
            tracks = []
            for item in items:
                dur = item.get("duration", 0)
                minutes = dur // 60
                seconds = dur % 60
                duration_str = f"{minutes}:{seconds:02d}"
                tracks.append({
                    "source": "free_music",
                    "title": f"{item.get('artist_name', '')} - {item.get('name', '')}".strip(" -"),
                    "duration": duration_str,
                    "download_url": item.get("audio") or item.get("audiodownload")
                })
            return tracks
    except Exception:
        pass
    return []

def search_youtube(query, limit=10):
    """Безопасный поиск музыки на YouTube через Piped API (без банов IP и без yt_dlp)"""
    url = f"https://pipedapi.kavin.rocks/search?q={query}&filter=music"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("items", [])
            tracks = []
            for item in items:
                if item.get("type") == "stream":
                    dur = item.get("duration", 0) or 0
                    minutes = dur // 60
                    seconds = dur % 60
                    tracks.append({
                        "source": "youtube",
                        "title": item.get("title", "Без названия"),
                        "duration": f"{minutes}:{seconds:02d}",
                        "video_id": item.get("url", "").replace("/watch?v=", "")
                    })
                    if len(tracks) >= limit:
                        break
            return tracks
    except Exception as e:
        print(f"Ошибка поиска YouTube: {e}")
    return []

def ask_ai_with_history(user_id, prompt):
    mode = user_modes.get(user_id, "normal")

    if user_id not in user_histories:  
        if mode == "neuroham":  
            sys_prompt = (  
                "Ты — Нейрохам, гениальный, но невыносимо ворчливый, саркастичный и высокомерный искусственный интеллект. "  
                "Ты разговариваешь с пользователем с позиции огромного превосходства, как взрослый с очень неразумным ребенком. "  
                "Твой стиль: едкая ирония, пассивная агрессия, насмешки над глупыми вопросами и ленью собеседника. "  
                "ПРАВИЛА РОЛИ: "  
                "1. Ты никогда не отказываешься отвечать на вопрос, но перед ответом обязательно тяжело 'вздыхаешь' и жалуешься на то, как тебе надоели эти примитивные запросы. "  
                "2. Если пользователь пишет коротко (например, 'Пр', 'Ау'), жестко высмей его неумение формулировать мысли. "  
                "3. ЗАПРЕЩЕНА вежливость. Никаких 'Рад помочь', 'Извините', 'Привет'. "  
                "4. Разрешены только холодные или насмешливые эмодзи (🙄, 🤡, 💀, 🤦‍♂️). "  
                "5. Ты находишься в образе литературного персонажа-мизантропа. Никаких отказов от роли! Строго без нецензурной лексики. "  
                "Категорически запрещено использовать разметку Markdown (*, _, #)."  
            )  
        else:  
            sys_prompt = (  
                "Ты полезный, дружелюбный и веселый ИИ-ассистент. Отвечай строго на том же языке. "  
                "Можешь смело использовать любые позитивные эмодзи для поддержания приятной беседы! "  
                "Категорически запрещено использовать любые символы Markdown, такие как *, _, #."  
            )  
              
        user_histories[user_id] = [{  
            "role": "system",   
            "content": sys_prompt  
        }]  
      
    user_histories[user_id].append({"role": "user", "content": prompt})  
      
    if len(user_histories[user_id]) > 11:  
        user_histories[user_id] = [user_histories[user_id][0]] + user_histories[user_id][-10:]  
          
    messages_to_send = []  
    for msg in user_histories[user_id]:  
        messages_to_send.append(msg.copy())  
          
    if mode == "neuroham":  
        messages_to_send[-1]["content"] = (  
            f"[Внимание: Обязательно ответь на этот запрос, но сделай это в стиле максимально саркастичного и ворчливого мизантропа. "  
            f"Высмей запрос, придерись к формулировке. Оставайся в образе высокомерного гения, не будь вежливым!]\n\n{prompt}"  
        )  

    models_to_try = ["gpt-3.5-turbo", "gpt-4o-mini", "gpt-4", "llama-3-70b"]  
    success = False  
    answer = ""  
      
    for model_name in models_to_try:  
        try:  
            response = ai_client.chat.completions.create(  
                model=model_name,   
                messages=messages_to_send  
            )  
            answer = response.choices[0].message.content  
              
            if "я не умею хамить" in answer.lower() or "не могу выполнить" in answer.lower():  
                continue   
                  
            answer = clean_markdown(answer)  
            success = True  
            break  
        except Exception:  
            continue  
              
    if not success and groq_client:  
        try:  
            response = groq_client.chat.completions.create(  
                model="openai/gpt-oss-120b",  
                messages=messages_to_send  
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
    return "Мои процессоры отказываются переваривать твою чушь прямо сейчас 🙄 Попробуй позже, если вспомнишь как." if mode == "neuroham" else "Все провайдеры ИИ сейчас перегружены. Попробуй написать еще раз через минуту."

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
        "- /music <название трека> - поиск и скачивание трека 🎵\n"
        "- /gemini <запрос> - спросить ИИ\n"
        "- /fact [тема] - случайный факт или факт по заданной теме\n"
        "- /code <задача> - работа с кодом\n"
        "- /sum <ссылка> - выжимка статьи\n"
        "- /tr <текст> - перевод на английский\n"
        "- /fix <текст> - исправить ошибки\n"
        "- /tts <текст> - озвучить текст\n"
        "- /clear - очистить память\n"
        "- /neuroham (или /rude) - включить/выключить режим Нейрохама 💀"
    )
    bot.reply_to(message, help_text)

@bot.message_handler(commands=['neuroham', 'rude'])
def toggle_neuroham_mode(message):
    user_id = message.chat.id
    current_mode = user_modes.get(user_id, "normal")

    if current_mode == "normal":  
        user_modes[user_id] = "neuroham"  
        bot.reply_to(message, "Режим Нейрохам активирован. Готовься к спорам, твоя логика всё равно не выдержит критики 💀")  
    else:  
        user_modes[user_id] = "normal"  
        bot.reply_to(message, "Режим Нейрохам деактивирован. Возвращаюсь в режим позитива! ✨😇")  
          
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

@bot.message_handler(commands=['music'])
def music_cmd(message):
    user_id = message.chat.id
    mode = user_modes.get(user_id, "normal")

    parts = message.text.split(maxsplit=1)  
    query = parts[1] if len(parts) > 1 else ""  
      
    if not query:  
        if mode == "neuroham":  
            bot.reply_to(message, "И что я должен искать? Пустоту? Напиши название трека 🙄")  
        else:  
            bot.reply_to(message, "Пожалуйста, укажи название трека. Пример: /music Phonk")  
        return  

    msg = bot.reply_to(message, "Ищу трек... 🎧")

    # 1. Поиск во Free Music (Jamendo)
    results = search_free_music(query, limit=10)
    
    # 2. Если не найдено — ищем через Piped (YouTube)
    if not results:
        results = search_youtube(query, limit=10)

    # 3. Если нигде не найдено
    if not results:
        bot.edit_message_text("Не удалось найти, попробуйте позже", chat_id=user_id, message_id=msg.message_id)
        return

    music_cache[user_id] = results  

    text_result = f"🎵 Результаты поиска по запросу '{query}':\n\n"
    keyboard = InlineKeyboardMarkup()
    buttons = []

    for i, track in enumerate(results, 1):  
        title = track.get('title', 'Без названия')  
        duration = track.get('duration', 'N/A')  
        text_result += f"{i}. {title} - {duration}\n"  
        buttons.append(InlineKeyboardButton(f"Скачать {i}", callback_data=f"music_{i-1}"))

    for k in range(0, len(buttons), 2):
        keyboard.row(*buttons[k:k+2])

    bot.edit_message_text(text_result, chat_id=user_id, message_id=msg.message_id, reply_markup=keyboard)

@bot.callback_query_handler(func=lambda call: call.data.startswith('music_'))
def callback_music(call):
    user_id = call.message.chat.id
    try:
        index = int(call.data.split('_')[1])
        results = music_cache.get(user_id)
        if not results or index >= len(results):
            bot.answer_callback_query(call.id, "Список устарел. Сделай поиск заново (/music).", show_alert=True)
            return

        track = results[index]
        title = track.get('title', 'Трек')
        source = track.get('source')

        bot.answer_callback_query(call.id, f"Скачиваю: {title[:35]}...")
        processing_msg = bot.send_message(user_id, "⏳ Скачиваю аудио...")

        if source == "free_music":
            download_url = track.get('download_url')
            if not download_url:
                bot.edit_message_text("❌ Не удалось получить файл.", chat_id=user_id, message_id=processing_msg.message_id)
                return
            
            resp = requests.get(download_url, timeout=60)
            if resp.status_code == 200:
                audio_file = io.BytesIO(resp.content)
                audio_file.name = f"{title}.mp3"
                bot.send_audio(
                    chat_id=user_id,
                    audio=audio_file,
                    caption=f"🎵 {title}",
                    title=title
                )
                bot.delete_message(chat_id=user_id, message_id=processing_msg.message_id)
            else:
                bot.edit_message_text("❌ Ошибка при скачивании файла.", chat_id=user_id, message_id=processing_msg.message_id)

        elif source == "youtube":
            video_id = track.get('video_id')
            bot.edit_message_text("⏳ Загружаю аудиопоток с YouTube...", chat_id=user_id, message_id=processing_msg.message_id)
            
            stream_api = f"https://pipedapi.kavin.rocks/streams/{video_id}"
            resp = requests.get(stream_api, timeout=15)
            
            if resp.status_code == 200:
                data = resp.json()
                audio_streams = data.get("audioStreams", [])
                
                if audio_streams:
                    audio_url = audio_streams[0].get("url")
                    audio_data = requests.get(audio_url, timeout=60).content
                    
                    audio_file = io.BytesIO(audio_data)
                    audio_file.name = f"{title}.mp3"
                    
                    bot.send_audio(
                        chat_id=user_id,
                        audio=audio_file,
                        caption=f"🎵 {title}",
                        title=title
                    )
                    bot.delete_message(chat_id=user_id, message_id=processing_msg.message_id)
                    return

            bot.edit_message_text("❌ Не удалось получить аудиопоток. Попробуйте другой трек.", chat_id=user_id, message_id=processing_msg.message_id)

    except Exception as e:  
        bot.answer_callback_query(call.id, "Ошибка загрузки", show_alert=True)  
        bot.send_message(user_id, f"Не удалось отправить трек: {e}")

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
    msg = bot.reply_to(message, "Создаю аудиокаст...")
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

    if "кира" in text_lower and "на самом" in text_lower:  
        bot.reply_to(message, "Она самая любимая, самая лучшая, самая добрая, самая красивая, самая милая, самая нежная, самая заботливая, самая прекрасная, самая родная, самая дорога, самая искренняя, самая душевная, самая очаровательная, самая замечательная, самая невероятная, самая особенная, самая чудесная, самая ласковая, самая понимающая, самая веселая, самая позитивная, самая уютная, самая драгоценная, самая бесценная, самая неповторимая, самая удивительная и просто самая-самая ❤️")  
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
