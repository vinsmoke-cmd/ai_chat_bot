import os
import telebot
import google.generativeai as genai

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_KEY")

# Настройка API ключа
genai.configure(api_key=GEMINI_KEY)

# Использование актуальной быстрой модели Gemini 1.5 Flash
model = genai.GenerativeModel("gemini-1.5-flash")

bot = telebot.TeleBot(BOT_TOKEN)

@bot.message_handler(func=lambda message: True)
def answer_ai(message):
    try:
        response = model.generate_content(message.text)
        if response and response.text:
            bot.reply_to(message, response.text)
        else:
            bot.reply_to(message, "⚠️ Не удалось сформировать ответ.")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка генерации: {str(e)}")

if __name__ == "__main__":
    bot.infinity_polling()
    
