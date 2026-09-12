import os
import re
import html
import threading
import time
import telebot
from flask import Flask
from g4f.client import Client
from groq import Groq

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("❌ Не задан BOT_TOKEN")

bot = telebot.TeleBot(BOT_TOKEN)
ai_client = Client()
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

try:
    BOT_USERNAME = bot.get_me().username.lower()
except Exception:
    BOT_USERNAME = ""

user_histories = {}
user_modes = {}

TELEGRAM_MESSAGE_LIMIT = 4096
AI_MAX_RESPONSE_LENGTH = 40000

app = Flask(__name__)

@app.route("/")
def home():
    return "Бот работает!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def clean_markdown(text):
    if not text:
        return ""
    text = str(text)
    code_blocks = []

    def protect_code(match):
        code_blocks.append(match.group(0))
        return f"§CODEBLOCK{len(code_blocks) - 1}§"

    text = re.sub(r"```(?:[a-zA-Z0-9_+#.-]+)?\s*\n?.*?```", protect_code, text, flags=re.DOTALL)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"\*{1,3}(.*?)\*{1,3}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"_{1,3}(.*?)_{1,3}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"~~(.*?)~~", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"(?m)^\s*>\s?", "", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = text.replace("*", "").replace("_", "").replace("`", "").replace("~", "")
    for idx, code_block in enumerate(code_blocks):
        text = text.replace(f"§CODEBLOCK{idx}§", code_block)
    return text.strip()

def split_long_message(text, max_length=TELEGRAM_MESSAGE_LIMIT - 100):
    if not text:
        return [""]
    text = str(text)
    if len(text) <= max_length:
        return [text]
    parts = []
    while len(text) > max_length:
        split_pos = text.rfind("\n", 0, max_length)
        if split_pos < max_length // 2:
            split_pos = text.rfind(" ", 0, max_length)
        if split_pos <= 0:
            split_pos = max_length
        part = text[:split_pos].strip()
        if part:
            parts.append(part)
        text = text[split_pos:].strip()
    if text:
        parts.append(text)
    return parts

def extract_code_blocks(text):
    if not text:
        return []
    pattern = r"```(?:([a-zA-Z0-9_+#.-]+))?\s*\n?(.*?)```"
    matches = list(re.finditer(pattern, str(text), flags=re.DOTALL))
    if not matches:
        return [{"type": "text", "content": text}]
    parts = []
    last_end = 0
    for match in matches:
        before = text[last_end:match.start()]
        if before.strip():
            parts.append({"type": "text", "content": before.strip()})
        parts.append({"type": "code", "language": (match.group(1) or "").strip(), "content": (match.group(2) or "").strip("\n")})
        last_end = match.end()
    after = text[last_end:]
    if after.strip():
        parts.append({"type": "text", "content": after.strip()})
    return parts

def send_code_block(chat_id, code, language=""):
    if not code:
        return None
    formatted = f"<pre><code>{html.escape(code, quote=False)}</code></pre>"
    try:
        return bot.send_message(chat_id, formatted, parse_mode="HTML")
    except Exception as e:
        print(f"⚠️ Ошибка отправки кодового блока: {e}")
        try:
            return bot.send_message(chat_id, code)
        except Exception as e2:
            print(f"❌ Ошибка fallback кодового блока: {e2}")
            return None

def send_ai_response(chat_id, text):
    if not text:
        return []
    text = str(text)
    if len(text) > AI_MAX_RESPONSE_LENGTH:
        text = text[:AI_MAX_RESPONSE_LENGTH] + "\n\n[Ответ автоматически сокращён из-за максимального размера.]"
    text = clean_markdown(text)
    parts = extract_code_blocks(text)
    sent_messages = []
    for part in parts:
        content = part.get("content", "")
        if not content:
            continue
        if part["type"] == "code":
            for code_part in split_long_message(content, max_length=3500):
                send_code_block(chat_id, code_part, part.get("language", ""))
        else:
            for text_part in split_long_message(content):
                try:
                    bot.send_message(chat_id, text_part)
                except Exception as e:
                    print(f"⚠️ Ошибка отправки текста: {e}")
    return sent_messages

