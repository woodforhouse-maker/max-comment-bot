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

# Юзернейм твоего канала (для красивых ссылок)
CHANNEL_USERNAME = "channel_ignatyevy"

app = Flask(__name__)


def build_post_link(recipient):
    """
    Собирает человекочитаемую ссылку на пост через юзернейм канала.
    Формат: https://max.ru/@{CHANNEL_USERNAME}/post/{post_id}
    Если post_id нет — возвращает пустую строку.
    """
    post_id = recipient.get("post_id", "")
    if post_id:
        return f"https://max.ru/@{CHANNEL_USERNAME}/post/{post_id}"
    return ""


def get_author_name(message_data):
    """
    Пытается вытащить имя автора из данных вебхука.
    Приоритет: first_name -> name -> "Пользователь".
    """
    from_data = message_data.get("from", {})
    first_name = from_data.get("first_name")
    name = from_data.get("name")

    if first_name:
        return first_name
    if name:
        return name
    return "Пользователь"


def send_message(user_id=None, chat_id=None, text=""):
    """Отправка сообщения в чат MAX."""
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

    message = data.get("message", {})
    recipient = message.get("recipient", {})

    author_name = get_author_name(message)
    post_link = build_post_link(recipient)

    # Обработка нового комментария
    if update_type == "comment_created":
        comment_text = message.get("body", {}).get("text", "")

        notification = (
            f"🆕 Новый комментарий под постом\n"
            f"Автор: {author_name}\n\n"
            f"{comment_text}"
        )

        if post_link:
            notification += f"\n\n🔗 Ссылка на пост: {post_link}"

        logger.info(f"Уведомление: {notification}")
        send_message(user_id=NOTIFY_CHAT_ID, text=notification)

    # Обработка изменения комментария
    elif update_type == "comment_edited":
        comment_text = message.get("body", {}).get("text", "")

        notification = (
            f"✏️ Изменён комментарий под постом\n"
            f"Автор: {author_name}\n\n"
            f"{comment_text}"
        )

        if post_link:
            notification += f"\n\n🔗 Ссылка на пост: {post_link}"

        logger.info(f"Уведомление: {notification}")
        send_message(user_id=NOTIFY_CHAT_ID, text=notification)

    # Обработка удаления комментария
    elif update_type == "comment_removed":
        notification = (
            f"🗑️ Удалён комментарий под постом\n"
            f"Автор: {author_name}"
        )

        if post_link:
            notification += f"\n\n🔗 Ссылка на пост: {post_link}"

        logger.info(f"Уведомление: {notification}")
        send_message(user_id=NOTIFY_CHAT_ID, text=notification)

    # Обработка обычных сообщений боту
    elif update_type == "message_created":
        sender_id = message.get("sender", {}).get("user_id", "")
        text = message.get("body", {}).get("text", "")

        logger.info(f"Сообщение от user_id={sender_id}: {text}")

        if text and text.lower().startswith("/start"):
            send_message(
                user_id=sender_id,
                text="Привет! Я бот для отслеживания комментариев. "
                     "Я буду присылать уведомления о новых, изменённых и удалённых комментариях."
            )

    return jsonify({"ok": True}), 200


@app.route("/", methods=["GET"])
def index():
    return "Бот работает!", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
