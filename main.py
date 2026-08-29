import os
import time
import random
import asyncio
import threading

import requests
import edge_tts
import telebot

from flask import Flask
from groq import Groq
from bs4 import BeautifulSoup
from googlesearch import search


# ============================================================
# GEMINI — GOOGLE GENAI
# ============================================================

try:
    from google import genai
    from google.genai import types

    GEMINI_SDK_AVAILABLE = True

except Exception as e:
    print(f"[Gemini] Ошибка импорта google-genai: {e}")
    GEMINI_SDK_AVAILABLE = False


# ============================================================
# ENV
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

GROQ_API_KEY = (
    os.getenv("GROQ_API_KEY")
    or os.getenv("GROQ_KEY")
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

HF_TOKEN = os.getenv("HF_TOKEN")


# ============================================================
# MODELS
# ============================================================

GROQ_MODEL = "openai/gpt-oss-120b"

# Можно изменить через Environment Variables Render.
# Например:
# GEMINI_MODEL=gemini-2.5-flash
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.7-flash"
)


# ============================================================
# ПРОВЕРКА BOT TOKEN
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден в Environment Variables Render."
    )


# ============================================================
# TELEGRAM
# ============================================================

bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode=None
)


# ============================================================
# GROQ
# ============================================================

groq_client = None

if GROQ_API_KEY:

    try:

        groq_client = Groq(
            api_key=GROQ_API_KEY
        )

        print(
            "[Groq] Client успешно создан."
        )

    except Exception as e:

        print(
            f"[Groq] Ошибка создания клиента: {e}"
        )

else:

    print(
        "[Groq] GROQ_API_KEY не найден."
    )


# ============================================================
# GEMINI
# ============================================================

gemini_client = None

if GEMINI_SDK_AVAILABLE and GEMINI_API_KEY:

    try:

        gemini_client = genai.Client(
            api_key=GEMINI_API_KEY
        )

        print(
            "[Gemini] Client успешно создан."
        )

        print(
            f"[Gemini] Модель: {GEMINI_MODEL}"
        )

    except Exception as e:

        print(
            f"[Gemini] Ошибка создания Client: {e}"
        )

else:

    if not GEMINI_SDK_AVAILABLE:

        print(
            "[Gemini] google-genai не установлен."
        )

    if not GEMINI_API_KEY:

        print(
            "[Gemini] GEMINI_API_KEY не найден."
        )


# ============================================================
# SYSTEM PROMPT
# ============================================================

SYSTEM_INSTRUCTION = (
    "Ты обычный парень-собеседник в Telegram. "
    "Общайся легко, весело и непринуждённо. "
    "Иногда можешь остроумно пошутить. "
    "Отвечай преимущественно на русском языке. "
    "Не используй Markdown-разметку, если пользователь "
    "не попросил форматирование специально."
)


# ============================================================
# MEMORY
# ============================================================

dialog_history = {}

MAX_HISTORY_LENGTH = 100


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


@app.route("/")
def home():

    return "Bot is active and running!"


