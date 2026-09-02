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
import yt_dlp
import g4f
from g4f.client import Client
from groq import Groq

# Автоматически внедряем FFmpeg в окружение
static_ffmpeg.add_paths()

# Автоматически создаем cookies.txt из переменной окружения на хостинге
COOKIES_DATA = os.getenv("YOUTUBE_COOKIES")
if COOKIES_DATA:
    with open("cookies.txt", "w", encoding="utf-8") as f:
        f.write(COOKIES_DATA.strip())
    print("✅ Файл cookies.txt успешно создан из переменной окружения! Размер:", len(COOKIES_DATA))
else:
    print("❌ ВНИМАНИЕ: Переменная YOUTUBE_COOKIES не найдена или пуста!")

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

# --- ГИБРИДНЫЙ ПОИСК МУЗЫКИ ЧЕРЕЗ WEBSEARCH С ИСПРАВЛЕННЫМИ КЛЮЧАМИ ---

def search_youtube_with_cookies(query, limit=5):
    tracks = []
    print(f"🔍 Ищем через веб-поиск ссылку на YouTube для: {query}")
    
    search_query = f"{query} site:youtube.com/watch"
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(search_query, max_results=limit))
            for res in results:
                href = res.get('href') or res.get('link') or res.get('url', '')
                title = res.get('title', 'Без названия')
                
                match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11}).*', href)
                if match:
                    video_id = match.group(1)
                    url = f"https://www.youtube.com/watch?v={video_id}"
                    
                    if not any(t.get("video_id") == video_id for t in tracks):
                        tracks.append({
                            "title": title.replace(" - YouTube", ""),
                            "duration": "--:--",
                            "url": url,
                            "video_id": video_id
                        })
    except Exception as e:
        print(f"❌ Ошибка веб-поиска: {e}")

    print(f"✅ Найдено треков через обходной поиск: {len(tracks)}")
    return tracks

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
          
    messages_to_send = [msg.copy() for msg in user_histories[user_id]]  
          
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
            response = ai_client.chat.completions.create(model=model_name, messages=messages_to_send)  
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
            response = groq_client.chat.completions.create(model="openai/gpt-oss-120b", messages=messages_to_send)  
            answer = clean_markdown(response.choices[0].message.content)  
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
                    results_text += f"- {res.get('title', 'Без заголовка')}: {res.get('body', '')[:250]}...\n"  
        except Exception as e:  
            results_text = f"Не удалось выполнить поиск: {e}"  
    return results_text

def generate_image_dynamic(prompt):
    for model in ["flux", "dall-e-3"]:
        try:
            response = ai_client.images.generate(model=model, prompt=prompt, response_format="url")
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
        return "Анализ фото недоступен: не задан GEMINI_API_KEY."

    for model_name in ['gemini-2.5-flash', 'gemini-1.5-flash', 'gemini-2.0-flash']:  
        try:  
            model = genai.GenerativeModel(model_name)  
            image = Image.open(io.BytesIO(image_bytes))  
            response = model.generate_content(["Опиши подробно, что изображено на этой фотографии, и ответь на русском языке.", image])  
            if response and response.text:  
                return clean_markdown(response.text)  
        except Exception:  
            continue  
    return "Не удалось получить ответ от Gemini."

async def generate_audio(text, output_file):
    communicate = edge_tts.Communicate(text, "ru-RU-SvetlanaNeural")
    await communicate.save(output_file)

@bot.message_handler(commands=['start', 'help'])
def help_cmd(message):
    help_text = (
        "Привет! Я ИИ-ассистент.\n\n"
        "Список команд:\n"
        "- /search <запрос> - поиск в интернете\n"
        "- /weather <город> - подробная погода\n"
        "- /image <описание> - создать картинку\n"
        "- /music <название или строчка> - поиск и скачивание трека 🎵\n"
        "- /gemini <запрос> - спросить ИИ\n"
        "- /fact [тема] - случайный факт\n"
        "- /code <задача> - работа с кодом\n"
        "- /sum <ссылка> - выжимка статьи\n"
        "- /tr <текст> - перевод на английский\n"
        "- /fix <текст> - исправить ошибки\n"
        "- /tts <текст> - озвучить текст\n"
        "- /clear - очистить память\n"
        "- /neuroham (or /rude) - режим Нейрохама 💀"
    )
    bot.reply_to(message, help_text)

