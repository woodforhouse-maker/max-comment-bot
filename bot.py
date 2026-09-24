import os
import hmac
import base64
import json
import re
import time
import signal
import threading
import logging
from flask import Flask, request, jsonify
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# === ЛОГИРОВАНИЕ ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# === КОНФИГУРАЦИЯ ===
TOKEN = os.environ.get("MAX_BOT_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
NOTIFY_CHAT_ID = os.environ.get("NOTIFY_CHAT_ID", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
API_URL = "https://platform-api2.max.ru"
CHANNEL_USERNAME = "channel_ignatyevy"
SESSION_TIMEOUT = 600  # 10 минут
STATE_DIR = os.path.join(os.path.dirname(__file__), "state")
os.makedirs(STATE_DIR, exist_ok=True)

# Список админов: Мария (NOTIFY_CHAT_ID) и Евгений (user_id 39193669)
ADMIN_IDS = set()
if NOTIFY_CHAT_ID:
    ADMIN_IDS.add(str(NOTIFY_CHAT_ID))
ADMIN_IDS.add("39193669")

REQUIRED_ENV = ["MAX_BOT_TOKEN", "WEBHOOK_URL", "NOTIFY_CHAT_ID"]
_missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
if _missing:
    logger.warning(f"Не заданы переменные окружения: {', '.join(_missing)}. Бот запустится, но часть функций не будет работать.")

# === СОСТОЯНИЕ С ПОДДЕРЖКОЙ PERSISTENCE ===
_lock = threading.Lock()

pending_replies: dict = {}
user_carts: dict = {}
question_counter = 0
active_dialogs: dict = {}

STATE_FILES = {
    "pending_replies": os.path.join(STATE_DIR, "pending_replies.json"),
    "user_carts": os.path.join(STATE_DIR, "user_carts.json"),
    "active_dialogs": os.path.join(STATE_DIR, "active_dialogs.json"),
    "question_counter": os.path.join(STATE_DIR, "question_counter.json"),
}

# Хранилище сессий админ-режима (добавление/редактирование)
admin_sessions: dict = {}


def save_state():
    """Сохраняет состояние в JSON-файлы."""
    try:
        with open(STATE_FILES["pending_replies"], "w", encoding="utf-8") as f:
            json.dump(_strip_for_save(pending_replies), f, ensure_ascii=False)
        with open(STATE_FILES["user_carts"], "w", encoding="utf-8") as f:
            json.dump(user_carts, f, ensure_ascii=False)
        with open(STATE_FILES["active_dialogs"], "w", encoding="utf-8") as f:
            json.dump(active_dialogs, f, ensure_ascii=False)
        with open(STATE_FILES["question_counter"], "w", encoding="utf-8") as f:
            json.dump({"value": question_counter}, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Ошибка сохранения состояния: {e}")


def _strip_for_save(d):
    result = {}
    for k, v in d.items():
        if isinstance(v, dict):
            clean = {kk: vv for kk, vv in v.items() if kk != "item"}
            result[k] = clean
        else:
            result[k] = v
    return result


def load_state():
    global question_counter, pending_replies, user_carts, active_dialogs
    try:
        if os.path.exists(STATE_FILES["pending_replies"]):
            with open(STATE_FILES["pending_replies"], "r", encoding="utf-8") as f:
                pending_replies = json.load(f)
            logger.info(f"Загружено pending_replies: {len(pending_replies)} записей")
    except Exception as e:
        logger.error(f"Ошибка загрузки pending_replies: {e}")

    try:
        if os.path.exists(STATE_FILES["user_carts"]):
            with open(STATE_FILES["user_carts"], "r", encoding="utf-8") as f:
                user_carts = json.load(f)
            logger.info(f"Загружено user_carts: {len(user_carts)} записей")
    except Exception as e:
        logger.error(f"Ошибка загрузки user_carts: {e}")

    try:
        if os.path.exists(STATE_FILES["active_dialogs"]):
            with open(STATE_FILES["active_dialogs"], "r", encoding="utf-8") as f:
                raw = json.load(f)
                active_dialogs = {int(k): v for k, v in raw.items()}
            logger.info(f"Загружено active_dialogs: {len(active_dialogs)} записей")
    except Exception as e:
        logger.error(f"Ошибка загрузки active_dialogs: {e}")

    try:
        if os.path.exists(STATE_FILES["question_counter"]):
            with open(STATE_FILES["question_counter"], "r", encoding="utf-8") as f:
                question_counter = json.load(f).get("value", 0)
            logger.info(f"Загружен question_counter: {question_counter}")
    except Exception as e:
        logger.error(f"Ошибка загрузки question_counter: {e}")

    for uid, state in pending_replies.items():
        if isinstance(state, dict) and state.get("step") == "waiting_contact_quick" and "item_id" in state:
            item = find_item_by_id(state["item_id"])
            if item:
                state["item"] = item
                logger.info(f"Восстановлен item для pending_replies[{uid}]")


def cleanup_stale_sessions():
    now = time.time()
    stale = []
    with _lock:
        for uid, state in pending_replies.items():
            if isinstance(state, dict) and "timestamp" in state:
                if now - state["timestamp"] > SESSION_TIMEOUT:
                    stale.append(uid)
        for uid in stale:
            del pending_replies[uid]
            logger.info(f"Удалена устаревшая сессия для user_id={uid}")
    if stale:
        save_state()


# === КАТАЛОГ ===

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
CATALOG_INDEX: dict = {}
for _cat in CATALOG_DATA.get("categories", []):
    for _item in _cat.get("items", []):
        CATALOG_INDEX[_item["id"]] = _item


def rebuild_catalog_index():
    global CATALOG_DATA
    CATALOG_DATA = load_catalog()
    CATALOG_INDEX.clear()
    for _cat in CATALOG_DATA.get("categories", []):
        for _item in _cat.get("items", []):
            CATALOG_INDEX[_item["id"]] = _item


def find_item_by_id(item_id):
    return CATALOG_INDEX.get(item_id)


def save_catalog():
    """Перезаписывает catalog.json, предварительно создав резервную копию."""
    file_path = os.path.join(os.path.dirname(__file__), "catalog.json")
    backup_path = os.path.join(os.path.dirname(__file__), "catalog_backup.json")
    try:
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                old_data = f.read()
            with open(backup_path, "w", encoding="utf-8") as f:
                f.write(old_data)
    except Exception as e:
        logger.error(f"Ошибка создания резервной копии catalog.json: {e}")
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(CATALOG_DATA, f, ensure_ascii=False, indent=2)
        logger.info("catalog.json сохранён.")
    except Exception as e:
        logger.error(f"Ошибка записи catalog.json: {e}")


def is_admin(user_id):
    return str(user_id) in ADMIN_IDS


def slugify(text):
    """Транслитерация кириллицы в латиницу, генерация slug для id товара."""
    translit_map = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
        'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
        ' ': '_', '"': '', '«': '', '»': '', "'": '', ',': '', '.': '',
        '(': '', ')': '', '!': '', '?': '', ':': '', ';': '',
    }
    result = text.lower().strip()
    for k, v in translit_map.items():
        result = result.replace(k, v)
    result = re.sub(r'[^a-z0-9_]', '', result)
    result = re.sub(r'_+', '_', result).strip('_')
    return result if result else "item"


def generate_unique_id(base_id):
    if base_id not in CATALOG_INDEX:
        return base_id
    i = 2
    while f"{base_id}_{i}" in CATALOG_INDEX:
        i += 1
    return f"{base_id}_{i}"


# === FLASK ===

app = Flask(__name__)


# === РАБОТА С API MAX ===

def api_request(method, endpoint, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = TOKEN
    headers["Content-Type"] = "application/json"
    kwargs.setdefault("timeout", 10)
    kwargs.setdefault("verify", False)
    try:
        resp = requests.request(method, f"{API_URL}{endpoint}", headers=headers, **kwargs)
        logger.info(f"API {method} {endpoint}: status={resp.status_code}")
        return resp
    except requests.RequestException as e:
        logger.error(f"API {method} {endpoint} — ошибка: {e}")
        return None


def register_commands():
    commands = [
        {"name": "start", "description": "Начать работу с ботом"},
        {"name": "каталог", "description": "Открыть каталог изделий"},
        {"name": "корзина", "description": "Посмотреть корзину"},
        {"name": "помощь", "description": "Как пользоваться ботом"},
        {"name": "cancel", "description": "Отменить текущее действие"},
    ]
    resp = api_request("PATCH", "/me/commands", json={"commands": commands})
    if resp:
        logger.info(f"Регистрация команд: {resp.text}")


def update_webhook_subscription():
    if not WEBHOOK_URL:
        logger.warning("WEBHOOK_URL не задан — пропускаем обновление подписки")
        return
    update_types = [
        "message_created",
        "message_callback",
        "bot_started",
        "comment_created",
        "comment_edited",
        "comment_removed",
    ]
    body = {"url": WEBHOOK_URL, "update_types": update_types}
    if WEBHOOK_SECRET:
        body["secret"] = WEBHOOK_SECRET
    resp = api_request("POST", "/subscriptions", json=body)
    if resp:
        logger.info(f"Обновление подписки: {resp.text}")


def get_post_seq(post_id):
    resp = api_request("GET", "/messages", params={"message_ids": post_id})
    if resp and resp.status_code == 200:
        messages = resp.json().get("messages", [])
        if messages:
            seq = messages[0].get("body", {}).get("seq")
            if seq:
                return int(seq)
    return None


def build_post_link(chat_id, post_id):
    if not chat_id or not post_id:
        return f"https://max.ru/@{CHANNEL_USERNAME}"
    seq = get_post_seq(post_id)
    if seq:
        seq_bytes = seq.to_bytes(8, "big")
        encoded = base64.urlsafe_b64encode(seq_bytes).decode().rstrip("=")
        return f"https://max.ru/c/{chat_id}/{encoded}"
    return f"https://max.ru/@{CHANNEL_USERNAME}"


def get_author_name(message_data):
    from_data = message_data.get("from", {}) or message_data.get("sender", {})
    return from_data.get("first_name") or from_data.get("name") or "Пользователь"


def send_message(user_id=None, chat_id=None, text="", attachments=None, keyboard=None):
    params = {}
    if user_id:
        params["user_id"] = int(user_id)
    elif chat_id:
        params["chat_id"] = int(chat_id)
    else:
        logger.error("send_message: не указан user_id или chat_id!")
        return

    body = {"text": text}
    if attachments:
        body["attachments"] = list(attachments)
    if keyboard:
        body.setdefault("attachments", [])
        body["attachments"].append({
            "type": "inline_keyboard",
            "payload": {"buttons": keyboard},
        })

    resp = api_request("POST", "/messages", params=params, json=body)
    if resp:
        logger.info(f"Отправка: {resp.text[:200]}")
    return resp


def answer_callback(callback_id, notification=None):
    if not callback_id:
        return
    body = {}
    if notification:
        body["notification"] = notification
    api_request("POST", "/answers", params={"callback_id": callback_id}, json=body)


def post_comment(post_id, text, reply_to_mid=None):
    body = {"text": text}
    if reply_to_mid:
        body["link"] = {"type": "reply", "mid": reply_to_mid}
    resp = api_request("POST", f"/messages/{post_id}/comments", json=body)
    return resp is not None and resp.status_code == 200


# === КЛАВИАТУРЫ И МЕНЮ ===

def build_main_menu_keyboard():
    return [
        [
            {"type": "message", "text": "\U0001F4CB Каталог", "payload": "\U0001F4CB Каталог"},
            {"type": "message", "text": "\U0001F6D2 Корзина", "payload": "\U0001F6D2 Корзина"},
        ],
        [
            {"type": "message", "text": "\U0001F4DE Мастер", "payload": "\U0001F4DE Мастер"},
            {"type": "message", "text": "\u2753 Задать вопрос", "payload": "\u2753 Задать вопрос"},
        ],
    ]


def build_catalog_keyboard():
    return [
        [{"type": "callback", "text": "\U0001F4CB Каталог", "payload": "back_to_catalog"}],
    ]


def build_cart_catalog_keyboard():
    return [
        [
            {"type": "message", "text": "\U0001F6D2 Корзина", "payload": "\U0001F6D2 Корзина"},
            {"type": "message", "text": "\U0001F4CB Каталог", "payload": "\U0001F4CB Каталог"},
        ],
    ]


def build_cancel_keyboard():
    return [
        [{"type": "callback", "text": "\u274C Отмена", "payload": "cancel_action"}],
    ]


def build_admin_add_keyboard():
    return [
        [
            {"type": "callback", "text": "\U0001F4E6 Категорию", "payload": "admin_add_category"},
            {"type": "callback", "text": "\U0001F4CB Товар", "payload": "admin_add_item"},
        ],
        [{"type": "callback", "text": "\u2B05\ufe0f Назад", "payload": "admin_back_to_menu"}],
    ]


def build_admin_edit_keyboard():
    return [
        [
            {"type": "callback", "text": "\u270f\ufe0f Категорию", "payload": "admin_edit_category"},
            {"type": "callback", "text": "\u270f\ufe0f Товар", "payload": "admin_edit_item"},
        ],
        [
            {"type": "callback", "text": "\U0001F4E6 Удалить категорию", "payload": "admin_delete_category"},
            {"type": "callback", "text": "\U0001F4CB Удалить товар", "payload": "admin_delete_item"},
        ],
        [{"type": "callback", "text": "\u2B05\ufe0f Назад", "payload": "admin_back_to_menu"}],
    ]


def send_welcome(target_id, is_chat=False):
    text = (
        "Привет! Я бот мастерской Игнатьевых. \U0001FAB5\n\n"
        "Здесь вы можете посмотреть каталог изделий, "
        "собрать корзину и оформить заказ.\n\n"
        "Вам не нужно ничего писать — просто нажмите кнопку:"
    )
    if is_chat:
        send_message(chat_id=target_id, text=text)
    else:
        send_message(user_id=target_id, text=text)
    send_message(
        chat_id=target_id if is_chat else None,
        user_id=None if is_chat else target_id,
        text="\U0001F447Выберите действие\U0001F447",
        keyboard=build_main_menu_keyboard(),
    )


def send_main_menu(user_id):
    send_message(user_id=user_id, text="\U0001F447Выберите действие\U0001F447", keyboard=build_main_menu_keyboard())


def build_post_keyboard(post_link, post_id=None, comment_mid=None):
    buttons = []
    row = []
    if post_link:
        row.append({"type": "link", "text": "\U0001F517 Открыть пост", "url": post_link})
    if post_id and comment_mid:
        row.append({
            "type": "callback",
            "text": "\U0001F4AC Ответить",
            "payload": f"reply:{post_id}:{comment_mid}",
        })
    if row:
        buttons.append(row)
    if not buttons:
        return None
    return [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]


def send_product_card(user_id, item):
    text = (
        f"\U0001FA91 \"{item['name']}\"\n"
        f"\U0001F4B0 Цена: {item['price']} руб.\n"
        f"\U0001F4DD {item['description']}\n\n"
        f"\U0001F447Выберите действие\U0001F447"
    )
    attachments = []
    if item.get("photo_url"):
        attachments.append({"type": "image", "payload": {"url": item["photo_url"]}})
    keyboard_buttons = [
        [
            {"type": "callback", "text": "\U0001F6D2 В корзину", "payload": f"add_to_cart:{item['id']}"},
            {"type": "callback", "text": "\U0001F4AC Заказать", "payload": f"quick_order:{item['id']}"},
        ],
        [
            {"type": "callback", "text": "\u2753 Задать вопрос", "payload": f"ask_question:{item['id']}"},
            {"type": "callback", "text": "\U0001F4CB Каталог", "payload": "back_to_catalog"},
        ],
    ]
    send_message(user_id=user_id, text=text, attachments=attachments, keyboard=keyboard_buttons)


def show_catalog(user_id):
    categories = CATALOG_DATA.get("categories", [])
    if not categories:
        send_message(user_id=user_id, text="Каталог пока пуст.")
        return
    buttons = []
    for i, cat in enumerate(categories):
        buttons.append([
            {"type": "callback", "text": f"\U0001F4E6 {cat['name']}", "payload": f"show_category:{i}"}
        ])
    send_message(user_id=user_id, text="\U0001F6E0 Каталог мастерской Игнатьевых\n\nВыберите категорию:", keyboard=buttons)


def show_cart(user_id):
    cart = user_carts.get(str(user_id), [])
    if not cart:
        send_message(user_id=user_id, text="\U0001F6D2 Ваша корзина пуста.\n\n\U0001F449 Откройте \u00ab\U0001F4CB Каталог\u00bb — выберите изделие!", keyboard=build_catalog_keyboard())
        return
    cart_text = "\U0001F6D2 Ваша корзина:\n\n"
    total = 0
    for item_id in cart:
        item = find_item_by_id(item_id)
        if item:
            cart_text += f"\u2022 \"{item['name']}\" — {item['price']} руб.\n"
            total += item["price"]
    cart_text += f"\n\U0001F4B0 Итого: {total} руб.\n\n"
    keyboard_buttons = [
        [
            {"type": "callback", "text": "\u2705 Оформить заказ", "payload": "start_checkout"},
            {"type": "callback", "text": "\U0001F5D1 Очистить", "payload": "clear_cart"},
        ],
        [
            {"type": "message", "text": "\U0001F4CB Каталог", "payload": "\U0001F4CB Каталог"},
        ],
    ]
    send_message(user_id=user_id, text=cart_text + "\U0001F447Чтобы продолжить, нажмите кнопку\U0001F447", keyboard=keyboard_buttons)


# === ВАЛИДАЦИЯ ===

def validate_contact(text):
    if not text or len(text.strip()) < 3:
        return False, "Слишком короткое сообщение. Напишите имя и номер телефона."
    phone_match = re.search(r"[\d\+\-\(\)\s]{7,}", text)
    if not phone_match:
        return False, "Не вижу номер телефона. Напишите имя и номер, например: «Иван 89001234567»."
    return True, ""


# === ОБРАБОТКА CALLBACK ===

def handle_callback(data):
    callback = data.get("callback", {})
    callback_id = callback.get("callback_id", "")
    payload = callback.get("payload", "")
    sender_id = (
        callback.get("user", {}).get("user_id", "")
        or data.get("sender", {}).get("user_id", "")
        or data.get("user", {}).get("user_id", "")
    )
    sender_id = str(sender_id) if sender_id else ""
    logger.info(f"Callback от user_id={sender_id}, payload={payload}")

    if not sender_id:
        answer_callback(callback_id, "Ошибка: не удалось определить пользователя")
        return jsonify({"ok": True}), 200

    # --- cancel_action (кнопка Отмена из админ-режима) ---
    if payload == "cancel_action":
        admin_sessions.pop(sender_id, None)
        with _lock:
            pending_replies.pop(sender_id, None)
            save_state()
        answer_callback(callback_id, "Отменено")
        send_main_menu(sender_id)
        return jsonify({"ok": True}), 200

    # --- admin_back_to_menu ---
    if payload == "admin_back_to_menu":
        admin_sessions.pop(sender_id, None)
        answer_callback(callback_id, "")
        send_main_menu(sender_id)
        return jsonify({"ok": True}), 200

    # --- back_to_catalog ---
    if payload == "back_to_catalog":
        answer_callback(callback_id, "Каталог")
        show_catalog(sender_id)
        return jsonify({"ok": True}), 200

    # --- admin: add category ---
    if payload == "admin_add_category" and is_admin(sender_id):
        admin_sessions[sender_id] = {"action": "add_category", "step": "category_name"}
        answer_callback(callback_id, "Добавление категории")
        send_message(user_id=sender_id, text="\U0001F4E6 Напишите название новой категории:", keyboard=build_cancel_keyboard())
        return jsonify({"ok": True}), 200

    # --- admin: add item ---
    if payload == "admin_add_item" and is_admin(sender_id):
        categories = CATALOG_DATA.get("categories", [])
        if not categories:
            answer_callback(callback_id, "Сначала создайте категорию")
            send_message(user_id=sender_id, text="\u26a0\ufe0f Нет категорий. Сначала создайте категорию через /добавить.")
            return jsonify({"ok": True}), 200
        buttons = []
        for i, cat in enumerate(categories):
            buttons.append([
                {"type": "callback", "text": f"\U0001F4E6 {cat['name']}", "payload": f"admin_add_item_cat:{i}"}
            ])
        buttons.append([{"type": "callback", "text": "\u274C Отмена", "payload": "cancel_action"}])
        answer_callback(callback_id, "Выбор категории")
        send_message(user_id=sender_id, text="\U0001F4CB Выберите категорию для нового товара:", keyboard=buttons)
        return jsonify({"ok": True}), 200

    # --- admin: add item — category selected ---
    if payload.startswith("admin_add_item_cat:") and is_admin(sender_id):
        cat_index = int(payload.split(":", 1)[1])
        categories = CATALOG_DATA.get("categories", [])
        if cat_index >= len(categories):
            answer_callback(callback_id, "Категория не найдена")
            return jsonify({"ok": True}), 200
        admin_sessions[sender_id] = {"action": "add_item", "step": "item_name", "cat_index": cat_index}
        answer_callback(callback_id, f"Категория: {categories[cat_index]['name']}")
        send_message(user_id=sender_id, text="\U0001F4DD Напишите название изделия:", keyboard=build_cancel_keyboard())
        return jsonify({"ok": True}), 200

    # --- admin: edit category ---
    if payload == "admin_edit_category" and is_admin(sender_id):
        categories = CATALOG_DATA.get("categories", [])
        if not categories:
            answer_callback(callback_id, "Нет категорий")
            send_message(user_id=sender_id, text="\u26a0\ufe0f Нет категорий для редактирования.")
            return jsonify({"ok": True}), 200
        buttons = []
        for i, cat in enumerate(categories):
            buttons.append([
                {"type": "callback", "text": f"\u270f\ufe0f {cat['name']}", "payload": f"admin_edit_cat:{i}"}
            ])
        buttons.append([{"type": "callback", "text": "\u2B05\ufe0f Назад", "payload": "admin_back_to_menu"}])
        answer_callback(callback_id, "Редактирование категории")
        send_message(user_id=sender_id, text="\u270f\ufe0f Выберите категорию для переименования:", keyboard=buttons)
        return jsonify({"ok": True}), 200

    # --- admin: edit category — selected ---
    if payload.startswith("admin_edit_cat:") and is_admin(sender_id):
        cat_index = int(payload.split(":", 1)[1])
        categories = CATALOG_DATA.get("categories", [])
        if cat_index >= len(categories):
            answer_callback(callback_id, "Категория не найдена")
            return jsonify({"ok": True}), 200
        admin_sessions[sender_id] = {"action": "edit_category", "step": "new_name", "cat_index": cat_index}
        answer_callback(callback_id, f"Редактируем: {categories[cat_index]['name']}")
        send_message(user_id=sender_id, text=f"\u270f\ufe0f Текущее название: {categories[cat_index]['name']}\n\n\U0001F4DD Напишите новое название:", keyboard=build_cancel_keyboard())
        return jsonify({"ok": True}), 200

    # --- admin: edit item ---
    if payload == "admin_edit_item" and is_admin(sender_id):
        categories = CATALOG_DATA.get("categories", [])
        if not categories:
            answer_callback(callback_id, "Нет категорий")
            send_message(user_id=sender_id, text="\u26a0\ufe0f Нет категорий.")
            return jsonify({"ok": True}), 200
        buttons = []
        for i, cat in enumerate(categories):
            buttons.append([
                {"type": "callback", "text": f"\U0001F4E6 {cat['name']}", "payload": f"admin_edit_item_cat:{i}"}
            ])
        buttons.append([{"type": "callback", "text": "\u2B05\ufe0f Назад", "payload": "admin_back_to_menu"}])
        answer_callback(callback_id, "Выбор категории")
        send_message(user_id=sender_id, text="\u270f\ufe0f Выберите категорию:", keyboard=buttons)
        return jsonify({"ok": True}), 200

    # --- admin: edit item — category selected ---
    if payload.startswith("admin_edit_item_cat:") and is_admin(sender_id):
        cat_index = int(payload.split(":", 1)[1])
        categories = CATALOG_DATA.get("categories", [])
        if cat_index >= len(categories):
            answer_callback(callback_id, "Категория не найдена")
            return jsonify({"ok": True}), 200
        items = categories[cat_index].get("items", [])
        if not items:
            answer_callback(callback_id, "В категории нет товаров")
            send_message(user_id=sender_id, text="\u26a0\ufe0f В этой категории нет товаров.")
            return jsonify({"ok": True}), 200
        buttons = []
        for j, item in enumerate(items):
            buttons.append([
                {"type": "callback", "text": f"\U0001FA91 {item['name']} — {item['price']} руб.", "payload": f"admin_edit_item_select:{cat_index}:{j}"}
            ])
        buttons.append([{"type": "callback", "text": "\u2B05\ufe0f Назад", "payload": "admin_back_to_menu"}])
        answer_callback(callback_id, "Выбор товара")
        send_message(user_id=sender_id, text=f"\U0001F4CB Товары в категории «{categories[cat_index]['name']}»:", keyboard=buttons)
        return jsonify({"ok": True}), 200

    # --- admin: edit item — item selected ---
    if payload.startswith("admin_edit_item_select:") and is_admin(sender_id):
        parts = payload.split(":")
        cat_index = int(parts[1])
        item_index = int(parts[2])
        categories = CATALOG_DATA.get("categories", [])
        if cat_index >= len(categories):
            answer_callback(callback_id, "Категория не найдена")
            return jsonify({"ok": True}), 200
        items = categories[cat_index].get("items", [])
        if item_index >= len(items):
            answer_callback(callback_id, "Товар не найден")
            return jsonify({"ok": True}), 200
        item = items[item_index]
        admin_sessions[sender_id] = {
            "action": "edit_item",
            "step": "select_field",
            "cat_index": cat_index,
            "item_index": item_index,
        }
        answer_callback(callback_id, "Редактирование товара")
        photo_info = "есть" if item.get("photo_url") else "нет"
        text = (
            f"\u270f\ufe0f Редактирование товара\n"
            f"\U0001FA91 {item['name']}\n"
            f"\U0001F4B0 Цена: {item['price']} руб.\n"
            f"\U0001F4DD {item.get('description', '')}\n"
            f"\U0001F4F7 Фото: {photo_info}\n"
            f"\U0001F194 id: {item['id']}\n\n"
            f"Что изменить?"
        )
        keyboard = [
            [
                {"type": "callback", "text": "\U0001FA91 Название", "payload": "admin_edit_field:name"},
                {"type": "callback", "text": "\U0001F4B0 Цену", "payload": "admin_edit_field:price"},
            ],
            [
                {"type": "callback", "text": "\U0001F4DD Описание", "payload": "admin_edit_field:description"},
                {"type": "callback", "text": "\U0001F4F7 Фото", "payload": "admin_edit_field:photo_url"},
            ],
            [
                {"type": "callback", "text": "\U0001F4CB Удалить товар", "payload": f"admin_delete_item_confirm:{cat_index}:{item_index}"},
                {"type": "callback", "text": "\u2B05\ufe0f Назад", "payload": "admin_back_to_menu"},
            ],
        ]
        send_message(user_id=sender_id, text=text, keyboard=keyboard)
        return jsonify({"ok": True}), 200

    # --- admin: edit field ---
    if payload.startswith("admin_edit_field:") and is_admin(sender_id):
        field = payload.split(":", 1)[1]
        session = admin_sessions.get(sender_id)
        if not session or session.get("action") != "edit_item":
            answer_callback(callback_id, "Сессия истекла")
            send_main_menu(sender_id)
            return jsonify({"ok": True}), 200
        field_names = {
            "name": "название",
            "price": "цену (только число, в рублях)",
            "description": "описание",
            "photo_url": "ссылку на фото (или «нет» чтобы убрать)",
        }
        cat_index = session["cat_index"]
        item_index = session["item_index"]
        item = CATALOG_DATA["categories"][cat_index]["items"][item_index]
        current_val = item.get(field, "")
        if field == "price":
            current_val = f"{current_val} руб."
        session["step"] = "edit_field_value"
        session["edit_field"] = field
        answer_callback(callback_id, f"Изменение: {field_names.get(field, field)}")
        send_message(user_id=sender_id, text=f"\u270f\ufe0f Изменение: {field_names.get(field, field)}\nТекущее значение: {current_val}\n\n\U0001F4DD Напишите новое значение:", keyboard=build_cancel_keyboard())
        return jsonify({"ok": True}), 200

    # --- admin: delete category ---
    if payload == "admin_delete_category" and is_admin(sender_id):
        categories = CATALOG_DATA.get("categories", [])
        if not categories:
            answer_callback(callback_id, "Нет категорий")
            send_message(user_id=sender_id, text="\u26a0\ufe0f Нет категорий для удаления.")
            return jsonify({"ok": True}), 200
        buttons = []
        for i, cat in enumerate(categories):
            item_count = len(cat.get("items", []))
            buttons.append([
                {"type": "callback", "text": f"\U0001F4E6 {cat['name']} ({item_count} тов.)", "payload": f"admin_delete_cat_confirm:{i}"}
            ])
        buttons.append([{"type": "callback", "text": "\u2B05\ufe0f Назад", "payload": "admin_back_to_menu"}])
        answer_callback(callback_id, "Удаление категории")
        send_message(user_id=sender_id, text="\u26a0\ufe0f Выберите категорию для удаления (вместе с товарами):", keyboard=buttons)
        return jsonify({"ok": True}), 200

    # --- admin: delete category — confirm ---
    if payload.startswith("admin_delete_cat_confirm:") and is_admin(sender_id):
        cat_index = int(payload.split(":", 1)[1])
        categories = CATALOG_DATA.get("categories", [])
        if cat_index >= len(categories):
            answer_callback(callback_id, "Категория не найдена")
            return jsonify({"ok": True}), 200
        cat_name = categories[cat_index]["name"]
        item_count = len(categories[cat_index].get("items", []))
        answer_callback(callback_id, "Подтверждение")
        send_message(user_id=sender_id, text=f"\u26a0\ufe0f Удалить категорию «{cat_name}»?\nБудут удалены {item_count} товаров!\n\nЭто действие необратимо!", keyboard=[
            [
                {"type": "callback", "text": "\u2705 Да, удалить", "payload": f"admin_delete_cat_yes:{cat_index}"},
                {"type": "callback", "text": "\u274C Отмена", "payload": "cancel_action"},
            ]
        ])
        return jsonify({"ok": True}), 200

    # --- admin: delete category — yes ---
    if payload.startswith("admin_delete_cat_yes:") and is_admin(sender_id):
        cat_index = int(payload.split(":", 1)[1])
        categories = CATALOG_DATA.get("categories", [])
        if cat_index < len(categories):
            cat_name = categories[cat_index]["name"]
            del CATALOG_DATA["categories"][cat_index]
            save_catalog()
            rebuild_catalog_index()
            answer_callback(callback_id, "Удалено")
            send_message(user_id=sender_id, text=f"\u2705 Категория «{cat_name}» удалена!\n\n\U0001F447Что дальше?\U0001F447", keyboard=build_admin_edit_keyboard())
        else:
            answer_callback(callback_id, "Категория не найдена")
        return jsonify({"ok": True}), 200

    # --- admin: delete item — confirm ---
    if payload.startswith("admin_delete_item_confirm:") and is_admin(sender_id):
        parts = payload.split(":")
        cat_index = int(parts[1])
        item_index = int(parts[2])
        categories = CATALOG_DATA.get("categories", [])
        if cat_index >= len(categories):
            answer_callback(callback_id, "Категория не найдена")
            return jsonify({"ok": True}), 200
        items = categories[cat_index].get("items", [])
        if item_index >= len(items):
            answer_callback(callback_id, "Товар не найден")
            return jsonify({"ok": True}), 200
        item_name = items[item_index]["name"]
        answer_callback(callback_id, "Подтверждение")
        send_message(user_id=sender_id, text=f"\u26a0\ufe0f Удалить товар «{item_name}»?\n\nЭто действие необратимо!", keyboard=[
            [
                {"type": "callback", "text": "\u2705 Да, удалить", "payload": f"admin_delete_item_yes:{cat_index}:{item_index}"},
                {"type": "callback", "text": "\u274C Отмена", "payload": "cancel_action"},
            ]
        ])
        return jsonify({"ok": True}), 200

    # --- admin: delete item — yes ---
    if payload.startswith("admin_delete_item_yes:") and is_admin(sender_id):
        parts = payload.split(":")
        cat_index = int(parts[1])
        item_index = int(parts[2])
        categories = CATALOG_DATA.get("categories", [])
        if cat_index < len(categories):
            items = categories[cat_index].get("items", [])
            if item_index < len(items):
                item_name = items[item_index]["name"]
                del items[item_index]
                save_catalog()
                rebuild_catalog_index()
                answer_callback(callback_id, "Удалено")
                send_message(user_id=sender_id, text=f"\u2705 Товар «{item_name}» удалён!\n\n\U0001F447Что дальше?\U0001F447", keyboard=build_admin_edit_keyboard())
                return jsonify({"ok": True}), 200
        answer_callback(callback_id, "Товар не найден")
        return jsonify({"ok": True}), 200

    # --- reply:<post_id>:<comment_mid> ---
    if payload.startswith("reply:"):
        parts = payload.split(":", 2)
        if len(parts) == 3:
            post_id, comment_mid = parts[1], parts[2]
            with _lock:
                pending_replies[sender_id] = {
                    "post_id": post_id,
                    "comment_mid": comment_mid,
                    "timestamp": time.time(),
                }
                save_state()
            answer_callback(callback_id, "\u270D\uFE0F Напишите ответ — бот отправит его как комментарий")
            send_message(user_id=sender_id, text="\u270D\uFE0F Напишите ответ следующим сообщением — бот отправит его как комментарий-ответ.", keyboard=build_cancel_keyboard())
        else:
            answer_callback(callback_id, "Ошибка: неверный формат")
        return jsonify({"ok": True}), 200

    # --- show_category:<index> ---
    if payload.startswith("show_category:"):
        cat_index = int(payload.split(":", 1)[1])
        categories = CATALOG_DATA.get("categories", [])
        if cat_index < len(categories):
            category = categories[cat_index]
            answer_callback(callback_id, f"Открываю: {category['name']}")
            items = category.get("items", [])
            for i, item in enumerate(items):
                send_product_card(sender_id, item)
                if i < len(items) - 1:
                    time.sleep(0.3)
        else:
            answer_callback(callback_id, "Категория не найдена")
        return jsonify({"ok": True}), 200

    # --- add_to_cart:<item_id> ---
    if payload.startswith("add_to_cart:"):
        item_id = payload.split(":", 1)[1]
        item = find_item_by_id(item_id)
        if not item:
            answer_callback(callback_id, "Товар не найден")
            return jsonify({"ok": True}), 200
        with _lock:
            if str(sender_id) not in user_carts:
                user_carts[str(sender_id)] = []
            user_carts[str(sender_id)].append(item_id)
            save_state()
        answer_callback(callback_id, "\u2705 Добавлено в корзину!")
        count = len(user_carts[str(sender_id)])
        send_message(user_id=sender_id, text=f"\U0001F6D2 \u00ab{item['name']}\u00bb добавлен в корзину.\nВ корзине товаров: {count}\n\n\U0001F449 Нажмите \u00ab\U0001F6D2 Корзина\u00bb, чтобы оформить заказ.", keyboard=build_cart_catalog_keyboard())
        return jsonify({"ok": True}), 200

    # --- quick_order:<item_id> ---
    if payload.startswith("quick_order:"):
        item_id = payload.split(":", 1)[1]
        item = find_item_by_id(item_id)
        if not item:
            answer_callback(callback_id, "Товар не найден")
            return jsonify({"ok": True}), 200
        answer_callback(callback_id, "Принято!")
        send_message(user_id=sender_id, text=f"\U0001F4D8 Быстрый заказ: \"{item['name']}\" (Цена: {item['price']} руб.)\n\nЧтобы мастер связался с вами \U0001F4A1\n\U0001F4DD Напишите, как вас зовут и номер вашего телефона (в любом формате)", keyboard=build_cancel_keyboard())
        with _lock:
            pending_replies[sender_id] = {
                "step": "waiting_contact_quick",
                "item": item,
                "item_id": item_id,
                "timestamp": time.time(),
            }
            save_state()
        return jsonify({"ok": True}), 200

    # --- ask_question:<item_id> ---
    if payload.startswith("ask_question:"):
        item_id = payload.split(":", 1)[1]
        item = find_item_by_id(item_id)
        item_name = item["name"] if item else "изделие"
        answer_callback(callback_id, "Напишите вопрос")
        send_message(user_id=sender_id, text=f"\U0001F4AC Напишите ваш вопрос про \"{item_name}\"\n\nМастер увидит его сразу и ответит в течение 30 минут.", keyboard=build_cancel_keyboard())
        with _lock:
            pending_replies[sender_id] = {
                "step": "waiting_question",
                "item_id": item_id,
                "item_name": item_name,
                "timestamp": time.time(),
            }
            save_state()
        return jsonify({"ok": True}), 200

    # --- start_checkout ---
    if payload == "start_checkout":
        cart = user_carts.get(str(sender_id), [])
        if not cart:
            answer_callback(callback_id, "Корзина пуста")
            return jsonify({"ok": True}), 200
        total = 0
        items_text = ""
        for item_id in cart:
            item = find_item_by_id(item_id)
            if item:
                items_text += f"\u2022 \"{item['name']}\" — {item['price']} руб.\n"
                total += item["price"]
        answer_callback(callback_id, "Начинаем оформление")
        send_message(user_id=sender_id, text=f"\U0001F6D2 Оформляем заказ:\n\n{items_text}\U0001F4B0 Итого: {total} руб.\n\n\U0001F4DD Напишите, как вас зовут и номер вашего телефона (в любом формате)", keyboard=build_cancel_keyboard())
        with _lock:
            pending_replies[sender_id] = {
                "step": "waiting_contact",
                "cart": cart,
                "total": total,
                "timestamp": time.time(),
            }
            save_state()
        return jsonify({"ok": True}), 200

    # --- clear_cart ---
    if payload == "clear_cart":
        with _lock:
            user_carts[str(sender_id)] = []
            save_state()
        answer_callback(callback_id, "Корзина очищена")
        send_message(user_id=sender_id, text="\U0001F5D1 Корзина очищена.\n\n\U0001F449 Откройте \u00ab\U0001F4CB Каталог\u00bb — выберите изделие!", keyboard=build_catalog_keyboard())
        return jsonify({"ok": True}), 200

    answer_callback(callback_id, "Ок")
    return jsonify({"ok": True}), 200


# === ОБРАБОТКА СООБЩЕНИЙ ===

def handle_admin_reply(sender_id, text):
    match = re.match(r"^#?(\d+)\s*[:.\u3001\s]\s*(.+)", text, re.DOTALL)
    if match:
        num = int(match.group(1))
        reply_text = match.group(2).strip()
        if num in active_dialogs:
            client_user_id = active_dialogs[num]["user_id"]
            send_message(user_id=client_user_id, text=reply_text, keyboard=build_main_menu_keyboard())
            send_message(user_id=sender_id, text=f"\u2705 Ответ #{num} отправлен клиенту.")
            logger.info(f"Ответ #{num} отправлен user_id={client_user_id}")
            with _lock:
                del active_dialogs[num]
                save_state()
        else:
            active_nums = list(active_dialogs.keys())
            send_message(user_id=sender_id, text=f"\u26A0\uFE0F Диалог #{num} не найден. Активные: {active_nums}")
        return True
    return False


def handle_admin_session(sender_id, text):
    """Обработка сессий админ-режима (добавление/редактирование). Возвращает True если обработано."""
    session = admin_sessions.get(sender_id)
    if not session:
        return False

    action = session.get("action")
    step = session.get("step")
    text = (text or "").strip()

    if not text:
        send_message(user_id=sender_id, text="Пожалуйста, напишите ответ или нажмите «Отмена».", keyboard=build_cancel_keyboard())
        return True

    # --- Добавление категории ---
    if action == "add_category" and step == "category_name":
        # Проверка на дубликат
        for cat in CATALOG_DATA.get("categories", []):
            if cat["name"].lower() == text.lower():
                send_message(user_id=sender_id, text=f"\u26a0\ufe0f Категория «{text}» уже существует!\n\nНапишите другое название:", keyboard=build_cancel_keyboard())
                return True
        CATALOG_DATA.setdefault("categories", []).append({"name": text, "items": []})
        save_catalog()
        rebuild_catalog_index()
        admin_sessions.pop(sender_id, None)
        send_message(user_id=sender_id, text=f"\u2705 Категория «{text}» добавлена!\n\nТеперь можно добавить в неё товары.", keyboard=build_admin_add_keyboard())
        return True

    # --- Добавление товара ---
    if action == "add_item":
        if step == "item_name":
            session["item_name"] = text
            session["step"] = "item_price"
            send_message(user_id=sender_id, text="\U0001F4B0 Напишите цену в рублях (только число):", keyboard=build_cancel_keyboard())
            return True
        if step == "item_price":
            try:
                price = int(re.sub(r'[^\d]', '', text))
                if price <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                send_message(user_id=sender_id, text="\u26a0\ufe0f Неверная цена. Напишите число, например: 7900", keyboard=build_cancel_keyboard())
                return True
            session["item_price"] = price
            session["step"] = "item_description"
            send_message(user_id=sender_id, text="\U0001F4DD Напишите краткое описание (2–3 строки):", keyboard=build_cancel_keyboard())
            return True
        if step == "item_description":
            session["item_description"] = text
            session["step"] = "item_photo"
            send_message(user_id=sender_id, text="\U0001F4F7 Отправьте ссылку на фото. Если фото нет — напишите «нет»:", keyboard=build_cancel_keyboard())
            return True
        if step == "item_photo":
            photo_url = ""
            if text.lower() != "нет" and text.lower() != "no":
                photo_url = text
            cat_index = session["cat_index"]
            item_name = session["item_name"]
            price = session["item_price"]
            description = session["item_description"]
            base_id = slugify(item_name)
            item_id = generate_unique_id(base_id)
            new_item = {
                "id": item_id,
                "name": item_name,
                "price": price,
                "description": description,
                "photo_url": photo_url,
            }
            CATALOG_DATA["categories"][cat_index].setdefault("items", []).append(new_item)
            save_catalog()
            rebuild_catalog_index()
            admin_sessions.pop(sender_id, None)
            send_message(user_id=sender_id, text=f"\u2705 Товар «{item_name}» добавлен!\n\U0001F194 id: {item_id}\n\n\U0001F447Что дальше?\U0001F447", keyboard=build_admin_add_keyboard())
            return True

    # --- Редактирование категории ---
    if action == "edit_category" and step == "new_name":
        cat_index = session["cat_index"]
        old_name = CATALOG_DATA["categories"][cat_index]["name"]
        CATALOG_DATA["categories"][cat_index]["name"] = text
        save_catalog()
        rebuild_catalog_index()
        admin_sessions.pop(sender_id, None)
        send_message(user_id=sender_id, text=f"\u2705 Категория переименована!\nБыло: {old_name}\nСтало: {text}\n\n\U0001F447Что дальше?\U0001F447", keyboard=build_admin_edit_keyboard())
        return True

    # --- Редактирование товара ---
    if action == "edit_item" and step == "edit_field_value":
        field = session["edit_field"]
        cat_index = session["cat_index"]
        item_index = session["item_index"]
        item = CATALOG_DATA["categories"][cat_index]["items"][item_index]
        old_val = item.get(field, "")
        if field == "price":
            try:
                new_val = int(re.sub(r'[^\d]', '', text))
                if new_val <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                send_message(user_id=sender_id, text="\u26a0\ufe0f Неверная цена. Напишите число, например: 7900", keyboard=build_cancel_keyboard())
                return True
        elif field == "photo_url":
            new_val = "" if text.lower() in ("нет", "no") else text
        else:
            new_val = text
        item[field] = new_val
        save_catalog()
        rebuild_catalog_index()
        field_names = {"name": "Название", "price": "Цена", "description": "Описание", "photo_url": "Фото"}
        field_label = field_names.get(field, field)
        if field == "price":
            old_display = f"{old_val} руб."
            new_display = f"{new_val} руб."
        elif field == "photo_url":
            old_display = "есть" if old_val else "нет"
            new_display = "есть" if new_val else "нет"
        else:
            old_display = old_val
            new_display = new_val
        admin_sessions.pop(sender_id, None)
        send_message(user_id=sender_id, text=f"\u2705 {field_label} изменён!\nБыло: {old_display}\nСтало: {new_display}\n\n\U0001F447Что дальше?\U0001F447", keyboard=build_admin_edit_keyboard())
        return True

    return False


def handle_pending_state(sender_id, text):
    state = pending_replies.get(sender_id)
    if not state or not isinstance(state, dict):
        return False

    step = state.get("step")
    if not step:
        return False

    if "timestamp" in state and time.time() - state["timestamp"] > SESSION_TIMEOUT:
        with _lock:
            del pending_replies[sender_id]
            save_state()
        send_message(user_id=sender_id, text="\u23F1\uFE0F Время ожидания истекло. Начните заново.", keyboard=build_main_menu_keyboard())
        return True

    if step == "waiting_question":
        question_text = (text or "").strip()
        if not question_text:
            send_message(user_id=sender_id, text="Пожалуйста, напишите ваш вопрос:", keyboard=build_cancel_keyboard())
            return True
        with _lock:
            pending_replies.pop(sender_id, None)
            save_state()
        global question_counter
        question_counter += 1
        num = question_counter
        item_name = state.get("item_name")
        with _lock:
            active_dialogs[num] = {
                "user_id": sender_id,
                "name": state.get("first_name", "Пользователь"),
                "text": question_text,
                "item_name": item_name,
            }
            save_state()
        if item_name:
            forward_text = (
                f"#{num} \U0001F4AC Вопрос от {state.get('first_name', 'Пользователь')}\n"
                f"\U0001F4E6 Изделие: {item_name}\n\n"
                f"{question_text}\n\n"
                f"\u21AA\uFE0F Чтобы ответить, напишите: {num}: ваш текст"
            )
        else:
            forward_text = (
                f"#{num} \U0001F4AC Вопрос от {state.get('first_name', 'Пользователь')}\n\n"
                f"{question_text}\n\n"
                f"\u21AA\uFE0F Чтобы ответить, напишите: {num}: ваш текст"
            )
        send_message(user_id=NOTIFY_CHAT_ID, text=forward_text)
        send_message(user_id=sender_id, text="\u2705 Спасибо, вопрос передан мастеру!\n\n\u23F1\uFE0F Ответим в течение 30 минут.", keyboard=build_main_menu_keyboard())
        logger.info(f"Вопрос #{num} от user_id={sender_id}: {question_text}")
        return True

    if step == "waiting_contact":
        contact_text = (text or "").strip()
        valid, msg = validate_contact(contact_text)
        if not valid:
            send_message(user_id=sender_id, text=msg, keyboard=build_cancel_keyboard())
            return True
        with _lock:
            pending_replies.pop(sender_id, None)
            save_state()
        cart = state.get("cart", [])
        total = state.get("total", 0)
        order_text = f"\U0001F4D8 Новый заказ!\n\nИмя и телефон: {contact_text}\nТовары:\n"
        for item_id in cart:
            item = find_item_by_id(item_id)
            if item:
                order_text += f"\u2022 \"{item['name']}\" — {item['price']} руб.\n"
        order_text += f"\n\U0001F4B0 Итого: {total} руб."
        send_message(user_id=NOTIFY_CHAT_ID, text=order_text)
        send_message(user_id=sender_id, text="\u2705 Спасибо за заказ!\n\nМастер свяжется с вами в ближайшее время.\n\n\U0001F449 Если нужно что-то изменить — нажмите \u00ab\U0001F6D2 Корзина\u00bb", keyboard=build_main_menu_keyboard())
        with _lock:
            user_carts[str(sender_id)] = []
            save_state()
        return True

    if step == "waiting_contact_quick":
        contact_text = (text or "").strip()
        valid, msg = validate_contact(contact_text)
        if not valid:
            send_message(user_id=sender_id, text=msg, keyboard=build_cancel_keyboard())
            return True
        with _lock:
            pending_replies.pop(sender_id, None)
            save_state()
        item = state.get("item")
        order_text = f"\U0001F4D8 Быстрый заказ!\n\nТовар: \"{item['name']}\"\nЦена: {item['price']} руб.\nИмя и телефон: {contact_text}"
        send_message(user_id=NOTIFY_CHAT_ID, text=order_text)
        send_message(user_id=sender_id, text="\u2705 Спасибо! Мастер свяжется с вами в ближайшее время.\n\n\U0001F449 Если нужно что-то изменить — откройте \u00ab\U0001F4CB Каталог\u00bb", keyboard=build_main_menu_keyboard())
        return True

    # Совместимость со старыми шагами
    if step == "waiting_name":
        name = (text or "").strip()
        if not name:
            send_message(user_id=sender_id, text="Пожалуйста, напишите имя:", keyboard=build_cancel_keyboard())
            return True
        with _lock:
            pending_replies[sender_id]["name"] = name
            pending_replies[sender_id]["step"] = "waiting_phone"
            pending_replies[sender_id]["timestamp"] = time.time()
            save_state()
        send_message(user_id=sender_id, text=f"{name}, спасибо! \U0001F4DE Напишите ваш номер телефона (в любом формате)", keyboard=build_cancel_keyboard())
        return True

    if step == "waiting_phone":
        phone = (text or "").strip()
        if not phone:
            send_message(user_id=sender_id, text="Пожалуйста, напишите номер телефона:", keyboard=build_cancel_keyboard())
            return True
        with _lock:
            pending_replies.pop(sender_id, None)
            save_state()
        cart = state.get("cart", [])
        total = state.get("total", 0)
        name = state.get("name", "Не указано")
        order_text = f"\U0001F4D8 Новый заказ!\nИмя: {name}\nТелефон: {phone}\nТовары:\n"
        for item_id in cart:
            item = find_item_by_id(item_id)
            if item:
                order_text += f"\u2022 \"{item['name']}\" — {item['price']} руб.\n"
        order_text += f"\n\U0001F4B0 Итого: {total} руб."
        send_message(user_id=NOTIFY_CHAT_ID, text=order_text)
        send_message(user_id=sender_id, text="\u2705 Спасибо за заказ!\n\nМастер свяжется с вами в ближайшее время.\n\n\U0001F449 Если нужно что-то изменить — нажмите \u00ab\U0001F6D2 Корзина\u00bb", keyboard=build_main_menu_keyboard())
        with _lock:
            user_carts[str(sender_id)] = []
            save_state()
        return True

    if step == "waiting_phone_quick":
        phone = (text or "").strip()
        if not phone:
            send_message(user_id=sender_id, text="Пожалуйста, напишите номер телефона:", keyboard=build_cancel_keyboard())
            return True
        with _lock:
            pending_replies.pop(sender_id, None)
            save_state()
        item = state.get("item")
        order_text = f"\U0001F4D8 Быстрый заказ!\nТовар: \"{item['name']}\"\nЦена: {item['price']} руб.\nТелефон: {phone}"
        send_message(user_id=NOTIFY_CHAT_ID, text=order_text)
        send_message(user_id=sender_id, text="\u2705 Спасибо! Мастер свяжется с вами в ближайшее время.\n\n\U0001F449 Если нужно что-то изменить — откройте \u00ab\U0001F4CB Каталог\u00bb", keyboard=build_main_menu_keyboard())
        return True

    return False


def handle_pending_reply_comment(sender_id, text):
    state = pending_replies.get(sender_id)
    if isinstance(state, dict) and "post_id" in state:
        with _lock:
            reply_data = pending_replies.pop(sender_id, None)
            save_state()
        if not reply_data:
            return True
        post_id = reply_data["post_id"]
        comment_mid = reply_data["comment_mid"]
        success = post_comment(post_id, text, reply_to_mid=comment_mid)
        if success:
            send_message(user_id=sender_id, text="\u2705 Ответ отправлен в канал!", keyboard=build_main_menu_keyboard())
        else:
            send_message(user_id=sender_id, text="\u274C Не удалось отправить ответ. Проверьте, что бот — администратор канала с правом write.", keyboard=build_main_menu_keyboard())
            with _lock:
                pending_replies[sender_id] = reply_data
                save_state()
        return True
    return False


def handle_message_created(data):
    global question_counter
    message = data.get("message", {})
    sender_id = str(message.get("sender", {}).get("user_id", ""))
    text = message.get("body", {}).get("text", "")
    first_name = message.get("sender", {}).get("first_name", "Пользователь")
    logger.info(f"Сообщение от user_id={sender_id}: {text}")
    cmd = text.lower().strip() if text else ""

    # Перехват ответов админа (формат N: текст)
    if is_admin(sender_id) and text and not text.startswith("/"):
        if handle_admin_reply(sender_id, text):
            return

    # Команда /cancel — отменить текущее действие
    if cmd in ["/cancel", "/отмена"]:
        admin_sessions.pop(sender_id, None)
        with _lock:
            had = pending_replies.pop(sender_id, None)
            if had:
                save_state()
        if had:
            send_message(user_id=sender_id, text="\u274C Действие отменено.\n\n\U0001F447Выберите действие\U0001F447", keyboard=build_main_menu_keyboard())
        else:
            send_message(user_id=sender_id, text="Нечего отменять.\n\n\U0001F447Выберите действие\U0001F447", keyboard=build_main_menu_keyboard())
        return

    # Кнопки главного меню — очищаем pending state и admin sessions
    if text and text.strip() == "\U0001F4CB Каталог":
        admin_sessions.pop(sender_id, None)
        with _lock:
            if sender_id in pending_replies:
                del pending_replies[sender_id]
                save_state()
        show_catalog(sender_id)
        return
    if text and text.strip() == "\U0001F6D2 Корзина":
        admin_sessions.pop(sender_id, None)
        with _lock:
            if sender_id in pending_replies:
                del pending_replies[sender_id]
                save_state()
        show_cart(sender_id)
        return
    if text and text.strip() == "\U0001F4DE Мастер":
        admin_sessions.pop(sender_id, None)
        with _lock:
            if sender_id in pending_replies:
                del pending_replies[sender_id]
                save_state()
        send_message(user_id=sender_id, text="\U0001F4DE Мастер:\n\nЕвгений\n\u260E\uFE0F 8 (989) 622-37-32\n\n\U0001F449 Или закажите через \u00ab\U0001F4CB Каталог\u00bb", keyboard=build_catalog_keyboard())
        return
    if text and text.strip() == "\u2753 Задать вопрос":
        admin_sessions.pop(sender_id, None)
        with _lock:
            if sender_id in pending_replies:
                del pending_replies[sender_id]
                save_state()
        with _lock:
            pending_replies[sender_id] = {"step": "waiting_question", "first_name": first_name, "timestamp": time.time()}
            save_state()
        send_message(user_id=sender_id, text="\U0001F4AC Напишите ваш вопрос прямо здесь.\n\nМастер увидит его сразу и ответит в течение 30 минут.", keyboard=build_cancel_keyboard())
        return

    # Текстовые команды
    if cmd in ["/catalog", "/каталог"]:
        show_catalog(sender_id)
        return
    if cmd in ["/cart", "/корзина"]:
        show_cart(sender_id)
        return
    if cmd in ["/help", "/помощь"]:
        send_message(user_id=sender_id, text="\U0001FAB5 Мастерская Игнатьевых — помощь\n\n\U0001F4CB /каталог — открыть каталог\n\U0001F6D2 /корзина — посмотреть корзину\n\u2753 /помощь — эта справка\n\U0001F4A1 /cancel — отменить текущее действие\n\nТакже можно нажимать кнопки под сообщениями бота.", keyboard=build_main_menu_keyboard())
        return

    # --- Админ-команды ---
    if cmd == "/добавить" and is_admin(sender_id):
        admin_sessions.pop(sender_id, None)
        with _lock:
            if sender_id in pending_replies:
                del pending_replies[sender_id]
                save_state()
        send_message(user_id=sender_id, text="\U0001F527 Режим добавления в каталог\n\nЧто добавляем?", keyboard=build_admin_add_keyboard())
        return

    if cmd == "/редактировать" and is_admin(sender_id):
        admin_sessions.pop(sender_id, None)
        with _lock:
            if sender_id in pending_replies:
                del pending_replies[sender_id]
                save_state()
        send_message(user_id=sender_id, text="\u270f\ufe0f Редактирование каталога\n\nЧто редактируем?", keyboard=build_admin_edit_keyboard())
        return

    if cmd == "/каталог_админ" and is_admin(sender_id):
        categories = CATALOG_DATA.get("categories", [])
        if not categories:
            send_message(user_id=sender_id, text="Каталог пуст.")
            return
        lines = []
        for i, cat in enumerate(categories):
            lines.append(f"\U0001F4E6 {cat['name']} (index={i})")
            for item in cat.get("items", []):
                lines.append(f"  \U0001FA91 {item['name']} — {item['price']} руб. (id={item['id']})")
        send_message(user_id=sender_id, text="\U0001F4CB Каталог (админ-режим):\n\n" + "\n".join(lines))
        return

    # Команда /вопросы — только для админа
    if cmd == "/вопросы" and is_admin(sender_id):
        if active_dialogs:
            lines = []
            for num, d in sorted(active_dialogs.items()):
                item_info = f" (\U0001F4E6 {d['item_name']})" if d.get("item_name") else ""
                preview = d["text"][:60] + ("..." if len(d["text"]) > 60 else "")
                lines.append(f"#{num} — {d['name']}{item_info}: {preview}")
            send_message(user_id=sender_id, text="\U0001F4CB Активные диалоги:\n\n" + "\n".join(lines))
        else:
            send_message(user_id=sender_id, text="Нет активных диалогов.")
        return

    # Обработка админ-сессий (добавление/редактирование)
    if sender_id in admin_sessions:
        if handle_admin_session(sender_id, text):
            return

    # Обработка шагов (вопрос, заказ, быстрый заказ)
    if sender_id in pending_replies:
        state = pending_replies.get(sender_id)
        if isinstance(state, dict) and "first_name" not in state and "step" in state:
            state["first_name"] = first_name
        if handle_pending_state(sender_id, text):
            return

        if text and not text.startswith("/"):
            if handle_pending_reply_comment(sender_id, text):
                return

    # /start
    if cmd and cmd.startswith("/start"):
        send_welcome(sender_id)
        return

    # Fallback — показываем главное меню
    send_message(user_id=sender_id, text="Я не совсем понял сообщение.\n\n\U0001F447Выберите действие\U0001F447", keyboard=build_main_menu_keyboard())


# === WEBHOOK ===

@app.route("/webhook", methods=["POST", "GET"])
def webhook():
    if request.method == "GET":
        return jsonify({"status": "ok"}), 200

    if WEBHOOK_SECRET:
        received_secret = request.headers.get("X-Max-Bot-Api-Secret", "")
        if not hmac.compare_digest(received_secret, WEBHOOK_SECRET):
            logger.warning("Неверный секрет webhook")
            return jsonify({"error": "forbidden"}), 403

    data = request.get_json()
    if not data:
        return jsonify({"error": "bad request"}), 400

    update_type = data.get("update_type", "")
    logger.info(f"Получено событие: {update_type}")

    cleanup_stale_sessions()

    if update_type == "bot_started":
        chat_id = data.get("chat_id")
        sender_id = data.get("user", {}).get("user_id")
        logger.info(f"bot_started: chat_id={chat_id}, user_id={sender_id}")
        if chat_id:
            send_welcome(chat_id, is_chat=True)
        elif sender_id:
            send_welcome(sender_id)
        return jsonify({"ok": True}), 200

    if update_type == "message_callback":
        return handle_callback(data)

    message = data.get("message", {})
    recipient = message.get("recipient", {})
    chat_id = recipient.get("chat_id", "")
    post_id = recipient.get("post_id", "")
    comment_mid = message.get("body", {}).get("mid", "")
    author_name = get_author_name(message)

    if update_type == "comment_created":
        comment_text = message.get("body", {}).get("text", "")
        notification = f"\U0001F195 Новый комментарий\nАвтор: {author_name}\n\n{comment_text}"
        post_link = build_post_link(chat_id, post_id)
        keyboard = build_post_keyboard(post_link, post_id, comment_mid)
        send_message(user_id=NOTIFY_CHAT_ID, text=notification, attachments=keyboard)
        return jsonify({"ok": True}), 200

    if update_type == "comment_edited":
        comment_text = message.get("body", {}).get("text", "")
        notification = f"\u270F\uFE0F Изменён комментарий\nАвтор: {author_name}\n\n{comment_text}"
        post_link = build_post_link(chat_id, post_id)
        keyboard = build_post_keyboard(post_link, post_id, comment_mid)
        send_message(user_id=NOTIFY_CHAT_ID, text=notification, attachments=keyboard)
        return jsonify({"ok": True}), 200

    if update_type == "comment_removed":
        notification = f"\U0001F5D1\uFE0F Удалён комментарий\nАвтор: {author_name}"
        post_link = build_post_link(chat_id, post_id)
        keyboard = build_post_keyboard(post_link)
        send_message(user_id=NOTIFY_CHAT_ID, text=notification, attachments=keyboard)
        return jsonify({"ok": True}), 200

    if update_type == "message_created":
        try:
            handle_message_created(data)
        except Exception as e:
            logger.error(f"Ошибка обработки message_created: {e}", exc_info=True)
        return jsonify({"ok": True}), 200

    return jsonify({"ok": True}), 200


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "status": "running",
        "catalog_items": len(CATALOG_INDEX),
        "active_dialogs": len(active_dialogs),
        "pending_replies": len(pending_replies),
        "carts": len(user_carts),
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "token_configured": bool(TOKEN),
        "webhook_url_configured": bool(WEBHOOK_URL),
        "notify_chat_configured": bool(NOTIFY_CHAT_ID),
        "catalog_items": len(CATALOG_INDEX),
    }), 200


# === ИНИЦИАЛИЗАЦИЯ ===

load_state()
register_commands()
update_webhook_subscription()


# === GRACEFUL SHUTDOWN ===

def on_shutdown(signum, frame):
    logger.info(f"Получен сигнал {signum}, сохраняю состояние...")
    save_state()
    logger.info("Состояние сохранено. Завершаю работу.")
    os._exit(0)


signal.signal(signal.SIGTERM, on_shutdown)
signal.signal(signal.SIGINT, on_shutdown)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
