import os
import time
import random
import asyncio
import threading

import requests
import edge_tts
import telebot

from flask import Flask
from openai import OpenAI
from bs4 import BeautifulSoup
from googlesearch import search

# ============================================================
# GEMINI — НОВЫЙ GOOGLE GENAI SDK
# ============================================================

try:
    from google import genai
    from google.genai import types
    GEMINI_SDK_AVAILABLE = True
except Exception as e:
    print(f"[Gemini] SDK import error: {e}")
    GEMINI_SDK_AVAILABLE = False


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

# OpenRouter:
# сначала ищем OPENROUTER_API_KEY,
# затем GROQ_KEY как запасной вариант.
OPENROUTER_KEY = (
    os.getenv("OPENROUTER_API_KEY")
    or os.getenv("GROQ_KEY")
    or os.getenv("GROQ_API_KEY")
)

GEMINI_KEY = os.getenv("GEMINI_API_KEY")

# Нужен только для Hugging Face /image
HF_TOKEN = os.getenv("HF_TOKEN")


# ============================================================
# ПРОВЕРКА BOT TOKEN
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден в Environment Variables Render."
    )


# ============================================================
# TELEGRAM BOT
# ============================================================

bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode=None
)


# ============================================================
# OPENROUTER CLIENT
# ============================================================

client = None

if OPENROUTER_KEY:
    try:
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_KEY,
        )
        print("[OpenRouter] Client создан.")
    except Exception as e:
        print(f"[OpenRouter] Ошибка создания клиента: {e}")
        client = None
else:
    print("[OpenRouter] OPENROUTER_API_KEY/GROQ_KEY не найден.")


# ============================================================
# GEMINI CLIENT
# ============================================================

gemini_client = None

if GEMINI_SDK_AVAILABLE and GEMINI_KEY:
    try:
        gemini_client = genai.Client(
            api_key=GEMINI_KEY
        )
        print("[Gemini] Client успешно создан.")
    except Exception as e:
        print(f"[Gemini] Ошибка создания Client: {e}")
        gemini_client = None
else:
    if not GEMINI_SDK_AVAILABLE:
        print("[Gemini] google-genai не установлен.")
    elif not GEMINI_KEY:
        print("[Gemini] GEMINI_API_KEY не найден.")


# ============================================================
# НАСТРОЙКИ
# ============================================================

SYSTEM_INSTRUCTION = (
    "Ты обычный парень-собеседник в Telegram. "
    "Общайся легко, весело и непринуждённо. "
    "Иногда можешь остроумно подшутить или сострить над вопросом. "
    "Не утверждай, что являешься человеком, если пользователь прямо спрашивает об этом. "
    "Отвечай на русском языке. "
    "Не используй Markdown-разметку: звездочки, решетки, подчёркивания и подобные символы."
)

MAX_HISTORY_LENGTH = 100

dialog_history = {}


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is active and running!"


def run_web():
    port = int(os.getenv("PORT", "8080"))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )


# ============================================================
# OPENROUTER AI
# ============================================================

def query_ai(messages, temperature=0.9):
    if not client:
        raise RuntimeError(
            "OpenRouter API ключ не задан."
        )

    model_name = "deepseek/deepseek-r1:free"

    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=temperature,
            extra_headers={
                "HTTP-Referer": "https://telegram.org",
                "X-Title": "TelegramBot",
            }
        )

        if not response.choices:
            raise RuntimeError(
                "OpenRouter не вернул ответ."
            )

        answer = response.choices[0].message.content

        if not answer:
            raise RuntimeError(
                "OpenRouter вернул пустой ответ."
            )

        # Удаляем возможный reasoning-блок DeepSeek
        if "</think>" in answer:
            answer = answer.split("</think>")[-1].strip()

        return answer.strip()

    except Exception as e:
        raise RuntimeError(
            f"Ошибка запроса к OpenRouter: {e}"
        )


# ============================================================
# GEMINI AI
# ============================================================

def query_gemini(prompt):
    if not GEMINI_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY не найден в Environment Variables Render."
        )

    if not GEMINI_SDK_AVAILABLE:
        raise RuntimeError(
            "Пакет google-genai не установлен."
        )

    if not gemini_client:
        raise RuntimeError(
            "Gemini Client не удалось создать."
        )

    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0.9,
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
        telebot.types.BotCommand("help", "Список всех команд"),
        telebot.types.BotCommand("image", "Сгенерировать картинку"),
        telebot.types.BotCommand("gemini", "Спросить Gemini"),
        telebot.types.BotCommand("search", "Поиск в интернете"),
        telebot.types.BotCommand("weather", "Узнать погоду"),
        telebot.types.BotCommand("fact", "Случайный факт"),
        telebot.types.BotCommand("code", "Написать или разобрать код"),
        telebot.types.BotCommand("sum", "Краткая выжимка"),
        telebot.types.BotCommand("tr", "Перевод"),
        telebot.types.BotCommand("fix", "Исправить ошибки"),
        telebot.types.BotCommand("tts", "Озвучить текст"),
        telebot.types.BotCommand("clear", "Сбросить контекст"),
    ])
