import os
import hmac
import base64
import json
import re
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
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
API_URL = "https://platform-api2.max.ru"
CHANNEL_USERNAME = "channel_ignatyevy"

pending_replies = {}
user_carts = {}

# === СИСТЕМА ДИАЛОГОВ ===
question_counter = 0
active_dialogs = {}  # {номер: {"user_id": ..., "name": ..., "text": ..., "item_name": ...}}


def load_catalog():
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


def find_item_by_id(item_id):
    for category in CATALOG_DATA.get("categories", []):
        for item in category.get("items", []):
            if item["id"] == item_id:
                return item
    return None


app = Flask(__name__)


def register_commands():
    commands = [
        {"name": "start", "description": "Начать работу с ботом"},
        {"name": "каталог", "description": "Открыть каталог изделий"},
        {"name": "корзина", "description": "Посмотреть корзину"},
        {"name": "помощь", "description": "Как пользоваться ботом"},
    ]
    try:
        response = requests.patch(
            f"{API_URL}/me/commands",
            headers={"Authorization": TOKEN, "Content-Type": "application/json"},
            json={"commands": commands},
            timeout=10,
            verify=False
        )
        logger.info(f"Регистрация команд: status={response.status_code}, body={response.text}")
    except Exception as e:
        logger.error(f"Ошибка регистрации команд: {e}")


def update_webhook_subscription():
    """Обновляет подписку вебхука, добавляя bot_started в список событий."""
    if not WEBHOOK_URL:
        logger.warning("WEBHOOK_URL не задан — пропускаем обновление подписки")
        return
    update_types = [
        "message_created",
        "message_callback",
        "bot_started",
        "comment_created",
        "comment_edited",
        "comment_removed"
    ]
    body = {
        "url": WEBHOOK_URL,
        "update_types": update_types,
    }
    if WEBHOOK_SECRET:
        body["secret"] = WEBHOOK_SECRET
    try:
        response = requests.post(
            f"{API_URL}/subscriptions",
            headers={"Authorization": TOKEN, "Content-Type": "application/json"},
            json=body,
            timeout=10,
            verify=False
        )
        logger.info(f"Обновление подписки: status={response.status_code}, body={response.text}")
    except Exception as e:
        logger.error(f"Ошибка обновления подписки: {e}")


register_commands()
update_webhook_subscription()


def get_post_seq(post_id):
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
    if not chat_id or not post_id:
        return f"https://max.ru/@{CHANNEL_USERNAME}"
    seq = get_post_seq(post_id)
    if seq:
        seq_bytes = seq.to_bytes(8, 'big')
        encoded = base64.urlsafe_b64encode(seq_bytes).decode().rstrip('=')
        return f"https://max.ru/c/{chat_id}/{encoded}"
    return f"https://max.ru/@{CHANNEL_USERNAME}"


def get_author_name(message_data):
    from_data = message_data.get("from", {}) or message_data.get("sender", {})
    first_name = from_data.get("first_name")
    name = from_data.get("name")
    if first_name:
        return first_name
    if name:
        return name
    return "Пользователь"


def send_message(user_id=None, chat_id=None, text="", attachments=None, keyboard=None):
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
    buttons = []
    row = []
    if post_link:
        row.append({"type": "link", "text": "\U0001F517 Открыть пост", "url": post_link})
    if post_id and comment_mid:
        row.append({
            "type": "callback",
            "text": "\U0001F4AC Ответить",
            "payload": f"reply:{post_id}:{comment_mid}"
        })
    if row:
        buttons.append(row)
    if not buttons:
        return None
    return [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]