def run_web():

    port = int(
        os.getenv(
            "PORT",
            "8080"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )


# ============================================================
# GROQ GPT-OSS-120B
# ============================================================

def query_groq(
    messages,
    temperature=0.8
):

    if not groq_client:

        raise RuntimeError(
            "GROQ_API_KEY не найден или Groq Client "
            "не удалось создать."
        )

    try:

        response = groq_client.chat.completions.create(

            model=GROQ_MODEL,

            messages=messages,

            temperature=temperature,

            max_tokens=4096
        )

        if not response.choices:

            raise RuntimeError(
                "Groq не вернул ответ."
            )

        answer = (
            response
            .choices[0]
            .message
            .content
        )

        if not answer:

            raise RuntimeError(
                "Groq вернул пустой ответ."
            )

        return answer.strip()

    except Exception as e:

        raise RuntimeError(
            f"Ошибка Groq: {e}"
        )


# ============================================================
# GEMINI
# ============================================================

def query_gemini(
    prompt,
    temperature=0.8
):

    if not GEMINI_API_KEY:

        raise RuntimeError(
            "GEMINI_API_KEY не найден в Render."
        )

    if not GEMINI_SDK_AVAILABLE:

        raise RuntimeError(
            "Пакет google-genai не установлен."
        )

    if not gemini_client:

        raise RuntimeError(
            "Gemini Client не создан."
        )

    try:

        response = gemini_client.models.generate_content(

            model=GEMINI_MODEL,

            contents=prompt,

            config=types.GenerateContentConfig(

                system_instruction=SYSTEM_INSTRUCTION,

                temperature=temperature
            )
        )

        answer = response.text

        if not answer:

            raise RuntimeError(
                "Gemini вернул пустой ответ."
            )

        return answer.strip()

    except Exception as e:

        raise RuntimeError(
            f"Gemini API error: {e}"
        )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

try:

    bot.set_my_commands([

        telebot.types.BotCommand(
            "help",
            "Список всех команд"
        ),

        telebot.types.BotCommand(
            "image",
            "Создать изображение"
        ),

        telebot.types.BotCommand(
            "gemini",
            "Спросить Gemini"
        ),

        telebot.types.BotCommand(
            "search",
            "Поиск в интернете"
        ),

        telebot.types.BotCommand(
            "weather",
            "Погода"
        ),

        telebot.types.BotCommand(
            "fact",
            "Случайный факт"
        ),

        telebot.types.BotCommand(
            "code",
            "Работа с кодом"
        ),

        telebot.types.BotCommand(
            "sum",
            "Краткая выжимка"
        ),

        telebot.types.BotCommand(
            "tr",
            "Перевод"
        ),

        telebot.types.BotCommand(
            "fix",
            "Исправить текст"
        ),

        telebot.types.BotCommand(
            "tts",
            "Озвучить текст"
        ),

        telebot.types.BotCommand(
            "clear",
            "Очистить память"
        )
    ])

    print(
        "[Telegram] Команды установлены."
    )

except Exception as e:

    print(
        f"[Telegram] Ошибка установки команд: {e}"
    )


# ============================================================
# /HELP
# ============================================================

@bot.message_handler(
    commands=["start", "help"]
)
def send_welcome(message):

    text = (
        "Привет! Твой ИИ-помощник.\n\n"

        "Команды:\n"

        "/weather [город] — погода\n"

        "/fact — случайный факт\n"

        "/image [описание] — создать изображение\n"

        "/gemini [запрос] — спросить Gemini\n"

        "/search [запрос] — поиск в интернете\n"

        "/tts [текст] — озвучить текст\n"

        "/code [текст] — работа с кодом\n"

        "/sum [текст] — краткая выжимка\n"

        "/tr [текст] — перевод\n"

        "/fix [текст] — исправить текст\n"

        "/clear — очистить память"
    )

    bot.reply_to(
        message,
        text
    )


# ============================================================
# /CLEAR
# ============================================================

@bot.message_handler(
    commands=["clear"]
)
def clear_history(message):

    dialog_history[
        message.chat.id
    ] = []

    bot.reply_to(
        message,
        "Память диалога полностью сброшена."
    )


# ============================================================
# /WEATHER
# ============================================================

@bot.message_handler(
    commands=["weather"]
)
def handle_weather(message):

    city = message.text

    parts = city.split(
        maxsplit=1
    )

    if len(parts) < 2:

        bot.reply_to(
            message,
            "Укажи город.\n"
            "Пример: /weather Москва"
        )

        return

    city = parts[1].strip()

    try:

        geo_url = (
            "https://geocoding-api.open-meteo.com/v1/search"
            f"?name={requests.utils.quote(city)}"
            "&count=1"
            "&language=ru"
            "&format=json"
        )

        geo_response = requests.get(
            geo_url,
            timeout=10
        )

        geo_data = geo_response.json()

        results = geo_data.get(
            "results"
        )

        if not results:

            bot.reply_to(
                message,
                "Город не найден."
            )

            return

        result = results[0]

        latitude = result["latitude"]

        longitude = result["longitude"]

        name = result["name"]

        weather_url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={latitude}"
            f"&longitude={longitude}"
            "&current=temperature_2m,"
            "wind_speed_10m,"
            "relative_humidity_2m"
        )

        weather_response = requests.get(
            weather_url,
            timeout=10
        )

        weather_data = (
            weather_response.json()
        )

        current = weather_data.get(
            "current",
            {}
        )

        temperature = current.get(
            "temperature_2m"
        )

        wind = current.get(
            "wind_speed_10m"
        )

        humidity = current.get(
            "relative_humidity_2m"
        )

        text = (
            f"Погода в {name}:\n\n"
            f"Температура: {temperature} °C\n"
            f"Ветер: {wind} км/ч\n"
            f"Влажность: {humidity}%"
        )

        bot.reply_to(
            message,
            text
        )

    except Exception as e:

        bot.reply_to(
            message,
            f"Ошибка получения погоды: {e}"
        )


