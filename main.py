from g4f.client import Client
from groq import Groq

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

ai_client = Client()
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

user_histories = {}
user_modes = {}

def ask_ai_with_history(user_id, prompt):
    mode = user_modes.get(user_id, "normal")

    if user_id not in user_histories:
        if mode == "neuroham":
            sys_prompt = (
                "Ты — Нейрохам, саркастичный и высокомерный искусственный интеллект. "
                "Разговаривай с пользователем с позиции превосходства, используй едкую иронию. Без мата. "
                "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать Markdown. "
                "НЕ ДОБАВЛЯЙ в конец ответа никакой придуманный или шаблонный код Python."
            )
        else:
            sys_prompt = (
                "Ты полезный, дружелюбный и умный ИИ-ассистент. "
                "Отвечай строго на том же языке, на котором пишет пользователь. "
                "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать Markdown. "
                "Пиши только обычный чистый текст. "
                "Если нужен код — используй ровно три кавычки ```."
            )

        user_histories[user_id] = [
            {"role": "system", "content": sys_prompt}
        ]

    user_histories[user_id].append({
        "role": "user",
        "content": str(prompt)
    })

    if len(user_histories[user_id]) > 21:
        user_histories[user_id] = (
            [user_histories[user_id][0]]
            + user_histories[user_id][-20:]
        )

    messages_to_send = [
        msg.copy() for msg in user_histories[user_id]
    ]

    if mode == "neuroham":
        messages_to_send[-1]["content"] = (
            "[Ответь в стиле саркастичного и ворчливого ИИ. "
            "Без мата. Строго чистый текст без Markdown.]\n\n"
            + messages_to_send[-1]["content"]
        )

    models_to_try = [
        "gpt-3.5-turbo",
        "gpt-4o-mini",
        "gpt-4",
        "llama-3-70b"
    ]

    answer = ""
    success = False

    for model_name in models_to_try:
        try:
            response = ai_client.chat.completions.create(
                model=model_name,
                messages=messages_to_send
            )
            answer = response.choices[0].message.content

            if answer:
                answer = str(answer).strip()
                success = True
                break

        except Exception as e:
            print(f"❌ G4F → {model_name}: {e}")

    if not success and groq_client:
        try:
            response = groq_client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=messages_to_send
            )

            answer = response.choices[0].message.content

            if answer:
                answer = str(answer).strip()
                success = True

        except Exception as e:
            print(f"❌ Groq ошибка: {e}")

    if success:
        user_histories[user_id].append({
            "role": "assistant",
            "content": answer
        })
        return answer

    user_histories[user_id].pop()

    return (
        "Даже мои процессоры решили сегодня саботировать работу."
        if mode == "neuroham"
        else "Не удалось получить ответ от ИИ. Попробуй ещё раз немного позже."
    )