except Exception as e:
    print(f"[Telegram] Не удалось установить команды: {e}")


# ============================================================
# /START /HELP
# ============================================================

@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    text = (
        "Привет! Твой ИИ-помощник.\n\n"
        "Команды:\n"
        "/weather [город] — погода\n"
        "/fact — случайный факт\n"
        "/image [описание] — генерация картинки\n"
        "/gemini [запрос] — Gemini\n"
        "/search [запрос] — поиск в интернете\n"
        "/tts [текст] — озвучка\n"
        "/code [текст] — работа с кодом\n"
        "/sum [текст] — краткая выжимка\n"
        "/tr [текст] — перевод\n"
        "/fix [текст] — исправление ошибок\n"
        "/clear — сбросить контекст"
    )

    bot.reply_to(message, text)


# ============================================================
# /CLEAR
# ============================================================

@bot.message_handler(commands=["clear"])
def clear_history(message):
    dialog_history[message.chat.id] = []

    bot.reply_to(
        message,
        "Память диалога полностью сброшена."
    )


# ============================================================
# /WEATHER
# ============================================================

@bot.message_handler(commands=["weather"])
def handle_weather(message):
    city = message.text.replace("/weather", "").strip()

    if not city:
        bot.reply_to(
            message,
            "Укажи город.\nПример: /weather Москва"
        )
        return

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

        if not geo_data.get("results"):
            bot.reply_to(
                message,
                "Город не найден."
            )
            return

        result = geo_data["results"][0]

        latitude = result["latitude"]
        longitude = result["longitude"]
        name = result["name"]

        weather_url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={latitude}"
            f"&longitude={longitude}"
            "&current=temperature_2m,wind_speed_10m"
        )

        weather_response = requests.get(
            weather_url,
            timeout=10
        )

        weather_data = weather_response.json()
        current = weather_data.get("current", {})

        temperature = current.get("temperature_2m")
        wind = current.get("wind_speed_10m")

        bot.reply_to(
            message,
            f"Погода в городе {name}:\n"
            f"Температура: {temperature} °C\n"
            f"Ветер: {wind} км/ч"
        )

    except Exception as e:
        bot.reply_to(
            message,
            f"Ошибка получения погоды: {e}"
        )


# ============================================================
# /FACT
# ============================================================

@bot.message_handler(commands=["fact"])
def handle_fact(message):
    facts = [
        "Мёд при правильном хранении может сохраняться очень долго.",
        "У осьминога три сердца.",
        "Банан с ботанической точки зрения является ягодой.",
        "Молния может неоднократно ударять в одно и то же место.",
        "У акул появились первые представители задолго до динозавров."
    ]

    bot.reply_to(
        message,
        random.choice(facts)
    )


# ============================================================
# /IMAGE
# ============================================================