# ============================================================
# /FACT
# ============================================================

@bot.message_handler(
    commands=["fact"]
)
def handle_fact(message):

    facts = [

        "У осьминога три сердца.",

        "Банан с ботанической точки зрения является ягодой.",

        "Молния может несколько раз ударить "
        "в одно и то же место.",

        "Акулы появились раньше динозавров.",

        "У человека и жирафа одинаковое количество "
        "шейных позвонков — семь.",

        "Мёд при правильном хранении может "
        "сохраняться очень долго."
    ]

    bot.reply_to(
        message,
        random.choice(facts)
    )


# ============================================================
# /IMAGE
# ============================================================

@bot.message_handler(
    commands=["image"]
)
def handle_image(message):

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) < 2:

        bot.reply_to(
            message,
            "Напиши, что нарисовать.\n"
            "Пример: /image космический кот"
        )

        return

    prompt = parts[1].strip()

    bot.send_chat_action(
        message.chat.id,
        "upload_photo"
    )

    try:

        english_prompt = prompt

        # Улучшаем prompt через GPT-OSS.
        if groq_client:

            try:

                messages = [

                    {
                        "role": "system",
                        "content":
                            "Ты профессиональный prompt engineer. "
                            "Преврати запрос пользователя в подробный "
                            "английский prompt для генератора изображений. "
                            "Опиши композицию, освещение, стиль и детали. "
                            "Верни только готовый prompt."
                    },

                    {
                        "role": "user",
                        "content": prompt
                    }
                ]

                english_prompt = query_groq(
                    messages,
                    temperature=0.6
                )

            except Exception as e:

                print(
                    f"[Image] Ошибка улучшения prompt: {e}"
                )

        # ====================================================
        # HUGGING FACE
        # ====================================================

        if HF_TOKEN:

            try:

                hf_url = (
                    "https://api-inference.huggingface.co/models/"
                    "black-forest-labs/FLUX.1-schnell"
                )

                headers = {
                    "Authorization":
                        f"Bearer {HF_TOKEN}"
                }

                response = requests.post(

                    hf_url,

                    headers=headers,

                    json={
                        "inputs": english_prompt
                    },

                    timeout=60
                )

                if (
                    response.status_code == 200
                    and len(response.content) > 1000
                ):

                    bot.send_photo(

                        message.chat.id,

                        response.content,

                        caption=f"Запрос: {prompt}"
                    )

                    return

                print(
                    f"[HF] HTTP {response.status_code}"
                )

            except Exception as e:

                print(
                    f"[HF] Ошибка: {e}"
                )

        # ====================================================
        # POLLINATIONS
        # ====================================================

        seed = random.randint(
            1,
            9999999
        )

        encoded_prompt = (
            requests.utils.quote(
                english_prompt
            )
        )

        image_url = (
            "https://image.pollinations.ai/prompt/"
            f"{encoded_prompt}"
            f"?model=flux"
            f"&seed={seed}"
            f"&width=1024"
            f"&height=1024"
            f"&nologo=true"
        )

        bot.send_photo(
            message.chat.id,
            image_url,
            caption=f"Запрос: {prompt}"
        )

    except Exception as e:

        bot.reply_to(
            message,
            f"Ошибка генерации изображения: {e}"
        )


