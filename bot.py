import os
import hmac
import logging
from flask import Flask, request, jsonify
import requests
import urllib3

# Отключаем предупреждения об SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Токен бота и настройки из переменных окружения
TOKEN = os.environ.get("MAX_BOT_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
NOTIFY_CHAT_ID = os.environ.get("NOTIFY_CHAT_ID", "")
API_URL = "https://platform-api2.max.ru"

app = Flask(__name__)


def send_message(user_id=None, chat_id=None, text=""):
    """Отправка сообщения в чат MAX.

    Для личных сообщений (диалог) — user_id.
    Для групповых чатов и каналов — chat_id.
    Передаётся как query-параметр, не в теле!
    """
    params = {}
    if user_id:
        params["user_id"] = int(user_id)
    elif chat_id:
        params["chat_id"] = int(chat_id)
    else:
        logger.error("Не указан user_id или chat_id для отправки!")
        return

    try:
        response = requests.post(
            f"{API_URL}/messages",
            headers={
                "Authorization": TOKEN,
                "Content-Type": "application/json"
            },
            params=params,
            json={"text": text},
            timeout=10,
            verify=False
        )
        logger.info(f"Отправка: status={response.status_code}, body={response.text}")
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")


@app.route("/webhook", methods=["POST", "GET"])
def webhook():
    """Приём событий от MAX."""
    if request.method == "GET":
        return jsonify({"status": "ok"}), 200

    # Проверка секрета
    if WEBHOOK_SECRET:
        received_secret = request.headers.get("X-Max-Bot-Api-Secret", "")
        if not hmac.compare_digest(received_secret, WEBHOOK_SECRET):
            logger.warning("Неверный секрет webhook")
            return jsonify({"error": "forbidden"}), 403

    data = request.get_json()
    update_type = data.get("update_type", "")
    logger.info(f"Получено событие: {update_type}")

    # Обработка нового комментария
    if update_type == "comment_created":
        message = data.get("message", {})
        comment_text = message.get("body", {}).get("text", "")
        post_id = message.get("recipient", {}).get("post_id", "")
        author = message.get("from", {}).get("name", "Пользователь")

        notification = (
            f"Новый комментарий под постом {post_id}\n"
            f"Автор: {author}\n\n"
            f"{comment_text}"
        )
        logger.info(f"Уведомление: {notification}")
        send_message(user_id=NOTIFY_CHAT_ID, text=notification)

    # Обработка изменения комментария
    elif update_type == "comment_edited":
        message = data.get("message", {})
        comment_text = message.get("body", {}).get("text", "")
        post_id = message.get("recipient", {}).get("post_id", "")
        author = message.get("from", {}).get("name", "Пользователь")

        notification = (
            f"Изменён комментарий под постом {post_id}\n"
            f"Автор: {author}\n\n"
            f"{comment_text}"
        )
        send_message(user_id=NOTIFY_CHAT_ID, text=notification)

    # Обработка удаления комментария
    elif update_type == "comment_removed":
        message = data.get("message", {})
        post_id = message.get("recipient", {}).get("post_id", "")
        notification = f"Удалён комментарий под постом {post_id}"
        send_message(user_id=NOTIFY_CHAT_ID, text=notification)

    # Обработка обычных сообщений боту
    elif update_type == "message_created":
        message = data.get("message", {})
        sender_id = message.get("sender", {}).get("user_id", "")
        text = message.get("body", {}).get("text", "")

        logger.info(f"Сообщение от user_id={sender_id}: {text}")

        if text and text.lower().startswith("/start"):
            send_message(
                user_id=sender_id,
                text="Привет! Я бот для отслеживания комментариев. "
                     "Я буду присылать уведомления о новых комментариях "
                     "в ваш канал."
            )

    return jsonify({"ok": True}), 200


@app.route("/", methods=["GET"])
def index():
    return "Бот работает!", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