@bot.message_handler(commands=["image"])
def handle_image_generation(message):
    prompt = message.text.replace("/image", "").strip()

    if not prompt:
        bot.reply_to(
            message,
            "Напиши, что нарисовать.\n"
            "Пример: /image космический кот"
        )
        return

    bot.send_chat_action(
        message.chat.id,
        "upload_photo"
    )

    try:
        english_prompt = prompt

        # Если OpenRouter доступен —
        # улучшаем prompt.
        if client:
            try:
                translation_messages = [
                    {
                        "role": "system",
                        "content": (
                            "Ты профессиональный prompt engineer. "
                            "Переведи запрос пользователя на английский "
                            "и сделай его подробным prompt для генерации "
                            "изображения FLUX. "
                            "Добавь описание композиции, света, деталей "
                            "и визуального стиля. "
                            "Верни только готовый prompt."
                        )
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]

                english_prompt = query_ai(
                    translation_messages,
                    temperature=0.7
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
                    "Authorization": f"Bearer {HF_TOKEN}"
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
                    "[HF] Не удалось получить изображение: "
                    f"{response.status_code}"
                )

            except Exception as e:
                print(
                    f"[HF] Ошибка: {e}"
                )

        # ====================================================
        # POLLINATIONS FALLBACK
        # ====================================================

        seed = random.randint(
            1,
            9999999
        )

        encoded_prompt = requests.utils.quote(
            english_prompt
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

@bot.message_handler(commands=["gemini"])
def handle_gemini(message):
    query = message.text.replace(
        "/gemini",
        "",
        1
    ).strip()

    if not query:
        bot.reply_to(
            message,
            "Напиши запрос.\n"
            "Пример: /gemini объясни теорию относительности"
        )
        return

    if not GEMINI_KEY:
        bot.reply_to(
            message,
            "Gemini недоступен.\n\n"
            "Причина: GEMINI_API_KEY не найден в Render."
        )
        return

    if not GEMINI_SDK_AVAILABLE:
        bot.reply_to(
            message,
            "Gemini недоступен.\n\n"
            "Причина: пакет google-genai не установлен."
        )
        return

    if not gemini_client:
        bot.reply_to(
            message,
            "Gemini недоступен.\n\n"
            "Причина: Gemini Client не удалось создать.\n"
            "Проверь GEMINI_API_KEY в Render."
        )
        return

    bot.send_chat_action(
        message.chat.id,
        "typing"
    )

    try:
        answer = query_gemini(
            query
        )

        bot.reply_to(
            message,
            answer
        )

    except Exception as e:
        print(
            f"[Gemini] {e}"
        )

        bot.reply_to(
            message,
            "Ошибка Gemini.\n\n"
            f"Причина: {e}"
        )


# ============================================================
# PHOTO → GEMINI VISION
# ============================================================

@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    if not GEMINI_KEY:
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

        downloaded_file = bot.download_file(
            file_info.file_path
        )

        user_caption = (
            message.caption
            or "Опиши это изображение подробно."
        )

        contents = [
            types.Part.from_bytes(
                data=downloaded_file,
                mime_type="image/jpeg"
            ),
            user_caption
        ]

        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0.7,
            )
        )

        answer = response.text

        if not answer:
            answer = "Gemini не смог дать описание изображения."

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
            f"Ошибка при обработке фото: {e}"
        )


# ============================================================
# /TTS
# ============================================================

@bot.message_handler(commands=["tts"])
def handle_tts(message):
    text = message.text.replace(
        "/tts",
        "",
        1
    ).strip()

    if not text:
        bot.reply_to(
            message,
            "Напиши текст для озвучки."
        )
        return

    bot.send_chat_action(
        message.chat.id,
        "record_voice"
    )

    filename = (
        f"voice_{message.chat.id}_"
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
            f"Ошибка аудио: {e}"
        )

    finally:
        try:
            if os.path.exists(filename):
                os.remove(filename)
        except Exception:
            pass


# ============================================================
# /SEARCH
# ============================================================

@bot.message_handler(commands=["search"])
def handle_search(message):
    query = message.text.replace(
        "/search",
        "",
        1
    ).strip()

    if not query:
        bot.reply_to(
            message,
            "Напиши запрос для поиска."
        )
        return

    if not client:
        bot.reply_to(
            message,
            "Поиск требует OpenRouter API ключ."
        )
        return

    bot.send_chat_action(
        message.chat.id,
        "typing"
    )

    try:
        urls = list(
            search(
                query,
                num_results=3
            )
        )

        if not urls:
            bot.reply_to(
                message,
                "Ничего не найдено."
            )
            return

        search_snippets = []

        headers = {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64)"
            )
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
                    ["script", "style", "noscript"]
                ):
                    tag.decompose()

                text = soup.get_text(
                    separator=" ",
                    strip=True
                )

                if text:
                    search_snippets.append(
                        f"Источник: {url}\n"
                        f"{text[:1200]}"
                    )

            except Exception as e:
                print(
                    f"[Search] {url}: {e}"
                )

        if not search_snippets:
            bot.reply_to(
                message,
                "Не удалось прочитать найденные сайты."
            )
            return

        search_text = "\n\n".join(
            search_snippets
        )

        prompt = (
            f"Запрос пользователя:\n{query}\n\n"
            f"Данные из найденных источников:\n"
            f"{search_text}\n\n"
            "На основе этих данных дай понятный "
            "и связный ответ на русском языке."
        )

        messages = [
            {
                "role": "system",
                "content": SYSTEM_INSTRUCTION
            },
            {
                "role": "user",
                "content": prompt
            }
        ]

        answer = query_ai(
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
            f"Ошибка поиска: {e}"
        )


# ============================================================
# /CODE /SUM /TR /FIX
# ============================================================