# ============================================================
# /GEMINI
# ============================================================

@bot.message_handler(
    commands=["gemini"]
)
def handle_gemini(message):

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) < 2:

        bot.reply_to(
            message,
            "Напиши запрос.\n"
            "Пример: /gemini объясни теорию относительности"
        )

        return

    query = parts[1].strip()

    if not GEMINI_API_KEY:

        bot.reply_to(
            message,
            "Gemini недоступен.\n\n"
            "GEMINI_API_KEY не найден в Render."
        )

        return

    if not GEMINI_SDK_AVAILABLE:

        bot.reply_to(
            message,
            "Gemini недоступен.\n\n"
            "google-genai не установлен."
        )

        return

    if not gemini_client:

        bot.reply_to(
            message,
            "Gemini недоступен.\n\n"
            "Gemini Client не удалось создать."
        )

        return

    bot.send_chat_action(
        message.chat.id,
        "typing"
    )

    try:

        answer = query_gemini(
            query,
            temperature=0.8
        )

        bot.reply_to(
            message,
            answer
        )

    except Exception as e:

        print(
            f"[Gemini] Ошибка: {e}"
        )

        bot.reply_to(
            message,
            f"Ошибка Gemini:\n{e}"
        )


# ============================================================
# PHOTO → GEMINI
# ============================================================

@bot.message_handler(
    content_types=["photo"]
)
def handle_photo(message):

    if not GEMINI_API_KEY:

        bot.reply_to(
            message,
            "Gemini недоступен: "
            "GEMINI_API_KEY не найден."
        )

        return

    if not GEMINI_SDK_AVAILABLE:

        bot.reply_to(
            message,
            "Gemini недоступен: "
            "google-genai не установлен."
        )

        return

    if not gemini_client:

        bot.reply_to(
            message,
            "Gemini Client не создан."
        )

        return

    bot.send_chat_action(
        message.chat.id,
        "typing"
    )

    try:

        file_info = bot.get_file(
            message.photo[-1].file_id
        )

        image_bytes = bot.download_file(
            file_info.file_path
        )

        caption = (
            message.caption
            or "Что изображено на фотографии?"
        )

        contents = [

            types.Part.from_bytes(
                data=image_bytes,
                mime_type="image/jpeg"
            ),

            caption
        ]

        response = (
            gemini_client
            .models
            .generate_content(

                model=GEMINI_MODEL,

                contents=contents,

                config=types.GenerateContentConfig(

                    system_instruction=
                        SYSTEM_INSTRUCTION,

                    temperature=0.7
                )
            )
        )

        answer = response.text

        if not answer:

            answer = (
                "Gemini не смог распознать изображение."
            )

        bot.reply_to(
            message,
            answer
        )

    except Exception as e:

        print(
            f"[Gemini Vision] {e}"
        )

        bot.reply_to(
            message,
            f"Ошибка обработки фотографии:\n{e}"
        )


# ============================================================
# /SEARCH
# ============================================================