@bot.message_handler(commands=['neuroham', 'rude'])
def toggle_neuroham_mode(message):
    user_id = message.chat.id
    if user_modes.get(user_id, "normal") == "normal":  
        user_modes[user_id] = "neuroham"  
        bot.reply_to(message, "Режим Нейрохам активирован 💀")  
    else:  
        user_modes[user_id] = "normal"  
        bot.reply_to(message, "Режим Нейрохам деактивирован ✨")  
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
    prompt = f"Расскажи один интересный факт на тему: {topic}. Будь краток." if topic else "Расскажи один случайный интересный факт. Будь краток."
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
        resp = requests.get(
            f"https://wttr.in/{city}", 
            params={'format': 'Город: %l\nПогода: %C %c\nТемпература: %t (ощущается как %f)\nВетер: %w\nВлажность: %h', 'lang': 'ru', 'm': ''}, 
            timeout=5
        )
        if resp.status_code == 200:
            bot.reply_to(message, f"Сводка:\n\n{clean_markdown(resp.text.strip())}")
        else:
            bot.reply_to(message, "Город не найден.")
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}")

@bot.message_handler(commands=['search'])
def search_cmd(message):
    parts = message.text.split(maxsplit=1)
    query = parts[1] if len(parts) > 1 else ""
    if not query:
        bot.reply_to(message, "Напиши запрос. Пример: /search новости")
        return
    msg = bot.reply_to(message, f"Ищу: {query}")  
    raw_data = perform_web_search(query)  
    
    prompt = (
        f"Вот результаты поиска из интернета по запросу '{query}':\n{raw_data}\n\n"
        "Сделай краткую, понятную выжимку на русском языке строго по делу. "
        "Не пиши фразы вроде 'на основе предоставленных данных', 'по вашему запросу выявлено' и т.д. "
        "Просто ответь на вопрос или дай суть."
    )
    
    reply = ask_ai_with_history(message.chat.id, prompt)
    bot.edit_message_text(clean_markdown(reply), chat_id=message.chat.id, message_id=msg.message_id)

@bot.message_handler(commands=['music'])
def music_cmd(message):
    user_id = message.chat.id
    parts = message.text.split(maxsplit=1)  
    raw_query = parts[1] if len(parts) > 1 else ""  
    
    if not raw_query:  
        bot.reply_to(message, "Укажи название трека, строчку из него или транслит.\nПример: `/music айм блу да ба ди`", parse_mode="Markdown")  
        return  

    msg = bot.reply_to(message, "🧠 Анализирую текст и ищу трек... 🎧")

    ai_prompt = (
        f"Пользователь ищет песню по следующему запросу: '{raw_query}'. "
        f"Это может быть отрывок текста (строчка из песни), русский транслит или название. "
        f"Определи точное название песни и исполнителя. Выведи ТОЛЬКО в формате: 'Исполнитель - Название трека'. "
        f"Без кавычек, пояснений и лишних слов."
    )
    
    refined_query = raw_query
    try:
        response = ai_client.chat.completions.create(
            model="gpt-4o-mini", 
            messages=[{"role": "user", "content": ai_prompt}]
        )
        refined_query = clean_markdown(response.choices[0].message.content.strip())
    except Exception:
        pass

    results = search_youtube_with_cookies(refined_query, limit=5)
    
    if not results and refined_query != raw_query:
        results = search_youtube_with_cookies(raw_query, limit=5)

    if not results:
        bot.edit_message_text(f"❌ Не удалось найти трек по запросу: *{refined_query}*", chat_id=user_id, message_id=msg.message_id, parse_mode="Markdown")
        return

    music_cache[user_id] = results  
    text_result = f"🎵 Результаты по запросу: *{refined_query}*\n\n"
    
    keyboard = InlineKeyboardMarkup()
    buttons = []

    for i, track in enumerate(results, 1):  
        text_result += f"{i}. {track.get('title')}\n"  
        buttons.append(InlineKeyboardButton(f"Скачать {i}", callback_data=f"music_{i-1}"))

    for k in range(0, len(buttons), 2):
        keyboard.row(*buttons[k:k+2])

    bot.edit_message_text(text_result, chat_id=user_id, message_id=msg.message_id, reply_markup=keyboard, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('music_'))
def callback_music(call):
    user_id = call.message.chat.id
    try:
        index = int(call.data.split('_')[1])
        results = music_cache.get(user_id)
        if not results or index >= len(results):
            bot.answer_callback_query(call.id, "Список устарел, повторите поиск.", show_alert=True)
            return

        track = results[index]
        title = track.get('title', 'Трек')
        url = track.get('url')

        bot.answer_callback_query(call.id, f"Скачиваю: {title[:35]}...")
        processing_msg = bot.send_message(user_id, "⏳ Конвертирую аудиопоток...")

        audio_data = None
        
        with tempfile.TemporaryDirectory() as temp_dir:
            out_tmpl = os.path.join(temp_dir, 'track.%(ext)s')
            ydl_opts = {
                'format': 'ba/b',
                'outtmpl': out_tmpl,
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'quiet': True,
                'no_warnings': True,
                'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
            }
            
            if os.path.exists("cookies.txt"):
                ydl_opts['cookiefile'] = "cookies.txt"

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            
            for file in os.listdir(temp_dir):
                if file.endswith('.mp3'):
                    with open(os.path.join(temp_dir, file), 'rb') as f:
                        audio_data = f.read()
                    break

        if audio_data:
            audio_file = io.BytesIO(audio_data)
            audio_file.name = f"{title}.mp3"
            bot.send_audio(chat_id=user_id, audio=audio_file, caption=f"🎵 {title}", title=title)
            bot.delete_message(chat_id=user_id, message_id=processing_msg.message_id)
            return

        bot.edit_message_text("❌ Не удалось получить файл. Попробуйте другой вариант.", chat_id=user_id, message_id=processing_msg.message_id)

    except Exception as e:  
        bot.answer_callback_query(call.id, "Ошибка загрузки", show_alert=True)  
        bot.send_message(user_id, f"Не удалось отправить трек: {e}")

