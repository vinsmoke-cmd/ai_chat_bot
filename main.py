import os
import telebot
import google.generativeai as genai

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_KEY")

genai.configure(api_key=GEMINI_KEY)

# Список актуальных моделей Google Gemini
MODELS_TO_TRY = [
    "gemini-1.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-pro",
    "gemini-1.0-pro"
]

bot = telebot.TeleBot(BOT_TOKEN)

@bot.message_handler(func=lambda message: True)
def answer_ai(message):
    last_error = None
    
    # Перебираем доступные имена моделей, пока одно из них не ответит
    for model_name in MODELS_TO_TRY:
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(message.text)
            if response and response.text:
                bot.reply_to(message, response.text)
                return
        except Exception as e:
            last_error = e
            continue

    bot.reply_to(message, f"❌ Не удалось получить ответ от моделей Gemini: {str(last_error)}")

if __name__ == "__main__":
    bot.infinity_polling()