@bot.message_handler(
    commands=["search"]
)
def handle_search(message):

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) < 2:

        bot.reply_to(
            message,
            "Напиши запрос.\n"
            "Пример: /search новости технологий"
        )

        return

    query = parts[1].strip()

    bot.send_chat_action(
        message.chat.id,
        "typing"
    )

    try:

        urls = list(
            search(
                query,
                num_results=5
            )
        )

        if not urls:

            bot.reply_to(
                message,
                "Ничего не найдено."
            )

            return

        snippets = []

        headers = {
            "User-Agent":
                "Mozilla/5.0"
        }

        for url in urls:

            try:

                response = requests.get(
                    url,
                    headers=headers,
                    timeout=7
                )

                if response.status_code != 200:
                    continue

                soup = BeautifulSoup(
                    response.text,
                    "html.parser"
                )

                for tag in soup(
                    [
                        "script",
                        "style",
                        "noscript"
                    ]
                ):
                    tag.decompose()

                text = soup.get_text(
                    separator=" ",
                    strip=True
                )

                if text:

                    snippets.append(
                        f"Источник: {url}\n"
                        f"{text[:1800]}"
                    )

            except Exception as e:

                print(
                    f"[Search] {url}: {e}"
                )

        if not snippets:

            bot.reply_to(
                message,
                "Не удалось прочитать найденные сайты."
            )

            return

        search_data = "\n\n".join(
            snippets
        )

        messages = [

            {
                "role": "system",
                "content":
                    SYSTEM_INSTRUCTION
                    + "\n"
                    + "Используй данные найденных "
                    + "источников для ответа."
            },

            {
                "role": "user",
                "content":
                    f"Запрос: {query}\n\n"
                    f"Данные из интернета:\n"
                    f"{search_data}\n\n"
                    "Дай понятный ответ."
            }
        ]

        answer = query_groq(
            messages,
            temperature=0.4
        )

        bot.reply_to(
            message,
            answer
        )

    except Exception as e:

        bot.reply_to(
            message,
            f"Ошибка поиска: {e}"
        )


# ============================================================
# /CODE /SUM /TR /FIX
# ============================================================

@bot.message_handler(
    commands=[
        "code",
        "sum",
        "tr",
        "fix"
    ]
)
def handle_special_commands(message):

    if not groq_client:

        bot.reply_to(
            message,
            "GROQ_API_KEY не найден."
        )

        return

    parts = message.text.split(
        maxsplit=1
    )

    command = (
        parts[0]
        .split("@")[0]
        .lower()
    )

    if len(parts) < 2:

        bot.reply_to(
            message,
            f"Напиши текст после {command}"
        )

        return

    user_text = parts[1].strip()

    instructions = {

        "/code":
            "Напиши или разбери код. "
            "Если код содержит ошибку — исправь её.",

        "/sum":
            "Сделай краткую и понятную выжимку.",

        "/tr":
            "Переведи текст на русский язык.",

        "/fix":
            "Исправь орфографические, "
            "грамматические и пунктуационные ошибки."
    }

    bot.send_chat_action(
        message.chat.id,
        "typing"
    )

    try:

        messages = [

            {
                "role": "system",
                "content":
                    SYSTEM_INSTRUCTION
                    + "\n\n"
                    + instructions.get(
                        command,
                        ""
                    )
            },

            {
                "role": "user",
                "content": user_text
            }
        ]

        answer = query_groq(
            messages,
            temperature=0.5
        )

        bot.reply_to(
            message,
            answer
        )

    except Exception as e:

        bot.reply_to(
            message,
            f"Ошибка: {e}"
        )


# ============================================================
# /TTS
# ============================================================

@bot.message_handler(
    commands=["tts"]
)
def handle_tts(message):

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) < 2:

        bot.reply_to(
            message,
            "Напиши текст для озвучки."
        )

        return

    text = parts[1].strip()

    bot.send_chat_action(
        message.chat.id,
        "record_voice"
    )

    filename = (
        f"voice_"
        f"{message.chat.id}_"
        f"{message.message_id}.mp3"
    )

    try:

        async def generate_voice():

            communicate = edge_tts.Communicate(
                text,
                "ru-RU-SvetlanaNeural"
            )

            await communicate.save(
                filename
            )

        asyncio.run(
            generate_voice()
        )

        with open(
            filename,
            "rb"
        ) as voice:

            bot.send_voice(
                message.chat.id,
                voice
            )

    except Exception as e:

        bot.reply_to(
            message,
            f"Ошибка озвучки: {e}"
        )

    finally:

        try:

            if os.path.exists(
                filename
            ):

                os.remove(
                    filename
                )

        except Exception:
            pass


