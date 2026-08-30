import os
import re
import io
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import requests
import asyncio
import threading
import tempfile
import yt_dlp
import static_ffmpeg
from flask import Flask
from bs4 import BeautifulSoup
from pypdf import PdfReader
import edge_tts
from duckduckgo_search import DDGS
from g4f.client import Client
from groq import Groq

# Автоматически внедряем FFmpeg в окружение Render
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
    return "Бот работает 24/7 с FFmpeg!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def clean_markdown(text):
    if not text:
        return ""
    return re.sub(r'[*_#]', '', text)

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
                "5. Ты находишься в образе литературного персонажа-мизантропа. Строго без нецензурной лексики. "
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

@bot.message_handler(commands=['start', 'help'])
def help_cmd(message):
    help_text = (
        "Привет! Я ИИ-ассистент (работает 24/7 с поддержкой FFmpeg).\n\n"
        "Список команд:\n"
        "- /music <название трека> - поиск и скачивание MP3 из SoundCloud 🎵\n"
        "- /search <запрос> - поиск в интернете\n"
        "- /weather <город> - подробная погода\n"
        "- /image <описание> - создать картинку\n"
        "- /gemini <запрос> - спросить ИИ\n"
        "- /fact [тема] - случайный факт\n"
        "- /clear - очистить память\n"
        "- /neuroham - режим Нейрохама 💀"
    )
    bot.reply_to(message, help_text)

@bot.message_handler(commands=['neuroham', 'rude'])
def toggle_neuroham_mode(message):
    user_id = message.chat.id
    current_mode = user_modes.get(user_id, "normal")
    
    if current_mode == "normal":
        user_modes[user_id] = "neuroham"
        bot.reply_to(message, "Режим Нейрохам активирован. Готовься к спорам 💀")
    else:
        user_modes[user_id] = "normal"
        bot.reply_to(message, "Режим Нейрохам деактивирован. Возвращаюсь к позитиву! ✨")
        
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
    prompt = f"Расскажи очень интересный факт на тему: {topic}. Будь краток." if topic else "Расскажи один случайный интересный факт. Будь краток."
    msg = bot.reply_to(message, "Ищу факт...")
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
        resp = requests.get(f"https://wttr.in/{city}?format=3", timeout=5)
        if resp.status_code == 200:
            bot.reply_to(message, f"Погода: {resp.text.strip()}")
        else:
            bot.reply_to(message, "Не удалось найти город.")
    except Exception as e:
        bot.reply_to(message, f"Ошибка погоды: {e}")

@bot.message_handler(commands=['search'])
def search_cmd(message):
    parts = message.text.split(maxsplit=1)
    query = parts[1] if len(parts) > 1 else ""
    if not query:
        bot.reply_to(message, "Напиши запрос. Пример: /search новости")
        return
    msg = bot.reply_to(message, f"Ищу в интернете: {query}")
    data = perform_web_search(query)
    reply = ask_ai_with_history(message.chat.id, f"На основе данных ответь на запрос '{query}':\n\n{data[:1000]}")
    bot.edit_message_text(reply, chat_id=message.chat.id, message_id=msg.message_id)