def send_product_card(user_id, item):
    text = (
        f"\U0001FA91 **{item['name']}**\n"
        f"\U0001F4B0 **{item['price']} \u20bd**\n"
        f"\U0001F4DD {item['description']}\n\n"
        f"\U0001F447 **Выберите действие:**"
    )
    attachments = []
    if item.get("photo_url"):
        attachments.append({
            "type": "image",
            "payload": {"url": item["photo_url"]}
        })
    keyboard_buttons = [
        [
            {"type": "callback", "text": "\U0001F6D2 В корзину", "payload": f"add_to_cart:{item['id']}"},
            {"type": "callback", "text": "\U0001F4AC Быстрый заказ", "payload": f"quick_order:{item['id']}"}
        ],
        [
            {"type": "callback", "text": "\u2753 Задать вопрос", "payload": f"ask_question:{item['id']}"}
        ]
    ]
    send_message(user_id=user_id, text=text, attachments=attachments, keyboard=keyboard_buttons)


def send_main_menu(user_id):
    keyboard_buttons = [
        [
            {"type": "message", "text": "\U0001F4CB Каталог", "payload": "\U0001F4CB Каталог"},
            {"type": "message", "text": "\U0001F6D2 Корзина", "payload": "\U0001F6D2 Корзина"}
        ],
        [
            {"type": "message", "text": "\U0001F4DE Связаться с мастером", "payload": "\U0001F4DE Связаться с мастером"},
            {"type": "message", "text": "\u2753 Задать вопрос", "payload": "\u2753 Задать вопрос"}
        ]
    ]
    send_message(user_id=user_id, text="\U0001F447 **Выберите действие — просто нажмите кнопку:**", keyboard=keyboard_buttons)


