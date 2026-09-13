import os
import re
import html
import telebot
from telebot import types
from g4f.client import Client
from groq import Groq

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("❌ Не задан BOT_TOKEN")

bot = telebot.TeleBot(BOT_TOKEN)

ai_client = Client()
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

user_histories = {}
user_modes = {}

TELEGRAM_MESSAGE_LIMIT = 4096
AI_MAX_RESPONSE_LENGTH = 40000


def clean_markdown(text):
    if not text:
        return ""

    text = str(text)
    code_blocks = []

    def protect_code(match):
        code_blocks.append(match.group(0))
        return f"§CODEBLOCK{len(code_blocks) - 1}§"

    text = re.sub(
        r"```(?:[a-zA-Z0-9_+#.-]+)?\s*\n?.*?```",
        protect_code,
        text,
        flags=re.DOTALL
    )

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
            parts.append({
                "type": "text",
                "content": before.strip()
            })

        parts.append({
            "type": "code",
            "language": (match.group(1) or "").strip(),
            "content": (match.group(2) or "").strip("\n")
        })

        last_end = match.end()

    after = text[last_end:]

    if after.strip():
        parts.append({
            "type": "text",
            "content": after.strip()
        })

    return parts


def send_code_block(chat_id, code, language=""):
    if not code:
        return None

    formatted = f"<pre><code>{html.escape(code, quote=False)}</code></pre>"

    try:
        return bot.send_message(
            chat_id,
            formatted,
            parse_mode="HTML"
        )
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
        text = (
            text[:AI_MAX_RESPONSE_LENGTH]
            + "\n\n[Ответ автоматически сокращён из-за максимального размера.]"
        )

    text = clean_markdown(text)
    parts = extract_code_blocks(text)

    sent_messages = []

    for part in parts:
        content = part.get("content", "")

        if not content:
            continue

        if part["type"] == "code":
            for code_part in split_long_message(
                content,
                max_length=3500
            ):
                send_code_block(
                    chat_id,
                    code_part,
                    part.get("language", "")
                )

        else:
            for text_part in split_long_message(content):
                try:
                    bot.send_message(chat_id, text_part)
                except Exception as e:
                    print(f"⚠️ Ошибка отправки текста: {e}")

    return sent_messages


def edit_or_send_long(chat_id, message_id, text):
    if not text:
        text = "Пустой ответ."

    text = str(text)

    if "```" not in text and len(text) <= TELEGRAM_MESSAGE_LIMIT - 100:
        try:
            bot.edit_message_text(
                clean_markdown(text),
                chat_id=chat_id,
                message_id=message_id
            )
            return
        except Exception as e:
            print(f"⚠️ Не удалось изменить сообщение: {e}")

    try:
        bot.delete_message(chat_id, message_id)
    except Exception as e:
        print(f"⚠️ Не удалось удалить временное сообщение: {e}")

    send_ai_response(chat_id, text)