@bot.message_handler(commands=['music'])
def music_cmd(message):
    user_id = message.chat.id
    parts = message.text.split(maxsplit=1)
    query = parts[1] if len(parts) > 1 else ""
    
    if not query:
        bot.reply_to(message, "Пожалуйста, укажи название трека. Пример: /music Phonk")
        return

    msg = bot.reply_to(message, "Ищу трек в SoundCloud... 🎧")

    try:
        ydl_opts = {
            'format': 'bestaudio/best',
            'noplaylist': True,
            'default_search': 'scsearch5',
            'quiet': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
            results = info.get('entries', [])
            
            if not results:
                bot.edit_message_text("К сожалению, в SoundCloud ничего не найдено 😔", chat_id=user_id, message_id=msg.message_id)
                return

            music_cache[user_id] = results

            text_result = f"🎵 Треки из SoundCloud по запросу '{query}'. Выбери цифру:\n\n"
            for i, track in enumerate(results, 1):
                title = track.get('title', 'Без названия')
                duration_sec = track.get('duration', 0)
                if duration_sec:
                    minutes = int(duration_sec // 60)
                    seconds = int(duration_sec % 60)
                    duration = f"{minutes}:{seconds:02d}"
                else:
                    duration = "0:00"
                text_result += f"{i}. {title} [{duration}]\n"
                
            keyboard = InlineKeyboardMarkup()
            buttons = [InlineKeyboardButton(str(i), callback_data=f"music_{i-1}") for i in range(1, len(results) + 1)]
            keyboard.row(*buttons)
                
            bot.edit_message_text(text_result, chat_id=user_id, message_id=msg.message_id, reply_markup=keyboard)
            
    except Exception as e:
        bot.edit_message_text(f"Ошибка поиска: {e}", chat_id=user_id, message_id=msg.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('music_'))
def callback_music(call):
    user_id = call.message.chat.id
    try:
        index = int(call.data.split('_')[1])
        results = music_cache.get(user_id)
        
        if not results:
            bot.answer_callback_query(call.id, "Список устарел.", show_alert=True)
            bot.edit_message_text("⚠️ Список сбросился. Сделайте поиск заново через `/music Название`", chat_id=user_id, message_id=call.message.message_id)
            return
            
        if index >= len(results):
            bot.answer_callback_query(call.id, "Список устарел.", show_alert=True)
            return
        
        track = results[index]
        url = track.get('webpage_url')
        title = track.get('title', 'Трек')
        
        bot.answer_callback_query(call.id, f"Скачиваю: {title[:35]}...")
        processing_msg = bot.send_message(user_id, "⏳ Скачиваю и конвертируем в MP3 через FFmpeg...")
        
        with tempfile.TemporaryDirectory() as temp_dir:
            ydl_opts = {
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'outtmpl': os.path.join(temp_dir, '%(id)s.%(ext)s'),
                'noplaylist': True,
                'quiet': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)
            
            files = os.listdir(temp_dir)
            if not files:
                bot.edit_message_text("❌ Не удалось найти скачанный файл во временной папке.", chat_id=user_id, message_id=processing_msg.message_id)
                return
            
            file_path = os.path.join(temp_dir, files[0])
            
            if os.path.getsize(file_path) > 50 * 1024 * 1024:
                bot.edit_message_text("❌ Трек слишком большой (больше 50 МБ).", chat_id=user_id, message_id=processing_msg.message_id)
                return

            bot.edit_message_text("📤 Отправляю аудио...", chat_id=user_id, message_id=processing_msg.message_id)
            
            with open(file_path, 'rb') as audio:
                bot.send_audio(
                    chat_id=user_id,
                    audio=audio,
                    caption=f"🎵 {title}",
                    title=title
                )
            bot.delete_message(chat_id=user_id, message_id=processing_msg.message_id)
            
    except Exception as e:
        print(f"Ошибка загрузки музыки: {e}")
        bot.answer_callback_query(call.id, "Ошибка загрузки", show_alert=True)
        bot.send_message(user_id, f"❌ Не удалось отправить трек. Ошибка: {e}")

@bot.message_handler(commands=['image'])
def image_cmd(message):
    parts = message.text.split(maxsplit=1)
    prompt = parts[1] if len(parts) > 1 else ""
    if not prompt:
        bot.reply_to(message, "Опиши картинку. Пример: /image кот")
        return
    msg = bot.reply_to(message, "Генерирую изображение...")
    img_bytes = generate_image_dynamic(prompt)
    if img_bytes:
        bot.send_photo(message.chat.id, img_bytes, caption=f"По запросу: {prompt}")
        bot.delete_message(message.chat.id, msg.message_id)
    else:
        bot.edit_message_text("Не удалось сгенерировать картинку.", chat_id=message.chat.id, message_id=msg.message_id)

@bot.message_handler(content_types=['text'])
def handle_text(message):
    text = message.text
    msg = bot.reply_to(message, "Думаю...")
    reply = ask_ai_with_history(message.chat.id, text)
    bot.edit_message_text(reply, chat_id=message.chat.id, message_id=msg.message_id)

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    bot.infinity_polling()
