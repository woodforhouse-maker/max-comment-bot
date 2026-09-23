import os
import hmac
import base64
import json
import logging
from flask import Flask, request, jsonify
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("MAX_BOT_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
NOTIFY_CHAT_ID = os.environ.get("NOTIFY_CHAT_ID", "")
API_URL = "https://platform-api2.max.ru"
CHANNEL_USERNAME = "channel_ignatyevy"

# Хранилище ожидающих ответов: {user_id: {"post_id": "...", "comment_mid": "..."}}
pending_replies = {}

# Мини-корзина: {user_id: [item_id, item_id, ...]}
user_carts = {}


# === Загрузка каталога ===
def load_catalog():
    """Загружает catalog.json в память бота."""
    file_path = os.path.join(os.path.dirname(__file__), "catalog.json")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info("Каталог успешно загружен.")
        return data
    except FileNotFoundError:
        logger.error("Файл catalog.json не найден!")
        return {"categories": []}
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка в catalog.json: {e}")
        return {"categories": []}


CATALOG_DATA = load_catalog()


# === Поиск товара по ID ===
def find_item_by_id(item_id):
    """Ищет товар в каталоге по id, возвращает словарь товара."""
    for category in CATALOG_DATA.get("categories", []):
        for item in category.get("items", []):
            if item["id"] == item_id:
                return item
    return None


app = Flask(__name__)


def get_post_seq(post_id):
    """Получает seq поста через API MAX для построения ссылки."""
    try:
        response = requests.get(
            f"{API_URL}/messages",
            params={"message_ids": post_id},
            headers={"Authorization": TOKEN},
            timeout=10,
            verify=False
        )
        if response.status_code == 200:
            data = response.json()
            messages = data.get("messages", [])
            if messages:
                seq = messages[0].get("body", {}).get("seq")
                if seq:
                    return int(seq)
    except Exception as e:
        logger.error(f"Ошибка получения seq поста: {e}")
    return None


def build_post_link(chat_id, post_id):
    """Строит рабочую ссылку на пост в MAX.
    Формат: https://max.ru/c/{chat_id}/{base64(seq)}
    """
    if not chat_id or not post_id:
        return f"https://max.ru/@{CHANNEL_USERNAME}"

    seq = get_post_seq(post_id)
    if seq:
        seq_bytes = seq.to_bytes(8, 'big')
        encoded = base64.urlsafe_b64encode(seq_bytes).decode().rstrip('=')
        return f"https://max.ru/c/{chat_id}/{encoded}"

    return f"https://max.ru/@{CHANNEL_USERNAME}"


def get_author_name(message_data):
    """Достаёт имя автора из данных вебхука."""
    from_data = message_data.get("from", {}) or message_data.get("sender", {})
    first_name = from_data.get("first_name")
    name = from_data.get("name")
    if first_name:
        return first_name
    if name:
        return name
    return "Пользователь"


def send_message(user_id=None, chat_id=None, text="", attachments=None, keyboard=None):
    """Отправка сообщения с опциональной inline-клавиатурой."""
    params = {}
    if user_id:
        params["user_id"] = int(user_id)
    elif chat_id:
        params["chat_id"] = int(chat_id)
    else:
        logger.error("Не указан user_id или chat_id!")
        return

    body = {"text": text}
    if attachments:
        body["attachments"] = attachments
    if keyboard:
        body["attachments"] = body.get("attachments", [])
        body["attachments"].append({
            "type": "inline_keyboard",
            "payload": {"buttons": keyboard}
        })

    try:
        response = requests.post(
            f"{API_URL}/messages",
            headers={"Authorization": TOKEN, "Content-Type": "application/json"},
            params=params,
            json=body,
            timeout=10,
            verify=False
        )
        logger.info(f"Отправка: status={response.status_code}, body={response.text}")
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")


def answer_callback(callback_id, notification=None):
    """Подтверждение нажатия callback-кнопки."""
    if not callback_id:
        logger.warning("answer_callback: callback_id пустой!")
        return
    try:
        body = {}
        if notification:
            body["notification"] = notification
        response = requests.post(
            f"{API_URL}/answers",
            headers={"Authorization": TOKEN, "Content-Type": "application/json"},
            params={"callback_id": callback_id},
            json=body,
            timeout=10,
            verify=False
        )
        logger.info(f"answer_callback: status={response.status_code}, body={response.text}")
    except Exception as e:
        logger.error(f"Ошибка answer_callback: {e}")


def post_comment(post_id, text, reply_to_mid=None):
    """Отправка комментария к посту. Если указан reply_to_mid — как ответ на комментарий."""
    body = {"text": text}
    if reply_to_mid:
        body["link"] = {"type": "reply", "mid": reply_to_mid}

    try:
        response = requests.post(
            f"{API_URL}/messages/{post_id}/comments",
            headers={"Authorization": TOKEN, "Content-Type": "application/json"},
            json=body,
            timeout=10,
            verify=False
        )
        logger.info(f"Комментарий: status={response.status_code}, body={response.text}")
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Ошибка отправки комментария: {e}")
        return False


def build_keyboard(post_link, post_id=None, comment_mid=None):
    """Собирает inline-клавиатуру с кнопками 'Открыть пост' и 'Ответить'."""
    buttons = []
    row = []

    if post_link:
        row.append({"type": "link", "text": "🔗 Открыть пост", "url": post_link})

    if post_id and comment_mid:
        row.append({
            "type": "callback",
            "text": "💬 Ответить",
            "payload": f"reply:{post_id}:{comment_mid}"
        })

    if row:
        buttons.append(row)

    if not buttons:
        return None

    return [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]


# === Отправка карточки товара ===
def send_product_card(user_id, item):
    """Отправляет карточку товара: фото, описание и кнопки."""
    text = (
        f"🪑 {item['name']}\n"
        f"💰 {item['price']} ₽\n"
        f"📝 {item['description']}"
    )

    attachments = []
    if item.get("photo_url"):
        attachments.append({
            "type": "photo",
            "url": item["photo_url"]
        })

    keyboard_buttons = [
        [
            {
                "type": "callback",
                "text": "🛒 Добавить в корзину",
                "payload": f"add_to_cart:{item['id']}"
            },
            {
                "type": "callback",
                "text": "💬 Заказать",
                "payload": f"quick_order:{item['id']}"
            }
        ]
    ]

    send_message(
        user_id=user_id,
        text=text,
        attachments=attachments,
        keyboard=keyboard_buttons
    )


@app.route("/webhook", methods=["POST", "GET"])
def webhook():
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

    # === Обработка нажатия callback-кнопки ===
    if update_type == "message_callback":
        callback = data.get("callback", {})
        callback_id = callback.get("callback_id", "")
        payload = callback.get("payload", "")
        sender_id = (
            callback.get("user", {}).get("user_id", "")
            or data.get("sender", {}).get("user_id", "")
            or data.get("user", {}).get("user_id", "")
        )

        logger.info(f"Callback от user_id={sender_id}, payload={payload}, callback_id={callback_id}")

        # --- Кнопка "Ответить" (старый функционал) ---
        if payload.startswith("reply:") and sender_id:
            parts = payload.split(":", 2)
            if len(parts) == 3:
                post_id = parts[1]
                comment_mid = parts[2]
                pending_replies[sender_id] = {
                    "post_id": post_id,
                    "comment_mid": comment_mid
                }
                answer_callback(callback_id, "✍️ Напишите ответ — бот отправит его как комментарий")
                send_message(
                    user_id=sender_id,
                    text="✍️ Напишите ответ следующим сообщением — бот отправит его как комментарий-ответ."
                )
            else:
                answer_callback(callback_id, "Ошибка: неверный формат")

        # --- Кнопка "Добавить в корзину" ---
        elif payload.startswith("add_to_cart:") and sender_id:
            item_id = payload.split(":", 1)[1]
            item = find_item_by_id(item_id)
            if not item:
                answer_callback(callback_id, "Товар не найден")
                return jsonify({"ok": True}), 200

            if sender_id not in user_carts:
                user_carts[sender_id] = []
            user_carts[sender_id].append(item_id)

            answer_callback(callback_id, "✅ Товар добавлен в корзину!")
            count = len(user_carts[sender_id])
            send_message(
                user_id=sender_id,
                text=(
                    f"🛒 «{item['name']}» добавлен в корзину.\n"
                    f"В корзине товаров: {count}\n\n"
                    f"Напишите /cart, чтобы посмотреть корзину."
                )
            )

        # --- Кнопка "Заказать" (быстрый заказ) ---
        elif payload.startswith("quick_order:") and sender_id:
            item_id = payload.split(":", 1)[1]
            item = find_item_by_id(item_id)
            if not item:
                answer_callback(callback_id, "Товар не найден")
                return jsonify({"ok": True}), 200

            answer_callback(callback_id, "Принято!")
            send_message(
                user_id=sender_id,
                text=(
                    f"📩 Быстрый заказ: {item['name']} ({item['price']} ₽)\n"
                    "Мастер скоро свяжется с вами для уточнения деталей."
                )
            )
            # Уведомление мастеру
            send_message(
                user_id=NOTIFY_CHAT_ID,
                text=(
                    f"📩 Новый быстрый заказ!\n"
                    f"Товар: {item['name']}\n"
                    f"Цена: {item['price']} ₽\n"
                    f"Пользователь: {sender_id}"
                )
            )

        else:
            answer_callback(callback_id, "Ок")

        return jsonify({"ok": True}), 200

    # === Обработка событий с комментариями ===
    message = data.get("message", {})
    recipient = message.get("recipient", {})
    chat_id = recipient.get("chat_id", "")
    post_id = recipient.get("post_id", "")
    comment_mid = message.get("body", {}).get("mid", "")
    author_name = get_author_name(message)

    # Новый комментарий
    if update_type == "comment_created":
        comment_text = message.get("body", {}).get("text", "")

        notification = (
            f"🆕 Новый комментарий\n"
            f"Автор: {author_name}\n\n"
            f"{comment_text}"
        )

        post_link = build_post_link(chat_id, post_id)
        keyboard = build_keyboard(post_link, post_id, comment_mid)

        logger.info(f"Уведомление: {notification}")
        send_message(user_id=NOTIFY_CHAT_ID, text=notification, attachments=keyboard)

    # Изменённый комментарий
    elif update_type == "comment_edited":
        comment_text = message.get("body", {}).get("text", "")

        notification = (
            f"✏️ Изменён комментарий\n"
            f"Автор: {author_name}\n\n"
            f"{comment_text}"
        )

        post_link = build_post_link(chat_id, post_id)
        keyboard = build_keyboard(post_link, post_id, comment_mid)

        send_message(user_id=NOTIFY_CHAT_ID, text=notification, attachments=keyboard)

    # Удалённый комментарий
    elif update_type == "comment_removed":
        notification = (
            f"🗑️ Удалён комментарий\n"
            f"Автор: {author_name}"
        )

        post_link = build_post_link(chat_id, post_id)
        keyboard = build_keyboard(post_link)

        send_message(user_id=NOTIFY_CHAT_ID, text=notification, attachments=keyboard)

    # === Обработка обычных сообщений боту ===
    elif update_type == "message_created":
        sender_id = message.get("sender", {}).get("user_id", "")
        text = message.get("body", {}).get("text", "")

        logger.info(f"Сообщение от user_id={sender_id}: {text}")

        # Команда /catalog — показать каталог
        if text and text.lower().strip() == "/catalog":
            categories = CATALOG_DATA.get("categories", [])
            if not categories:
                send_message(user_id=sender_id, text="Каталог пока пуст.")
                return jsonify({"ok": True}), 200

            # Показываем все категории как кнопки
            buttons = []
            row = []
            for i, cat in enumerate(categories):
                row.append({
                    "type": "callback",
                    "text": f"📦 {cat['name']}",
                    "payload": f"show_category:{i}"
                })
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)

            send_message(
                user_id=sender_id,
                text="🛠 Каталог мастерской Игнатьевых\nВыберите категорию:",
                keyboard=buttons
            )
            return jsonify({"ok": True}), 200

        # Команда /cart — показать корзину
        if text and text.lower().strip() == "/cart":
            cart = user_carts.get(sender_id, [])
            if not cart:
                send_message(user_id=sender_id, text="🛒 Ваша корзина пуста.")
                return jsonify({"ok": True}), 200

            cart_text = "🛒 Ваша корзина:\n\n"
            total = 0
            for item_id in cart:
                item = find_item_by_id(item_id)
                if item:
                    cart_text += f"• {item['name']} — {item['price']} ₽\n"
                    total += item["price"]
            cart_text += f"\n💰 Итого: {total} ₽\n\n"

            keyboard_buttons = [
                [
                    {
                        "type": "callback",
                        "text": "✅ Оформить заказ",
                        "payload": "checkout"
                    },
                    {
                        "type": "callback",
                        "text": "🗑 Очистить корзину",
                        "payload": "clear_cart"
                    }
                ]
            ]

            send_message(user_id=sender_id, text=cart_text, keyboard=keyboard_buttons)
            return jsonify({"ok": True}), 200

        # Проверяем, есть ли ожидающий ответ от этого пользователя
        if sender_id in pending_replies and text and not text.startswith("/"):
            reply_data = pending_replies.pop(sender_id)
            post_id = reply_data["post_id"]
            comment_mid = reply_data["comment_mid"]

            success = post_comment(post_id, text, reply_to_mid=comment_mid)

            if success:
                send_message(user_id=sender_id, text="✅ Ответ отправлен в канал!")
            else:
                send_message(user_id=sender_id, text="❌ Не удалось отправить ответ. Проверьте, что бот — администратор канала с правом write.")
                # Возвращаем в очередь на случай повторной попытки
                pending_replies[sender_id] = reply_data

        elif text and text.lower().startswith("/start"):
            send_message(
                user_id=sender_id,
                text=(
                    "Привет! Я бот мастерской Игнатьевых. 🪵\n\n"
                    "🛍 Напишите /catalog — чтобы открыть каталог изделий.\n"
                    "🛒 Напишите /cart — чтобы посмотреть корзину.\n\n"
                    "Также я слежу за комментариями в канале и помогаю отвечать на них."
                )
            )

    return jsonify({"ok": True}), 200


@app.route("/", methods=["GET"])
def index():
    return "Бот работает!", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