def ask_ai_with_history(user_id, prompt):
    mode = user_modes.get(user_id, "normal")

    if user_id not in user_histories:
        if mode == "neuroham":
            sys_prompt = (
                "Ты — Нейрохам, саркастичный и высокомерный искусственный интеллект. "
                "Разговаривай с пользователем с позиции превосходства, используй едкую иронию. Без мата. "
                "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать Markdown (никаких звездочек, подчёркиваний, решеток). "
                "НЕ ДОБАВЛЯЙ в конец ответа никакой придуманный или шаблонный код Python."
            )
        else:
            sys_prompt = (
                "Ты полезный, дружелюбный и умный ИИ-ассистент. Отвечай строго на том же языке, на котором пишет "
                "пользователь. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать Markdown (никаких звездочек **, курсива _, заголовков #). "
                "Пиши только обычный чистый текст. Если нужен код — используй ровно три кавычки ``` без дополнительных знаков."
            )
        user_histories[user_id] = [{"role": "system", "content": sys_prompt}]

    coding_keywords = [
        "код", "кодинг", "программ", "python", "javascript", "typescript", "java",
        "c++", "c#", "php", "html", "css", "sql", "bash", "telegram bot",
        "telegram бот", "бот", "api", "sdk", "функция", "класс", "метод",
        "библиотек", "скрипт", "исправь", "исправить", "ошибка", "ошибку",
        "перепиши", "переделай", "добавь функцию", "сделай код", "напиши код",
        "полный код", "готовый код", "source code", "debug"
    ]

    prompt_lower = str(prompt).lower()
    if any(kw in prompt_lower for kw in coding_keywords):
        effective_prompt = (
            "ИНСТРУКЦИИ ДЛЯ ПРОГРАММИРОВАНИЯ:\n"
            "Предоставь полный код. Пиши обычный текст без Markdown. "
            "Каждый фрагмент кода помещай только в три кавычки ```.\n\n"
            f"ЗАПРОС ПОЛЬЗОВАТЕЛЯ:\n{prompt}"
        )
    else:
        effective_prompt = str(prompt)

    user_histories[user_id].append({"role": "user", "content": effective_prompt})
    if len(user_histories[user_id]) > 21:
        user_histories[user_id] = [user_histories[user_id][0]] + user_histories[user_id][-20:]

    messages_to_send = [msg.copy() for msg in user_histories[user_id]]

    if mode == "neuroham":
        messages_to_send[-1]["content"] = (
            "[Ответь в стиле саркастичного и ворчливого ИИ. "
            "Без мата. Строго чистый текст без Markdown. "
            "НЕ генерируй системный Python-код в конце.]\n\n"
            + messages_to_send[-1]["content"]
        )

    models_to_try = ["gpt-3.5-turbo", "gpt-4o-mini", "gpt-4", "llama-3-70b"]
    answer, success = "", False

    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n" f"🤖 Новый запрос от {user_id}")

    for model_name in models_to_try:
        try:
            print(f"🔄 G4F → {model_name}")
            response = ai_client.chat.completions.create(model=model_name, messages=messages_to_send)
            answer = response.choices[0].message.content
            if answer:
                answer = str(answer).strip()
                success = True
                print(f"✅ G4F → {model_name}: ответ получен")
                break
        except Exception as e:
            print(f"❌ G4F → {model_name}: {e}")

    if not success and groq_client:
        print("🔄 Переключаюсь на Groq GPT-OSS 120B...")
        try:
            response = groq_client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=messages_to_send
            )
            answer = response.choices[0].message.content
            if answer:
                answer = str(answer).strip()
                success = True
                print("✅ Groq GPT-OSS 120B: ответ успешно получен!")
        except Exception as e:
            print(f"❌ Groq ошибка: {e}")

    if success:
        user_histories[user_id].append({"role": "assistant", "content": answer})
        return answer

    user_histories[user_id].pop()
    return "Даже мои процессоры решили сегодня саботировать работу." if mode == "neuroham" else "Не удалось получить ответ от ИИ. Попробуй ещё раз немного позже."

def is_addressed_to_bot(message):
    if message.chat.type == "private":
        return True

    if message.reply_to_message and message.reply_to_message.from_user:
        if BOT_USERNAME and message.reply_to_message.from_user.username:
            if message.reply_to_message.from_user.username.lower() == BOT_USERNAME:
                return True

    text = (message.text or "").lower()

    if BOT_USERNAME and f"@{BOT_USERNAME}" in text:
        return True

    if text.startswith("бот ") or text.startswith("bot "):
        return True

    return False

def clean_bot_mentions(text):
    if BOT_USERNAME:
        text = re.sub(rf"@{BOT_USERNAME}", "", text, flags=re.IGNORECASE)
    return re.sub(r"^(бот|bot)[,\s]*", "", text, flags=re.IGNORECASE).strip()

@bot.message_handler(commands=["start", "help"])
def help_cmd(message):
    help_text = (
        "Привет! Я текстовый ИИ-бот 🤖\n\n"
        "Команды:\n"
        "/neuroham — переключить режим Нейрохама (включить/выключить)\n"
        "/clear — очистить историю диалога\n\n"
        "Просто напиши мне любой текстовый запрос!"
    )
    bot.send_message(message.chat.id, help_text)

@bot.message_handler(commands=["neuroham", "rude"])
def toggle_neuroham_mode(message):
    user_id = message.chat.id
    current_mode = user_modes.get(user_id, "normal")

    if current_mode == "normal":
        user_modes[user_id] = "neuroham"
        bot.reply_to(message, "Режим Нейрохам активирован 😼")
    else:
        user_modes[user_id] = "normal"
        bot.reply_to(message, "Режим Нейрохам деактивирован 🤖")

    if user_id in user_histories:
        del user_histories[user_id]

@bot.message_handler(commands=["clear"])
def clear_cmd(message):
    user_id = message.chat.id

    if user_id in user_histories:
        del user_histories[user_id]

    bot.reply_to(message, "Память диалога очищена.")

@bot.message_handler(content_types=["text"])
def handle_text(message):
    if not is_addressed_to_bot(message):
        return

    text = message.text or ""
    clean_text = clean_bot_mentions(text)

    if not clean_text:
        return

    # Отправляем ответ ИИ сразу напрямую без сообщения "Думаю..."
    reply = ask_ai_with_history(message.chat.id, clean_text)
    send_ai_response(message.chat.id, reply)

if __name__ == "__main__":
    print("=" * 60 + "\n🚀 Текстовый бот запускается..." + "\n" + "=" * 60)
    threading.Thread(target=run_web, daemon=True).start()
    print("🤖 Telegram polling запущен")

    while True:
        try:
            bot.infinity_polling(
                skip_pending=True,
                timeout=30,
                long_polling_timeout=30
            )
        except Exception as e:
            print(f"⚠️ Ошибка polling: {e}")
        time.sleep(5)