@bot.message_handler(
    commands=["code", "sum", "tr", "fix"]
)
def handle_special_commands(message):
    if not client:
        bot.reply_to(
            message,
            "OpenRouter API ключ не задан."
        )
        return

    command = (
        message.text
        .split()[0]
        .split("@")[0]
        .lower()
    )

    user_text = message.text[
        len(message.text.split()[0]):
    ].strip()

    if not user_text:
        bot.reply_to(
            message,
            f"Напиши текст после {command}"
        )
        return

    instructions = {
        "/code": (
            "Напиши, объясни или разбери код. "
            "Если пользователь просит исправить код, "
            "верни исправленный вариант."
        ),
        "/sum": (
            "Сделай краткую и понятную выжимку текста."
        ),
        "/tr": (
            "Переведи текст на русский язык. "
            "Если он уже на русском, объясни это."
        ),
        "/fix": (
            "Исправь ошибки в тексте, "
            "сохрани смысл и стиль."
        )
    }

    bot.send_chat_action(
        message.chat.id,
        "typing"
    )

    try:
        messages = [
            {
                "role": "system",
                "content": (
                    SYSTEM_INSTRUCTION
                    + "\n\nЗадача: "
                    + instructions.get(
                        command,
                        ""
                    )
                )
            },
            {
                "role": "user",
                "content": user_text
            }
        ]

        answer = query_ai(
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
# ОБЫЧНЫЙ ТЕКСТОВЫЙ ЧАТ
# ============================================================

@bot.message_handler(
    func=lambda message: True,
    content_types=["text"]
)
def handle_text_message(message):
    if not client:
        bot.reply_to(
            message,
            "OpenRouter API ключ не задан."
        )
        return

    bot.send_chat_action(
        message.chat.id,
        "typing"
    )

    chat_id = message.chat.id

    if chat_id not in dialog_history:
        dialog_history[chat_id] = []

    history = dialog_history[chat_id]

    user_text = message.text

    if (
        message.reply_to_message
        and message.reply_to_message.text
    ):
        user_text = (
            f"[Ответ на сообщение: "
            f"'{message.reply_to_message.text}']\n"
            f"{user_text}"
        )

    messages_payload = [
        {
            "role": "system",
            "content": SYSTEM_INSTRUCTION
        }
    ]

    messages_payload.extend(
        history
    )

    messages_payload.append(
        {
            "role": "user",
            "content": user_text
        }
    )

    try:
        answer = query_ai(
            messages_payload,
            temperature=0.9
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

        max_messages = MAX_HISTORY_LENGTH * 2

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
    print("=" * 50)
    print("Запуск Telegram-бота...")
    print("=" * 50)

    if GEMINI_KEY:
        print("[Gemini] GEMINI_API_KEY найден.")
    else:
        print("[Gemini] GEMINI_API_KEY НЕ найден.")

    if gemini_client:
        print("[Gemini] Client готов.")
    else:
        print("[Gemini] Client НЕ готов.")

    if OPENROUTER_KEY:
        print("[OpenRouter] API key найден.")
    else:
        print("[OpenRouter] API key НЕ найден.")

    # Удаляем webhook.
    # Это важно, если раньше использовался webhook.
    try:
        bot.remove_webhook()
        print("[Telegram] Webhook удалён.")
    except Exception as e:
        print(
            f"[Telegram] Ошибка удаления webhook: {e}"
        )

    time.sleep(2)

    while True:
        try:
            print("[Telegram] Запускаю polling...")

            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                skip_pending=True,
                allowed_updates=[
                    "message"
                ]
            )

            print(
                "[Telegram] Polling остановился. "
                "Перезапуск через 5 секунд..."
            )

            time.sleep(5)

        except Exception as e:
            error_text = str(e)

            print(
                f"[Telegram] Polling error: {error_text}"
            )

            # Telegram 409:
            # другой процесс уже использует getUpdates.
            if (
                "409" in error_text
                or "Conflict" in error_text
                or "terminated by other getUpdates" in error_text
            ):
                print(
                    "[Telegram] Обнаружен 409 Conflict."
                )

                print(
                    "[Telegram] Возможно, "
                    "запущен второй экземпляр бота."
                )

                print(
                    "[Telegram] Жду 15 секунд "
                    "и пробую снова..."
                )

                time.sleep(15)

            else:
                print(
                    "[Telegram] Неизвестная ошибка."
                )

                print(
                    "[Telegram] Жду 5 секунд..."
                )

                time.sleep(5)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print("Бот запускается...")

    # Flask запускаем отдельным daemon-потоком.
    web_thread = threading.Thread(
        target=run_web,
        daemon=True
    )

    web_thread.start()

    # Telegram polling запускаем в основном потоке.
    run_bot()