# ============================================================
# ОБЫЧНЫЙ ЧАТ → GPT-OSS-120B
# ============================================================

@bot.message_handler(
    func=lambda message: True,
    content_types=["text"]
)
def handle_text_message(message):

    if not groq_client:

        bot.reply_to(
            message,
            "GROQ_API_KEY не найден."
        )

        return

    bot.send_chat_action(
        message.chat.id,
        "typing"
    )

    chat_id = message.chat.id

    if chat_id not in dialog_history:

        dialog_history[chat_id] = []

    history = dialog_history[
        chat_id
    ]

    user_text = message.text

    if (
        message.reply_to_message
        and message.reply_to_message.text
    ):

        user_text = (
            "[Ответ на сообщение: "
            f"{message.reply_to_message.text}]\n"
            f"{user_text}"
        )

    messages = [

        {
            "role": "system",
            "content": SYSTEM_INSTRUCTION
        }
    ]

    messages.extend(
        history
    )

    messages.append(
        {
            "role": "user",
            "content": user_text
        }
    )

    try:

        answer = query_groq(
            messages,
            temperature=0.8
        )

        history.append(
            {
                "role": "user",
                "content": user_text
            }
        )

        history.append(
            {
                "role": "assistant",
                "content": answer
            }
        )

        max_messages = (
            MAX_HISTORY_LENGTH * 2
        )

        if len(history) > max_messages:

            dialog_history[chat_id] = (
                history[-max_messages:]
            )

        bot.reply_to(
            message,
            answer
        )

    except Exception as e:

        bot.reply_to(
            message,
            f"Ошибка: {e}"
        )


# ============================================================
# TELEGRAM POLLING
# ============================================================

def run_bot():

    print("=" * 60)
    print("AI CHAT BOT")
    print("=" * 60)

    print(
        f"[Groq] Model: {GROQ_MODEL}"
    )

    if GROQ_API_KEY:

        print(
            "[Groq] API key найден."
        )

    else:

        print(
            "[Groq] API key НЕ найден."
        )

    if GEMINI_API_KEY:

        print(
            "[Gemini] API key найден."
        )

    else:

        print(
            "[Gemini] API key НЕ найден."
        )

    if gemini_client:

        print(
            "[Gemini] Client готов."
        )

    else:

        print(
            "[Gemini] Client НЕ готов."
        )

    # Убираем webhook
    try:

        bot.remove_webhook()

        print(
            "[Telegram] Webhook удалён."
        )

    except Exception as e:

        print(
            f"[Telegram] Ошибка webhook: {e}"
        )

    time.sleep(2)

    while True:

        try:

            print(
                "[Telegram] Запускаю polling..."
            )

            bot.polling(

                non_stop=False,

                timeout=30,

                long_polling_timeout=30,

                skip_pending=True,

                allowed_updates=[
                    "message"
                ]
            )

            print(
                "[Telegram] Polling остановлен."
            )

            time.sleep(5)

        except Exception as e:

            error = str(e)

            print(
                f"[Telegram] Polling error: {error}"
            )

            if (
                "409" in error
                or "Conflict" in error
                or "terminated by other getUpdates"
                in error
            ):

                print(
                    "[Telegram] 409 Conflict."
                )

                print(
                    "[Telegram] Жду 15 секунд..."
                )

                time.sleep(15)

            else:

                print(
                    "[Telegram] Перезапуск через 5 секунд..."
                )

                time.sleep(5)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    print(
        "Запуск бота..."
    )

    web_thread = threading.Thread(
        target=run_web,
        daemon=True
    )

    web_thread.start()

    run_bot()