def ask_ai_with_history(user_id, prompt):
    mode = user_modes.get(user_id, "normal")

    if user_id not in user_histories:

        if mode == "neuroham":
            sys_prompt = (
                "Ты — Нейрохам, саркастичный и высокомерный "
                "искусственный интеллект. "
                "Разговаривай с пользователем с позиции превосходства, "
                "используй едкую иронию. Без мата. "
                "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать Markdown "
                "(никаких звездочек, подчёркиваний, решеток). "
                "НЕ ДОБАВЛЯЙ в конец ответа никакой придуманный "
                "или шаблонный код Python."
            )

        else:
            sys_prompt = (
                "Ты полезный, дружелюбный и умный ИИ-ассистент. "
                "Отвечай строго на том же языке, на котором пишет "
                "пользователь. "
                "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать Markdown "
                "(никаких звездочек **, курсива _, заголовков #). "
                "Пиши только обычный чистый текст. "
                "Если нужен код — используй ровно три кавычки ``` "
                "без дополнительных знаков."
            )

        user_histories[user_id] = [
            {
                "role": "system",
                "content": sys_prompt
            }
        ]

    coding_keywords = [
        "код",
        "кодинг",
        "программ",
        "python",
        "javascript",
        "typescript",
        "java",
        "c++",
        "c#",
        "php",
        "html",
        "css",
        "sql",
        "bash",
        "telegram bot",
        "telegram бот",
        "бот",
        "api",
        "sdk",
        "функция",
        "класс",
        "метод",
        "библиотек",
        "скрипт",
        "исправь",
        "исправить",
        "ошибка",
        "ошибку",
        "перепиши",
        "переделай",
        "добавь функцию",
        "сделай код",
        "напиши код",
        "полный код",
        "готовый код",
        "source code",
        "debug"
    ]

    prompt_lower = str(prompt).lower()

    if any(kw in prompt_lower for kw in coding_keywords):
        effective_prompt = (
            "ИНСТРУКЦИИ ДЛЯ ПРОГРАММИРОВАНИЯ:\n"
            "Предоставь полный код. "
            "Пиши обычный текст без Markdown. "
            "Каждый фрагмент кода помещай только в три кавычки ```.\n\n"
            f"ЗАПРОС ПОЛЬЗОВАТЕЛЯ:\n{prompt}"
        )
    else:
        effective_prompt = str(prompt)

    user_histories[user_id].append({
        "role": "user",
        "content": effective_prompt
    })

    if len(user_histories[user_id]) > 21:
        user_histories[user_id] = (
            [user_histories[user_id][0]]
            + user_histories[user_id][-20:]
        )

    messages_to_send = [
        msg.copy()
        for msg in user_histories[user_id]
    ]

    if mode == "neuroham":
        messages_to_send[-1]["content"] = (
            "[Ответь в стиле саркастичного и ворчливого ИИ. "
            "Без мата. Строго чистый текст без Markdown. "
            "НЕ генерируй системный Python-код в конце.]\n\n"
            + messages_to_send[-1]["content"]
        )

    providers_models = [
        ("g4f", "gpt-4o-mini"),
        ("g4f", "gpt-3.5-turbo"),
        ("g4f", "gpt-4"),
        ("g4f", "llama-3-70b"),
        ("groq", "openai/gpt-oss-120b"),
        ("groq", "openai/gpt-oss-20b"),
        ("groq", "llama-3.3-70b-versatile"),
        ("groq", "llama-3.1-8b-instant"),
        ("groq", "qwen/qwen3-32b"),
        ("groq", "mixtral-8x7b-32768")
    ]

    answer = ""
    success = False

    for provider, model_name in providers_models:

        if provider == "g4f":
            try:
                print(f"🔄 G4F → {model_name}")

                response = ai_client.chat.completions.create(
                    model=model_name,
                    messages=messages_to_send,
                    timeout=7
                )

                if response and response.choices:
                    answer = response.choices[0].message.content

                    if answer and str(answer).strip():
                        answer = str(answer).strip()
                        success = True
                        print(
                            f"✅ G4F ({model_name}): "
                            "ответ успешно получен!"
                        )
                        break

            except Exception as e:
                print(
                    f"❌ G4F ({model_name}) ошибка / таймаут: {e}"
                )

        elif provider == "groq":

            if not groq_client:
                print(
                    "⚠️ Groq не инициализирован "
                    "(отсутствует GROQ_API_KEY). Пропускаем."
                )
                continue

            try:
                print(f"🔄 Groq → {model_name}")

                response = groq_client.chat.completions.create(
                    model=model_name,
                    messages=messages_to_send,
                    timeout=7
                )

                if response and response.choices:
                    answer = response.choices[0].message.content

                    if answer and str(answer).strip():
                        answer = str(answer).strip()
                        success = True
                        print(
                            f"✅ Groq ({model_name}): "
                            "ответ успешно получен!"
                        )
                        break

            except Exception as e:
                print(
                    f"❌ Groq ({model_name}) ошибка / таймаут: {e}"
                )

    if success:
        user_histories[user_id].append({
            "role": "assistant",
            "content": answer
        })
        return answer

    user_histories[user_id].pop()

    if mode == "neuroham":
        return "Даже мои процессоры решили сегодня саботировать работу."

    return (
        "Не удалось получить ответ ни от одной ИИ-модели. "
        "Попробуй ещё раз немного позже."
    )


@bot.message_handler(commands=["neuroham", "rude"])
def toggle_neuroham_mode(message):
    user_id = message.chat.id
    current_mode = user_modes.get(user_id, "normal")

    if current_mode == "normal":
        user_modes[user_id] = "neuroham"
        bot.reply_to(
            message,
            "Режим Нейрохам активирован"
        )
    else:
        user_modes[user_id] = "normal"
        bot.reply_to(
            message,
            "Режим Нейрохам деактивирован"
        )

    if user_id in user_histories:
        del user_histories[user_id]


@bot.message_handler(commands=["clear"])
def clear_cmd(message):
    user_id = message.chat.id

    if user_id in user_histories:
        del user_histories[user_id]

    bot.reply_to(
        message,
        "Память диалога очищена."
    )


@bot.message_handler(content_types=["text"])
def handle_text(message):
    text = message.text or ""

    if not text.strip():
        return

    msg = bot.reply_to(
        message,
        "Думаю..."
    )

    reply = ask_ai_with_history(
        message.chat.id,
        text
    )

    edit_or_send_long(
        message.chat.id,
        msg.message_id,
        reply
    )


if __name__ == "__main__":
    print("🤖 ИИ-бот запускается...")

    while True:
        try:
            bot.infinity_polling(
                skip_pending=True,
                timeout=30,
                long_polling_timeout=30
            )

        except Exception as e:
            print(f"⚠️ Ошибка polling: {e}")

            import time
            time.sleep(5)