def show_catalog(user_id):
    categories = CATALOG_DATA.get("categories", [])
    if not categories:
        send_message(user_id=user_id, text="\U0001F4CB Каталог пока пуст.\n\n\U000023F3 Загляните позже — скоро появятся новые изделия!")
        return
    buttons = []
    row = []
    for i, cat in enumerate(categories):
        row.append({"type": "callback", "text": f"\U0001F4E6 {cat['name']}", "payload": f"show_category:{i}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    send_message(user_id=user_id, text="\U0001F6E0\uFE0F **Каталог мастерской Игнатьевых**\n\n\U0001F447 Выберите категорию:", keyboard=buttons)


def show_cart(user_id):
    cart = user_carts.get(user_id, [])
    if not cart:
        send_message(user_id=user_id, text="\U0001F6D2 Ваша корзина пуста.\n\n\U0001F449 Откройте **\U0001F4CB Каталог** — выберите понравившееся изделие!")
        return
    cart_text = "\U0001F6D2 **Ваша корзина:**\n\n"
    total = 0
    for item_id in cart:
        item = find_item_by_id(item_id)
        if item:
            cart_text += f"\u2022 {item['name']} \u2014 **{item['price']} \u20bd**\n"
            total += item["price"]
    cart_text += f"\n\U0001F4B0 **Итого: {total} \u20bd**\n\n\U0001F447 **Чтобы продолжить, нажмите кнопку:**"
    keyboard_buttons = [
        [
            {"type": "callback", "text": "\u2705 Оформить заказ", "payload": "start_checkout"},
            {"type": "callback", "text": "\U0001F5D1 Очистить", "payload": "clear_cart"}
        ]
    ]
    send_message(user_id=user_id, text=cart_text, keyboard=keyboard_buttons)


@app.route("/webhook", methods=["POST", "GET"])
def webhook():
    global question_counter

    if request.method == "GET":
        return jsonify({"status": "ok"}), 200

    if WEBHOOK_SECRET:
        received_secret = request.headers.get("X-Max-Bot-Api-Secret", "")
        if not hmac.compare_digest(received_secret, WEBHOOK_SECRET):
            logger.warning("Неверный секрет webhook")
            return jsonify({"error": "forbidden"}), 403

    data = request.get_json()
    update_type = data.get("update_type", "")
    logger.info(f"Получено событие: {update_type}")

    # === ОБРАБОТКА КНОПКИ «НАЧАТЬ» ===
    if update_type == "bot_started":
        chat_id = data.get("chat_id")
        sender_id = data.get("user", {}).get("user_id")
        logger.info(f"bot_started: chat_id={chat_id}, user_id={sender_id}, data={json.dumps(data, ensure_ascii=False)}")
        welcome_text = (
            "\U0001FAB5 **Мастерская Игнатьевых**\n\n"
            "Здесь можно посмотреть каталог, собрать корзину и оформить заказ \u2014 "
            "**ничего писать не нужно.**\n\n"
            "\U0001F447 **Нажмите кнопку, чтобы начать:**"
        )
        if chat_id:
            send_message(chat_id=chat_id, text=welcome_text)
            keyboard_buttons = [
                [
                    {"type": "message", "text": "\U0001F4CB Каталог", "payload": "\U0001F4CB Каталог"},
                    {"type": "message", "text": "\U0001F6D2 Корзина", "payload": "\U0001F6D2 Корзина"}
                ],
                [
                    {"type": "message", "text": "\U0001F4DE Связаться с мастером", "payload": "\U0001F4DE Связаться с мастером"},
                    {"type": "message", "text": "\u2753 Задать вопрос", "payload": "\u2753 Задать вопрос"}
                ]
            ]
            send_message(chat_id=chat_id, text="\U0001F447 **Выберите действие \u2014 просто нажмите кнопку:**", keyboard=keyboard_buttons)
        elif sender_id:
            send_message(user_id=sender_id, text=welcome_text)
            send_main_menu(sender_id)
        return jsonify({"ok": True}), 200

    # === Обработка нажатия callback-кнопки ===
    if update_type == "message_callback":
        callback = data.get("callback", {})
        callback_id = callback.get("callback_id", "")
        payload = callback.get("payload", "")
        sender_id = (
            callback.get("user", {}).get("user_id", "") or
            data.get("sender", {}).get("user_id", "") or
            data.get("user", {}).get("user_id", "")
        )

        logger.info(f"Callback от user_id={sender_id}, payload={payload}, callback_id={callback_id}")

        if payload.startswith("reply:") and sender_id:
            parts = payload.split(":", 2)
            if len(parts) == 3:
                post_id = parts[1]
                comment_mid = parts[2]
                pending_replies[sender_id] = {"post_id": post_id, "comment_mid": comment_mid}
                answer_callback(callback_id, "\u270D\uFE0F Напишите ответ — бот отправит его в канал")
                send_message(user_id=sender_id, text="\u270D\uFE0F **Напишите ответ** следующим сообщением \u2014 бот отправит его как комментарий в канале.")
            else:
                answer_callback(callback_id, "Ошибка: неверный формат")
            return jsonify({"ok": True}), 200

        elif payload.startswith("show_category:") and sender_id:
            cat_index = int(payload.split(":", 1)[1])
            categories = CATALOG_DATA.get("categories", [])
            if cat_index < len(categories):
                category = categories[cat_index]
                answer_callback(callback_id, f"\U0001F4E6 Открываю: {category['name']}")
                for item in category.get("items", []):
                    send_product_card(sender_id, item)
            else:
                answer_callback(callback_id, "Категория не найдена")
            return jsonify({"ok": True}), 200

        elif payload.startswith("add_to_cart:") and sender_id:
            item_id = payload.split(":", 1)[1]
            item = find_item_by_id(item_id)
            if not item:
                answer_callback(callback_id, "Товар не найден")
                return jsonify({"ok": True}), 200
            if sender_id not in user_carts:
                user_carts[sender_id] = []
            user_carts[sender_id].append(item_id)
            answer_callback(callback_id, "\u2705 Добавлено в корзину!")
            count = len(user_carts[sender_id])
            send_message(user_id=sender_id, text=(
                f"\u2705 **«{item['name']}» добавлен в корзину!**\n"
                f"\U0001F4E6 В корзине товаров: **{count}**\n\n"
                f"\U0001F449 Нажмите **\U0001F6D2 Корзина**, чтобы оформить заказ."
            ))
            return jsonify({"ok": True}), 200

        elif payload.startswith("quick_order:") and sender_id:
            item_id = payload.split(":", 1)[1]
            item = find_item_by_id(item_id)
            if not item:
                answer_callback(callback_id, "Товар не найден")
                return jsonify({"ok": True}), 200
            answer_callback(callback_id, "\u2705 Принято!")
            send_message(user_id=sender_id, text=(
                f"\U0001F4D8 **Быстрый заказ: {item['name']}**\n"
                f"\U0001F4B0 Цена: **{item['price']} \u20bd**\n\n"
                f"\U0001F4DE **Напишите ваш номер телефона** \u2014 мастер свяжется с вами для уточнения деталей."
            ))
            pending_replies[sender_id] = {"step": "waiting_phone_quick", "item": item}
            return jsonify({"ok": True}), 200

        elif payload.startswith("ask_question:") and sender_id:
            item_id = payload.split(":", 1)[1]
            item = find_item_by_id(item_id)
            item_name = item["name"] if item else "изделие"
            answer_callback(callback_id, "\U0001F4AC Напишите вопрос")
            send_message(
                user_id=sender_id,
                text=(
                    f"\U0001F4AC **Напишите ваш вопрос про «{item_name}».**\n\n"
                    f"\U000023F1\uFE0F Мастер увидит его сразу и ответит **в течение 30 минут.**"
                )
            )
            pending_replies[sender_id] = {"step": "waiting_question", "item_id": item_id, "item_name": item_name}
            return jsonify({"ok": True}), 200

        elif payload == "start_checkout" and sender_id:
            cart = user_carts.get(sender_id, [])
            if not cart:
                answer_callback(callback_id, "Корзина пуста")
                return jsonify({"ok": True}), 200
            total = 0
            items_text = ""
            for item_id in cart:
                item = find_item_by_id(item_id)
                if item:
                    items_text += f"\u2022 {item['name']} \u2014 **{item['price']} \u20bd**\n"
                    total += item["price"]
            answer_callback(callback_id, "\u2705 Начинаем оформление")
            send_message(user_id=sender_id, text=(
                f"\U0001F6D2 **Оформляем заказ:**\n\n"
                f"{items_text}\n"
                f"\U0001F4B0 **Итого: {total} \u20bd**\n\n"
                f"\u270F\uFE0F **Напишите, пожалуйста, ваше имя:**"
            ))
            pending_replies[sender_id] = {"step": "waiting_name", "cart": cart, "total": total}
            return jsonify({"ok": True}), 200

        elif payload == "clear_cart" and sender_id:
            user_carts[sender_id] = []
            answer_callback(callback_id, "\U0001F5D1 Корзина очищена")
            send_message(user_id=sender_id, text="\U0001F5D1 **Корзина очищена.**\n\n\U0001F449 Откройте **\U0001F4CB Каталог**, чтобы выбрать новое изделие.")
            return jsonify({"ok": True}), 200

        else:
            answer_callback(callback_id, "\u2705 Ок")
            return jsonify({"ok": True}), 200

    # === Обработка событий с комментариями ===
    message = data.get("message", {})
    recipient = message.get("recipient", {})
    chat_id = recipient.get("chat_id", "")
    post_id = recipient.get("post_id", "")
    comment_mid = message.get("body", {}).get("mid", "")
    author_name = get_author_name(message)

    if update_type == "comment_created":
        comment_text = message.get("body", {}).get("text", "")
        notification = f"\U0001F195 **Новый комментарий**\n\U0001F464 Автор: {author_name}\n\n{comment_text}"
        post_link = build_post_link(chat_id, post_id)
        keyboard = build_keyboard(post_link, post_id, comment_mid)
        send_message(user_id=NOTIFY_CHAT_ID, text=notification, attachments=keyboard)

    elif update_type == "comment_edited":
        comment_text = message.get("body", {}).get("text", "")
        notification = f"\u270F\uFE0F **Изменён комментарий**\n\U0001F464 Автор: {author_name}\n\n{comment_text}"
        post_link = build_post_link(chat_id, post_id)
        keyboard = build_keyboard(post_link, post_id, comment_mid)
        send_message(user_id=NOTIFY_CHAT_ID, text=notification, attachments=keyboard)

    elif update_type == "comment_removed":
        notification = f"\U0001F5D1\uFE0F **Удалён комментарий**\n\U0001F464 Автор: {author_name}"
        post_link = build_post_link(chat_id, post_id)
        keyboard = build_keyboard(post_link)
        send_message(user_id=NOTIFY_CHAT_ID, text=notification, attachments=keyboard)

    # === Обработка обычных сообщений боту ===
    elif update_type == "message_created":
        sender_id = message.get("sender", {}).get("user_id", "")
        text = message.get("body", {}).get("text", "")
        first_name = message.get("sender", {}).get("first_name", "Пользователь")
        logger.info(f"Сообщение от user_id={sender_id}: {text}")
        cmd = text.lower().strip() if text else ""

        # === ПЕРЕХВАТ ОТВЕТОВ АДМИНА (диалоги с клиентами) ===
        if NOTIFY_CHAT_ID and str(sender_id) == NOTIFY_CHAT_ID and text and not text.startswith("/"):
            match = re.match(r'^#?(\d+)\s*[:\.\u3001\s]\s*(.+)', text, re.DOTALL)
            if match:
                num = int(match.group(1))
                reply_text = match.group(2).strip()
                if num in active_dialogs:
                    client_user_id = active_dialogs[num]["user_id"]
                    send_message(user_id=client_user_id, text=reply_text)
                    send_message(user_id=sender_id, text=f"\u2705 **Ответ #{num} отправлен клиенту.**")
                    logger.info(f"Ответ #{num} отправлен user_id={client_user_id}: {reply_text}")
                    del active_dialogs[num]
                else:
                    active_nums = list(active_dialogs.keys())
                    send_message(user_id=sender_id, text=f"\u26A0\uFE0F **Диалог #{num} не найден.**\n\U0001F4CB Активные: {active_nums}")
                return jsonify({"ok": True}), 200

        # Кнопки типа message из главного меню
        if text and text.strip() == "\U0001F4CB Каталог":
            show_catalog(sender_id)
            return jsonify({"ok": True}), 200

        if text and text.strip() == "\U0001F6D2 Корзина":
            show_cart(sender_id)
            return jsonify({"ok": True}), 200

        if text and text.strip() == "\U0001F4DE Связаться с мастером":
            send_message(user_id=sender_id, text=(
                "\U0001F4DE **Связаться с мастером:**\n\n"
                "\U0001F464 Евгений\n"
                "\U0001F4F1 **8 (989) 622-37-32**\n\n"
                "\U0001F449 Или закажите через каталог \u2014 нажмите **\U0001F4CB Каталог** и выберите изделие."
            ))
            return jsonify({"ok": True}), 200

        if text and text.strip() == "\u2753 Задать вопрос":
            pending_replies[sender_id] = {"step": "waiting_question"}
            send_message(
                user_id=sender_id,
                text=(
                    "\U0001F4AC **Напишите ваш вопрос прямо здесь.**\n\n"
                    "\U000023F1\uFE0F Мастер увидит его сразу и ответит **в течение 30 минут.**"
                )
            )
            return jsonify({"ok": True}), 200

        if cmd in ["/catalog", "/каталог"]:
            show_catalog(sender_id)
            return jsonify({"ok": True}), 200

        if cmd in ["/cart", "/корзина"]:
            show_cart(sender_id)
            return jsonify({"ok": True}), 200

        if cmd in ["/help", "/помощь"]:
            send_message(user_id=sender_id, text=(
                "\U0001FAB5 **Мастерская Игнатьевых \u2014 помощь**\n\n"
                "\U0001F4CB **/каталог** \u2014 открыть каталог изделий\n"
                "\U0001F6D2 **/корзина** \u2014 посмотреть корзину\n"
                "\u2753 **/помощь** \u2014 эта справка\n\n"
                "\U0001F4A1 Можно нажимать кнопки под сообщениями бота \u2014 **не нужно ничего писать вручную.**"
            ))
            return jsonify({"ok": True}), 200

        # Команда /вопросы — только для админа: показать активные диалоги
        if cmd == "/вопросы" and NOTIFY_CHAT_ID and str(sender_id) == NOTIFY_CHAT_ID:
            if active_dialogs:
                lines = []
                for num, d in sorted(active_dialogs.items()):
                    item_info = f" (\U0001F4E6 изделие: {d['item_name']})" if d.get("item_name") else ""
                    preview = d['text'][:60] + ("..." if len(d['text']) > 60 else "")
                    lines.append(f"#{num} \u2014 {d['name']}{item_info}: {preview}")
                send_message(user_id=sender_id, text="\U0001F4CB **Активные диалоги:**\n\n" + "\n".join(lines))
            else:
                send_message(user_id=sender_id, text="\u2705 Нет активных диалогов.")
            return jsonify({"ok": True}), 200

        # Обработка шагов оформления заказа, быстрого заказа и вопросов
        if sender_id in pending_replies:
            state = pending_replies.get(sender_id)
            step = state.get("step") if isinstance(state, dict) else None

            if step == "waiting_question":
                question_text = text.strip() if text else ""
                if not question_text:
                    send_message(user_id=sender_id, text="\u0001F4AC **Пожалуйста, напишите ваш вопрос:**")
                    return jsonify({"ok": True}), 200
                state = pending_replies.pop(sender_id)
                item_name = state.get("item_name")
                question_counter += 1
                num = question_counter
                active_dialogs[num] = {
                    "user_id": sender_id,
                    "name": first_name,
                    "text": question_text,
                    "item_name": item_name
                }
                if item_name:
                    forward_text = (
                        f"#{num} \U0001F4AC **Вопрос от {first_name}**\n"
                        f"\U0001F4E6 Изделие: {item_name}\n\n"
                        f"{question_text}\n\n"
                        f"\U000021A9\uFE0F **Чтобы ответить, напишите:** {num}: ваш текст"
                    )
                else:
                    forward_text = (
                        f"#{num} \U0001F4AC **Вопрос от {first_name}**\n\n"
                        f"{question_text}\n\n"
                        f"\U000021A9\uFE0F **Чтобы ответить, напишите:** {num}: ваш текст"
                    )
                send_message(user_id=NOTIFY_CHAT_ID, text=forward_text)
                send_message(user_id=sender_id, text=(
                    "\u2705 **Вопрос передан мастеру!**\n\n"
                    "\U000023F1\uFE0F Ответим **в течение 30 минут.**"
                ))
                logger.info(f"Вопрос #{num} от {first_name} (user_id={sender_id}): {question_text}")
                return jsonify({"ok": True}), 200

            elif step == "waiting_name":
                name = text.strip()
                if not name:
                    send_message(user_id=sender_id, text="\u270F\uFE0F **Пожалуйста, напишите ваше имя:**")
                    return jsonify({"ok": True}), 200
                pending_replies[sender_id]["name"] = name
                pending_replies[sender_id]["step"] = "waiting_phone"
                send_message(user_id=sender_id, text=(
                    f"{name}, спасибо! \u2705\n\n"
                    f"\U0001F4DE **Напишите ваш номер телефона** (можно в любом формате):"
                ))
                return jsonify({"ok": True}), 200

            elif step == "waiting_phone":
                phone = text.strip()
                if not phone:
                    send_message(user_id=sender_id, text="\U0001F4DE **Пожалуйста, напишите номер телефона:**")
                    return jsonify({"ok": True}), 200
                state = pending_replies.pop(sender_id)
                cart = state.get("cart", [])
                total = state.get("total", 0)
                name = state.get("name", "Не указано")
                order_text = (
                    f"\U0001F4D8 **Новый заказ!**\n"
                    f"\U0001F464 Имя: {name}\n"
                    f"\U0001F4DE Телефон: {phone}\n"
                    f"\U0001F4E6 Товары:\n"
                )
                for item_id in cart:
                    item = find_item_by_id(item_id)
                    if item:
                        order_text += f"\u2022 {item['name']} \u2014 {item['price']} \u20bd\n"
                order_text += f"\n\U0001F4B0 **Итого: {total} \u20bd**"
                send_message(user_id=NOTIFY_CHAT_ID, text=order_text)
                send_message(user_id=sender_id, text=(
                    "\u2705 **Спасибо за заказ!**\n\n"
                    "Мастер свяжется с вами в ближайшее время.\n\n"
                    "\U0001F449 Если нужно что-то изменить \u2014 нажмите **\U0001F6D2 Корзина**."
                ))
                user_carts[sender_id] = []
                return jsonify({"ok": True}), 200

            elif step == "waiting_phone_quick":
                phone = text.strip()
                if not phone:
                    send_message(user_id=sender_id, text="\U0001F4DE **Пожалуйста, напишите номер телефона:**")
                    return jsonify({"ok": True}), 200
                state = pending_replies.pop(sender_id)
                item = state.get("item")
                order_text = (
                    f"\U0001F4D8 **Быстрый заказ!**\n"
                    f"\U0001F4E6 Товар: {item['name']}\n"
                    f"\U0001F4B0 Цена: {item['price']} \u20bd\n"
                    f"\U0001F4DE Телефон: {phone}"
                )
                send_message(user_id=NOTIFY_CHAT_ID, text=order_text)
                send_message(user_id=sender_id, text=(
                    "\u2705 **Спасибо!**\n\n"
                    "Мастер свяжется с вами в ближайшее время.\n\n"
                    "\U0001F449 Если нужно что-то изменить \u2014 нажмите **\U0001F4CB Каталог**."
                ))
                return jsonify({"ok": True}), 200

        # Проверяем, есть ли ожидающий ответ на комментарий
        if sender_id in pending_replies and text and not text.startswith("/"):
            reply_data = pending_replies.get(sender_id)
            if isinstance(reply_data, dict) and "post_id" in reply_data:
                reply_data = pending_replies.pop(sender_id)
                post_id = reply_data["post_id"]
                comment_mid = reply_data["comment_mid"]
                success = post_comment(post_id, text, reply_to_mid=comment_mid)
                if success:
                    send_message(user_id=sender_id, text="\u2705 **Ответ отправлен в канал!**")
                else:
                    send_message(user_id=sender_id, text="\u274C **Не удалось отправить ответ.**\n\n\U000026A0\uFE0F Проверьте, что бот \u2014 администратор канала с правом write.")
                    pending_replies[sender_id] = reply_data
            return jsonify({"ok": True}), 200

        elif cmd and cmd.startswith("/start"):
            send_message(user_id=sender_id, text=(
                "\U0001FAB5 **Мастерская Игнатьевых**\n\n"
                "Здесь можно посмотреть каталог, собрать корзину и оформить заказ \u2014 "
                "**ничего писать не нужно.**\n\n"
                "\U0001F447 **Нажмите кнопку, чтобы начать:**"
            ))
            send_main_menu(sender_id)
            return jsonify({"ok": True}), 200

    return jsonify({"ok": True}), 200


@app.route("/", methods=["GET"])
def index():
    return "Бот работает!", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