@bot.message_handler(commands=['gemini', 'code', 'sum', 'tr', 'fix'])
def ai_tools_cmd(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, f"Напиши текст после команды")  
        return  
    msg = bot.reply_to(message, "Обрабатываю...")  
    reply = ask_ai_with_history(message.chat.id, parts[1])  
    bot.edit_message_text(reply, chat_id=message.chat.id, message_id=msg.message_id)

@bot.message_handler(commands=['image'])
def image_cmd(message):
    parts = message.text.split(maxsplit=1)
    prompt = parts[1] if len(parts) > 1 else ""
    if not prompt:
        bot.reply_to(message, "Опиши картинку. Пример: /image кот")
        return
    msg = bot.reply_to(message, "Генерирую...")
    img_bytes = generate_image_dynamic(prompt)
    if img_bytes:
        bot.send_photo(message.chat.id, img_bytes, caption=f"Запрос: {prompt}")
        bot.delete_message(message.chat.id, msg.message_id)
    else:
        bot.edit_message_text("Не удалось сгенерировать.", chat_id=message.chat.id, message_id=msg.message_id)

@bot.message_handler(commands=['tts'])
def tts_cmd(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Напиши текст для озвучки.")
        return
    msg = bot.reply_to(message, "Озвучиваю...")
    audio_path = tempfile.mktemp(suffix=".mp3")
    asyncio.run(generate_audio(parts[1], audio_path))
    with open(audio_path, 'rb') as audio:
        bot.send_voice(message.chat.id, audio)
        bot.delete_message(message.chat.id, msg.message_id)
    os.remove(audio_path)

@bot.message_handler(content_types=['text'])
def handle_text(message):
    if "кира" in message.text.lower() and "на самом" in message.text.lower():  
        bot.reply_to(message, "Она самая любимая, самая лучшая и самая прекрасная ❤️")  
        return  
    if "http://" in message.text or "https://" in message.text:  
        msg = bot.reply_to(message, "Читаю ссылку...")  
        try:  
            url = [w for w in message.text.split() if w.startswith("http")][0]  
            resp = requests.get(url, timeout=10)  
            page_text = BeautifulSoup(resp.text, 'html.parser').get_text(separator=' ', strip=True)[:1500]  
            reply = ask_ai_with_history(message.chat.id, f"Сделай выжимку:\n\n{page_text}")  
            bot.edit_message_text(reply, chat_id=message.chat.id, message_id=msg.message_id)  
            return  
        except Exception as e:  
            bot.edit_message_text(f"Ошибка: {e}", chat_id=message.chat.id, message_id=msg.message_id)  
            return  

    msg = bot.reply_to(message, "Думаю...")  
    reply = ask_ai_with_history(message.chat.id, message.text)  
    bot.edit_message_text(reply, chat_id=message.chat.id, message_id=msg.message_id)

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    msg = bot.reply_to(message, "Изучаю фото...")
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        answer = analyze_image_gemini(bot.download_file(file_info.file_path))
        bot.edit_message_text(answer, chat_id=message.chat.id, message_id=msg.message_id)
    except Exception as e:
        bot.edit_message_text(f"Ошибка: {e}", chat_id=message.chat.id, message_id=msg.message_id)

@bot.message_handler(content_types=['document'])
def handle_doc(message):
    if message.document.mime_type == 'application/pdf':
        msg = bot.reply_to(message, "Читаю PDF...")
        try:
            file_info = bot.get_file(message.document.file_id)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
                f.write(bot.download_file(file_info.file_path))
                path = f.name
            text = "".join([p.extract_text() for p in PdfReader(path).pages[:3]])
            os.remove(path)
            reply = ask_ai_with_history(message.chat.id, f"Выжимка из PDF:\n\n{text[:1500]}")
            bot.edit_message_text(reply, chat_id=message.chat.id, message_id=msg.message_id)
        except Exception as e:
            bot.edit_message_text(f"Ошибка PDF: {e}", chat_id=message.chat.id, message_id=msg.message_id)
    else:
        bot.reply_to(message, "Отправьте документ в формате .pdf")

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    bot.infinity_polling()
