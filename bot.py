import os
import hmac
import base64
import json
import re
import time
import signal
import threading
import logging
import shutil
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

REQUIRED_ENV = ["MAX_BOT_TOKEN", "WEBHOOK_URL", "NOTIFY_CHAT_ID"]
_missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
if _missing:
    logger.warning(f"Не заданы переменные окружения: {', '.join(_missing)}.")

# === АДМИНЫ ===
ADMIN_IDS = set()
if NOTIFY_CHAT_ID:
    ADMIN_IDS.add(str(NOTIFY_CHAT_ID))
ADMIN_IDS.add("39193669")  # Евгений


def is_admin(user_id):
    return str(user_id) in ADMIN_IDS


# === СОСТОЯНИЕ ===
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


def save_state():
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
    if stale:
        save_state()


# === КАТАЛОГ ===

CATALOG_FILE = os.path.join(os.path.dirname(__file__), "catalog.json")


def load_catalog():
    """Только ЧТЕНИЕ catalog.json. НИКОГДА не перезаписывает файл."""
    try:
        with open(CATALOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info(f"Каталог загружен: {len(data.get('categories', []))} категорий.")
        return data
    except FileNotFoundError:
        logger.warning("catalog.json не найден — каталог пуст.")
        return {"categories": []}
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка в catalog.json: {e}")
        return {"categories": []}


def save_catalog():
    """Сохраняет каталог в catalog.json.
    Перед перезаписью создаёт резервную копию catalog_backup.json.
    ВАЖНО: эта функция вызывается ТОЛЬКО при явных действиях админа
    (добавление, редактирование, удаление). Никогда — при запуске бота."""
    try:
        # Резервная копия перед перезаписью
        if os.path.exists(CATALOG_FILE):
            backup_path = os.path.join(os.path.dirname(__file__), "catalog_backup.json")
            shutil.copy2(CATALOG_FILE, backup_path)
            logger.info("Создана резервная копия catalog_backup.json")

        with open(CATALOG_FILE, "w", encoding="utf-8") as f:
            json.dump(CATALOG_DATA, f, ensure_ascii=False, indent=2)
        logger.info(f"Каталог сохранён: {len(CATALOG_DATA.get('categories', []))} категорий.")
        return True
    except Exception as e:
        logger.error(f"Ошибка сохранения каталога: {e}")
        return False


def rebuild_catalog_index():
    """Перестраивает индекс товаров из CATALOG_DATA в памяти."""
    CATALOG_INDEX.clear()
    for cat in CATALOG_DATA.get("categories", []):
        for item in cat.get("items", []):
            CATALOG_INDEX[item["id"]] = item
    logger.info(f"Индекс каталога перестроен: {len(CATALOG_INDEX)} товаров.")


CATALOG_DATA = load_catalog()
CATALOG_INDEX: dict = {}
rebuild_catalog_index()


def find_item_by_id(item_id):
    return CATALOG_INDEX.get(item_id)


# === FLASK ===
app = Flask(__name__)


# === API MAX ===

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
        logger.warning("WEBHOOK_URL не задан — пропускаем подписку")
        return
    update_types = [
        "message_created", "message_callback", "bot_started",
        "comment_created", "comment_edited", "comment_removed",
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


# === КЛАВИАТУРЫ ===

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


def build_cancel_keyboard():
    return [[{"type": "callback", "text": "\u274C Отмена", "payload": "cancel_action"}]]


def build_catalog_keyboard():
    return [[{"type": "callback", "text": "\U0001F4CB Каталог", "payload": "open_catalog"}]]


def build_cart_catalog_keyboard():
    return [
        [
            {"type": "callback", "text": "\U0001F6D2 Корзина", "payload": "open_cart"},
            {"type": "callback", "text": "\U0001F4CB Каталог", "payload": "open_catalog"},
        ],
    ]


def build_admin_add_keyboard():
    return [
        [
            {"type": "callback", "text": "\U0001F4E6 Категорию", "payload": "admin_add_category"},
            {"type": "callback", "text": "\U0001F4CB Товар", "payload": "admin_add_item"},
        ],
        [{"type": "callback", "text": "\u274C Отмена", "payload": "cancel_action"}],
    ]


def build_admin_edit_keyboard():
    return [
        [
            {"type": "callback", "text": "\u270F\uFE0F Категорию", "payload": "admin_edit_category"},
            {"type": "callback", "text": "\u270F\uFE0F Товар", "payload": "admin_edit_item"},
        ],
        [
            {"type": "callback", "text": "\U0001F4E6 Удалить категорию", "payload": "admin_del_category"},
            {"type": "callback", "text": "\U0001F4CB Удалить товар", "payload": "admin_del_item"},
        ],
        [{"type": "callback", "text": "\u2B05\uFE0F Назад", "payload": "cancel_action"}],
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
            {"type": "callback", "text": "\U0001F4CB Каталог", "payload": "open_catalog"},
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
        buttons.append([{"type": "callback", "text": f"\U0001F4E6 {cat['name']}", "payload": f"show_category:{i}"}])
    send_message(user_id=user_id, text="\U0001F6E0 Каталог мастерской Игнатьевых\n\nВыберите категорию:", keyboard=buttons)


def show_cart(user_id):
    cart = user_carts.get(str(user_id), [])
    if not cart:
        send_message(
            user_id=user_id,
            text="\U0001F6D2 Ваша корзина пуста.\n\n\U0001F449 Откройте \u00ab\U0001F4CB Каталог\u00bb — выберите изделие!",
            keyboard=build_catalog_keyboard(),
        )
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
            {"type": "callback", "text": "\U0001F4CB Каталог", "payload": "open_catalog"},
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


# === ТРАНСЛИТЕРАЦИЯ ДЛЯ ID ===

_TRANS_MAP = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
    ' ': '_', '"': '', "'": '', '«': '', '»': '', '(': '', ')': '',
}


def transliterate(text):
    result = []
    for ch in text.lower():
        result.append(_TRANS_MAP.get(ch, ch if ch.isascii() and (ch.isalnum() or ch == '_') else ''))
    slug = ''.join(result)
    slug = re.sub(r'_+', '_', slug).strip('_')
    return slug or 'item'


def make_unique_id(base_id):
    if base_id not in CATALOG_INDEX:
        return base_id
    i = 2
    while f"{base_id}_{i}" in CATALOG_INDEX:
        i += 1
    return f"{base_id}_{i}"


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

    # --- cancel_action (кнопка Отмена) ---
    if payload == "cancel_action":
        with _lock:
            had = pending_replies.pop(sender_id, None)
            if had:
                save_state()
        answer_callback(callback_id, "Отменено")
        send_main_menu(sender_id)
        return jsonify({"ok": True}), 200

    # --- open_catalog ---
    if payload == "open_catalog":
        answer_callback(callback_id, "Каталог")
        show_catalog(sender_id)
        return jsonify({"ok": True}), 200

    # --- open_cart ---
    if payload == "open_cart":
        answer_callback(callback_id, "Корзина")
        show_cart(sender_id)
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
            answer_callback(callback_id, "\u270D\uFE0F Напишите ответ — бот отправит как комментарий")
            send_message(
                user_id=sender_id,
                text="\u270D\uFE0F Напишите ответ следующим сообщением — бот отправит его как комментарий-ответ.\n\nЕсли передумали — нажмите «Отмена».",
                keyboard=build_cancel_keyboard(),
            )
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
            if not items:
                send_message(user_id=sender_id, text="В этой категории пока нет товаров.")
                return jsonify({"ok": True}), 200
            for item in items:
                send_product_card(sender_id, item)
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
            if sender_id not in user_carts:
                user_carts[sender_id] = []
            user_carts[sender_id].append(item_id)
            save_state()
        answer_callback(callback_id, "\u2705 Добавлено в корзину!")
        count = len(user_carts[sender_id])
        send_message(
            user_id=sender_id,
            text=f"\U0001F6D2 \u00ab{item['name']}\u00bb добавлен в корзину.\nВ корзине товаров: {count}",
            keyboard=build_cart_catalog_keyboard(),
        )
        return jsonify({"ok": True}), 200

    # --- quick_order:<item_id> ---
    if payload.startswith("quick_order:"):
        item_id = payload.split(":", 1)[1]
        item = find_item_by_id(item_id)
        if not item:
            answer_callback(callback_id, "Товар не найден")
            return jsonify({"ok": True}), 200
        answer_callback(callback_id, "Принято!")
        send_message(
            user_id=sender_id,
            text=f"\U0001F4D8 Быстрый заказ: \"{item['name']}\" (Цена: {item['price']} руб.)\n\n\U0001F4DD Напишите, как вас зовут и номер вашего телефона (в любом формате)",
            keyboard=build_cancel_keyboard(),
        )
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
        send_message(
            user_id=sender_id,
            text=f"\U0001F4AC Напишите ваш вопрос про \"{item_name}\"\n\nМастер увидит его сразу и ответит в течение 30 минут.",
            keyboard=build_cancel_keyboard(),
        )
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
        cart = user_carts.get(sender_id, [])
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
        send_message(
            user_id=sender_id,
            text=f"\U0001F6D2 Оформляем заказ:\n\n{items_text}\U0001F4B0 Итого: {total} руб.\n\n\U0001F4DD Напишите, как вас зовут и номер вашего телефона (в любом формате)",
            keyboard=build_cancel_keyboard(),
        )
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
            user_carts[sender_id] = []
            save_state()
        answer_callback(callback_id, "Корзина очищена")
        send_message(
            user_id=sender_id,
            text="\U0001F5D1 Корзина очищена.\n\n\U0001F449 Откройте \u00ab\U0001F4CB Каталог\u00bb — выберите изделие!",
            keyboard=build_catalog_keyboard(),
        )
        return jsonify({"ok": True}), 200

    # ===================== АДМИН-РЕЖИМ =====================

    if not is_admin(sender_id):
        answer_callback(callback_id, "Нет доступа")
        return jsonify({"ok": True}), 200

    # --- admin_add_category ---
    if payload == "admin_add_category":
        answer_callback(callback_id, "Добавление категории")
        send_message(
            user_id=sender_id,
            text="\U0001F4E6 Напишите название новой категории:",
            keyboard=build_cancel_keyboard(),
        )
        with _lock:
            pending_replies[sender_id] = {"step": "admin_new_category", "timestamp": time.time()}
            save_state()
        return jsonify({"ok": True}), 200

    # --- admin_add_item ---
    if payload == "admin_add_item":
        answer_callback(callback_id, "Добавление товара")
        categories = CATALOG_DATA.get("categories", [])
        if not categories:
            send_message(user_id=sender_id, text="Сначала создайте хотя бы одну категорию.")
            return jsonify({"ok": True}), 200
        buttons = []
        for i, cat in enumerate(categories):
            buttons.append([{"type": "callback", "text": f"\U0001F4E6 {cat['name']}", "payload": f"admin_add_item_cat:{i}"}])
        buttons.append([{"type": "callback", "text": "\u274C Отмена", "payload": "cancel_action"}])
        send_message(user_id=sender_id, text="\U0001F4CB Выберите категорию для нового товара:", keyboard=buttons)
        return jsonify({"ok": True}), 200

    # --- admin_add_item_cat:<index> ---
    if payload.startswith("admin_add_item_cat:"):
        cat_index = int(payload.split(":", 1)[1])
        categories = CATALOG_DATA.get("categories", [])
        if cat_index >= len(categories):
            answer_callback(callback_id, "Категория не найдена")
            return jsonify({"ok": True}), 200
        answer_callback(callback_id, "Введите название")
        send_message(
            user_id=sender_id,
            text="\U0001F4DD Напишите название изделия:",
            keyboard=build_cancel_keyboard(),
        )
        with _lock:
            pending_replies[sender_id] = {
                "step": "admin_new_item_name",
                "cat_index": cat_index,
                "timestamp": time.time(),
            }
            save_state()
        return jsonify({"ok": True}), 200

    # --- admin_edit_category ---
    if payload == "admin_edit_category":
        answer_callback(callback_id, "Редактирование категории")
        categories = CATALOG_DATA.get("categories", [])
        if not categories:
            send_message(user_id=sender_id, text="Категорий нет.")
            return jsonify({"ok": True}), 200
        buttons = []
        for i, cat in enumerate(categories):
            buttons.append([{"type": "callback", "text": f"\U0001F4E6 {cat['name']}", "payload": f"admin_edit_cat:{i}"}])
        buttons.append([{"type": "callback", "text": "\u2B05\uFE0F Назад", "payload": "admin_back_to_edit_menu"}])
        send_message(user_id=sender_id, text="\U0001F4E6 Выберите категорию для переименования:", keyboard=buttons)
        return jsonify({"ok": True}), 200

    # --- admin_edit_cat:<index> ---
    if payload.startswith("admin_edit_cat:"):
        cat_index = int(payload.split(":", 1)[1])
        categories = CATALOG_DATA.get("categories", [])
        if cat_index >= len(categories):
            answer_callback(callback_id, "Категория не найдена")
            return jsonify({"ok": True}), 200
        answer_callback(callback_id, "Введите новое название")
        send_message(
            user_id=sender_id,
            text=f"\u270F\uFE0F Текущее название: «{categories[cat_index]['name']}»\n\n\U0001F4DD Напишите новое название:",
            keyboard=build_cancel_keyboard(),
        )
        with _lock:
            pending_replies[sender_id] = {
                "step": "admin_rename_category",
                "cat_index": cat_index,
                "timestamp": time.time(),
            }
            save_state()
        return jsonify({"ok": True}), 200

    # --- admin_edit_item ---
    if payload == "admin_edit_item":
        answer_callback(callback_id, "Редактирование товара")
        categories = CATALOG_DATA.get("categories", [])
        if not categories:
            send_message(user_id=sender_id, text="Категорий нет.")
            return jsonify({"ok": True}), 200
        buttons = []
        for i, cat in enumerate(categories):
            buttons.append([{"type": "callback", "text": f"\U0001F4E6 {cat['name']}", "payload": f"admin_edit_item_cat:{i}"}])
        buttons.append([{"type": "callback", "text": "\u2B05\uFE0F Назад", "payload": "admin_back_to_edit_menu"}])
        send_message(user_id=sender_id, text="\U0001F4E6 Выберите категорию:", keyboard=buttons)
        return jsonify({"ok": True}), 200

    # --- admin_edit_item_cat:<index> ---
    if payload.startswith("admin_edit_item_cat:"):
        cat_index = int(payload.split(":", 1)[1])
        categories = CATALOG_DATA.get("categories", [])
        if cat_index >= len(categories):
            answer_callback(callback_id, "Категория не найдена")
            return jsonify({"ok": True}), 200
        items = categories[cat_index].get("items", [])
        if not items:
            send_message(user_id=sender_id, text="В этой категории нет товаров.")
            return jsonify({"ok": True}), 200
        buttons = []
        for item in items:
            buttons.append([{"type": "callback", "text": f"\U0001FA91 {item['name']} — {item['price']} руб.", "payload": f"admin_edit_item_pick:{cat_index}:{item['id']}"}])
        buttons.append([{"type": "callback", "text": "\u2B05\uFE0F Назад", "payload": "admin_edit_item"}])
        send_message(user_id=sender_id, text=f"\U0001F4CB Товары в категории «{categories[cat_index]['name']}»:", keyboard=buttons)
        return jsonify({"ok": True}), 200

    # --- admin_edit_item_pick:<cat_index>:<item_id> ---
    if payload.startswith("admin_edit_item_pick:"):
        parts = payload.split(":", 2)
        if len(parts) != 3:
            answer_callback(callback_id, "Ошибка формата")
            return jsonify({"ok": True}), 200
        cat_index = int(parts[1])
        item_id = parts[2]
        item = find_item_by_id(item_id)
        if not item:
            answer_callback(callback_id, "Товар не найден")
            return jsonify({"ok": True}), 200
        answer_callback(callback_id, "Редактирование товара")
        photo_info = "\U0001F4F7 Фото: есть" if item.get("photo_url") else "\U0001F4F7 Фото: нет"
        text = (
            f"\u270F\uFE0F Редактирование товара\n\n"
            f"\U0001FA91 {item['name']}\n"
            f"\U0001F4B0 Цена: {item['price']} руб.\n"
            f"\U0001F4DD {item['description']}\n"
            f"{photo_info}\n"
            f"\U0001F194 id: {item['id']}\n\n"
            f"Что изменить?"
        )
        buttons = [
            [
                {"type": "callback", "text": "\U0001FA91 Название", "payload": f"admin_edit_field:name:{cat_index}:{item_id}"},
                {"type": "callback", "text": "\U0001F4B0 Цену", "payload": f"admin_edit_field:price:{cat_index}:{item_id}"},
            ],
            [
                {"type": "callback", "text": "\U0001F4DD Описание", "payload": f"admin_edit_field:description:{cat_index}:{item_id}"},
                {"type": "callback", "text": "\U0001F4F7 Фото", "payload": f"admin_edit_field:photo_url:{cat_index}:{item_id}"},
            ],
            [
                {"type": "callback", "text": "\U0001F4CB Удалить товар", "payload": f"admin_del_item_confirm:{cat_index}:{item_id}"},
                {"type": "callback", "text": "\u2B05\uFE0F Назад", "payload": f"admin_edit_item_cat:{cat_index}"},
            ],
        ]
        send_message(user_id=sender_id, text=text, keyboard=buttons)
        return jsonify({"ok": True}), 200

    # --- admin_edit_field:<field>:<cat_index>:<item_id> ---
    if payload.startswith("admin_edit_field:"):
        parts = payload.split(":", 3)
        if len(parts) != 4:
            answer_callback(callback_id, "Ошибка формата")
            return jsonify({"ok": True}), 200
        field = parts[1]
        cat_index = int(parts[2])
        item_id = parts[3]
        item = find_item_by_id(item_id)
        if not item:
            answer_callback(callback_id, "Товар не найден")
            return jsonify({"ok": True}), 200
        field_names = {
            "name": "название",
            "price": "цену (только число, в рублях)",
            "description": "описание",
            "photo_url": "ссылку на фото (или «нет» чтобы убрать)",
        }
        current_val = item.get(field, "")
        if field == "price":
            current_val = f"{current_val} руб."
        answer_callback(callback_id, f"Изменение: {field_names.get(field, field)}")
        send_message(
            user_id=sender_id,
            text=f"\u270F\uFE0F Изменение: {field_names.get(field, field)}\n\nТекущее значение: {current_val}\n\n\U0001F4DD Напишите новое значение:",
            keyboard=build_cancel_keyboard(),
        )
        with _lock:
            pending_replies[sender_id] = {
                "step": "admin_edit_field_value",
                "field": field,
                "cat_index": cat_index,
                "item_id": item_id,
                "timestamp": time.time(),
            }
            save_state()
        return jsonify({"ok": True}), 200

    # --- admin_del_category ---
    if payload == "admin_del_category":
        answer_callback(callback_id, "Удаление категории")
        categories = CATALOG_DATA.get("categories", [])
        if not categories:
            send_message(user_id=sender_id, text="Категорий нет.")
            return jsonify({"ok": True}), 200
        buttons = []
        for i, cat in enumerate(categories):
            count = len(cat.get("items", []))
            buttons.append([{"type": "callback", "text": f"\U0001F4E6 {cat['name']} ({count} тов.)", "payload": f"admin_del_cat_confirm:{i}"}])
        buttons.append([{"type": "callback", "text": "\u2B05\uFE0F Назад", "payload": "admin_back_to_edit_menu"}])
        send_message(user_id=sender_id, text="\U0001F4E6 Выберите категорию для удаления:", keyboard=buttons)
        return jsonify({"ok": True}), 200

    # --- admin_del_cat_confirm:<index> ---
    if payload.startswith("admin_del_cat_confirm:"):
        cat_index = int(payload.split(":", 1)[1])
        categories = CATALOG_DATA.get("categories", [])
        if cat_index >= len(categories):
            answer_callback(callback_id, "Категория не найдена")
            return jsonify({"ok": True}), 200
        cat_name = categories[cat_index]["name"]
        count = len(categories[cat_index].get("items", []))
        answer_callback(callback_id, "Подтверждение")
        buttons = [
            [
                {"type": "callback", "text": "\u2705 Да, удалить", "payload": f"admin_del_cat_do:{cat_index}"},
                {"type": "callback", "text": "\u274C Нет", "payload": "admin_del_category"},
            ],
        ]
        send_message(
            user_id=sender_id,
            text=f"\u26A0\uFE0F Удалить категорию «{cat_name}»?\nВместе с ней удалятся {count} товаров!\n\nЭто действие нельзя отменить.",
            keyboard=buttons,
        )
        return jsonify({"ok": True}), 200

    # --- admin_del_cat_do:<index> ---
    if payload.startswith("admin_del_cat_do:"):
        cat_index = int(payload.split(":", 1)[1])
        categories = CATALOG_DATA.get("categories", [])
        if cat_index < len(categories):
            cat_name = categories[cat_index]["name"]
            del CATALOG_DATA["categories"][cat_index]
            save_catalog()
            rebuild_catalog_index()
            answer_callback(callback_id, "Удалено")
            send_message(user_id=sender_id, text=f"\u2705 Категория «{cat_name}» удалена.")
        else:
            answer_callback(callback_id, "Не найдено")
        return jsonify({"ok": True}), 200

    # --- admin_del_item ---
    if payload == "admin_del_item":
        answer_callback(callback_id, "Удаление товара")
        categories = CATALOG_DATA.get("categories", [])
        if not categories:
            send_message(user_id=sender_id, text="Категорий нет.")
            return jsonify({"ok": True}), 200
        buttons = []
        for i, cat in enumerate(categories):
            buttons.append([{"type": "callback", "text": f"\U0001F4E6 {cat['name']}", "payload": f"admin_del_item_cat:{i}"}])
        buttons.append([{"type": "callback", "text": "\u2B05\uFE0F Назад", "payload": "admin_back_to_edit_menu"}])
        send_message(user_id=sender_id, text="\U0001F4E6 Выберите категорию:", keyboard=buttons)
        return jsonify({"ok": True}), 200

    # --- admin_del_item_cat:<index> ---
    if payload.startswith("admin_del_item_cat:"):
        cat_index = int(payload.split(":", 1)[1])
        categories = CATALOG_DATA.get("categories", [])
        if cat_index >= len(categories):
            answer_callback(callback_id, "Категория не найдена")
            return jsonify({"ok": True}), 200
        items = categories[cat_index].get("items", [])
        if not items:
            send_message(user_id=sender_id, text="В этой категории нет товаров.")
            return jsonify({"ok": True}), 200
        buttons = []
        for item in items:
            buttons.append([{"type": "callback", "text": f"\U0001FA91 {item['name']} — {item['price']} руб.", "payload": f"admin_del_item_confirm:{cat_index}:{item['id']}"}])
        buttons.append([{"type": "callback", "text": "\u2B05\uFE0F Назад", "payload": "admin_del_item"}])
        send_message(user_id=sender_id, text=f"\U0001F4CB Товары в категории «{categories[cat_index]['name']}»:", keyboard=buttons)
        return jsonify({"ok": True}), 200

    # --- admin_del_item_confirm:<cat_index>:<item_id> ---
    if payload.startswith("admin_del_item_confirm:"):
        parts = payload.split(":", 2)
        if len(parts) != 3:
            answer_callback(callback_id, "Ошибка формата")
            return jsonify({"ok": True}), 200
        cat_index = int(parts[1])
        item_id = parts[2]
        item = find_item_by_id(item_id)
        if not item:
            answer_callback(callback_id, "Товар не найден")
            return jsonify({"ok": True}), 200
        answer_callback(callback_id, "Подтверждение")
        buttons = [
            [
                {"type": "callback", "text": "\u2705 Да, удалить", "payload": f"admin_del_item_do:{cat_index}:{item_id}"},
                {"type": "callback", "text": "\u274C Нет", "payload": f"admin_edit_item_cat:{cat_index}"},
            ],
        ]
        send_message(
            user_id=sender_id,
            text=f"\u26A0\uFE0F Удалить товар «{item['name']}»?\n\nЭто действие нельзя отменить.",
            keyboard=buttons,
        )
        return jsonify({"ok": True}), 200

    # --- admin_del_item_do:<cat_index>:<item_id> ---
    if payload.startswith("admin_del_item_do:"):
        parts = payload.split(":", 2)
        if len(parts) != 3:
            answer_callback(callback_id, "Ошибка формата")
            return jsonify({"ok": True}), 200
        cat_index = int(parts[1])
        item_id = parts[2]
        categories = CATALOG_DATA.get("categories", [])
        if cat_index < len(categories):
            items = categories[cat_index].get("items", [])
            for i, it in enumerate(items):
                if it["id"] == item_id:
                    item_name = it["name"]
                    del items[i]
                    save_catalog()
                    rebuild_catalog_index()
                    answer_callback(callback_id, "Удалено")
                    send_message(user_id=sender_id, text=f"\u2705 Товар «{item_name}» удалён.")
                    return jsonify({"ok": True}), 200
        answer_callback(callback_id, "Не найдено")
        return jsonify({"ok": True}), 200

    # --- admin_back_to_edit_menu ---
    if payload == "admin_back_to_edit_menu":
        answer_callback(callback_id, "Назад")
        send_message(
            user_id=sender_id,
            text="\u270F\uFE0F Редактирование каталога\n\nЧто делаем?",
            keyboard=build_admin_edit_keyboard(),
        )
        return jsonify({"ok": True}), 200

    answer_callback(callback_id, "Ок")
    return jsonify({"ok": True}), 200


# === ОБРАБОТКА АДМИН-СООБЩЕНИЙ ===

def handle_admin_text(sender_id, text):
    """Обработка текстовых шагов админ-режима. Возвращает True если обработано."""
    state = pending_replies.get(sender_id)
    if not isinstance(state, dict):
        return False

    step = state.get("step")
    if not step:
        return False

    # Проверка timeout
    if "timestamp" in state and time.time() - state["timestamp"] > SESSION_TIMEOUT:
        with _lock:
            del pending_replies[sender_id]
            save_state()
        send_message(user_id=sender_id, text="\u23F1\uFE0F Время ожидания истекло. Начните заново.", keyboard=build_main_menu_keyboard())
        return True

    # --- admin_new_category ---
    if step == "admin_new_category":
        cat_name = (text or "").strip()
        if not cat_name:
            send_message(user_id=sender_id, text="Название не может быть пустым. Напишите название:", keyboard=build_cancel_keyboard())
            return True
        # Проверка дубликата
        for cat in CATALOG_DATA.get("categories", []):
            if cat["name"].lower() == cat_name.lower():
                send_message(user_id=sender_id, text=f"\u26A0\uFE0F Категория «{cat_name}» уже существует. Напишите другое название:", keyboard=build_cancel_keyboard())
                return True
        CATALOG_DATA.setdefault("categories", []).append({"name": cat_name, "items": []})
        save_catalog()
        with _lock:
            pending_replies.pop(sender_id, None)
            save_state()
        send_message(
            user_id=sender_id,
            text=f"\u2705 Категория «{cat_name}» добавлена!\nТеперь можно добавить в неё товары.",
            keyboard=build_admin_add_keyboard(),
        )
        return True

    # --- admin_new_item_name ---
    if step == "admin_new_item_name":
        name = (text or "").strip()
        if not name:
            send_message(user_id=sender_id, text="Название не может быть пустым. Напишите название:", keyboard=build_cancel_keyboard())
            return True
        with _lock:
            pending_replies[sender_id]["item_name"] = name
            pending_replies[sender_id]["step"] = "admin_new_item_price"
            pending_replies[sender_id]["timestamp"] = time.time()
            save_state()
        send_message(user_id=sender_id, text="\U0001F4B0 Напишите цену в рублях (только число):", keyboard=build_cancel_keyboard())
        return True

    # --- admin_new_item_price ---
    if step == "admin_new_item_price":
        price_str = (text or "").strip()
        try:
            price = int(price_str)
        except ValueError:
            send_message(user_id=sender_id, text="\u26A0\uFE0F Нужно число. Например: 7900", keyboard=build_cancel_keyboard())
            return True
        with _lock:
            pending_replies[sender_id]["item_price"] = price
            pending_replies[sender_id]["step"] = "admin_new_item_desc"
            pending_replies[sender_id]["timestamp"] = time.time()
            save_state()
        send_message(user_id=sender_id, text="\U0001F4DD Напишите краткое описание (2–3 строки):", keyboard=build_cancel_keyboard())
        return True

    # --- admin_new_item_desc ---
    if step == "admin_new_item_desc":
        desc = (text or "").strip()
        if not desc:
            send_message(user_id=sender_id, text="Описание не может быть пустым. Напишите описание:", keyboard=build_cancel_keyboard())
            return True
        with _lock:
            pending_replies[sender_id]["item_desc"] = desc
            pending_replies[sender_id]["step"] = "admin_new_item_photo"
            pending_replies[sender_id]["timestamp"] = time.time()
            save_state()
        send_message(user_id=sender_id, text="\U0001F4F7 Отправьте ссылку на фото. Если фото нет — напишите «нет»:", keyboard=build_cancel_keyboard())
        return True

    # --- admin_new_item_photo ---
    if step == "admin_new_item_photo":
        photo_input = (text or "").strip()
        photo_url = "" if photo_input.lower() in ("нет", "no", "нету", "-") else photo_input

        name = state.get("item_name", "")
        price = state.get("item_price", 0)
        desc = state.get("item_desc", "")
        cat_index = state.get("cat_index", 0)

        base_id = transliterate(name)
        item_id = make_unique_id(base_id)

        new_item = {
            "id": item_id,
            "name": name,
            "price": price,
            "description": desc,
            "photo_url": photo_url,
        }

        categories = CATALOG_DATA.get("categories", [])
        if cat_index < len(categories):
            categories[cat_index].setdefault("items", []).append(new_item)
            save_catalog()
            rebuild_catalog_index()
            with _lock:
                pending_replies.pop(sender_id, None)
                save_state()
            # Превью
            preview_text = (
                f"\u2705 Товар «{name}» добавлен!\n"
                f"\U0001F194 id: {item_id}\n"
                f"\U0001F4B0 Цена: {price} руб.\n"
                f"\U0001F4DD {desc}\n"
            )
            if photo_url:
                preview_text += f"\U0001F4F7 Фото: {photo_url[:50]}...\n"
            send_message(
                user_id=sender_id,
                text=preview_text + "\n\U0001F447Что дальше?\U0001F447",
                keyboard=build_admin_add_keyboard(),
            )
        else:
            send_message(user_id=sender_id, text="\u274C Категория не найдена. Товар не сохранён.")
            with _lock:
                pending_replies.pop(sender_id, None)
                save_state()
        return True

    # --- admin_rename_category ---
    if step == "admin_rename_category":
        new_name = (text or "").strip()
        if not new_name:
            send_message(user_id=sender_id, text="Название не может быть пустым. Напишите новое название:", keyboard=build_cancel_keyboard())
            return True
        cat_index = state.get("cat_index", 0)
        categories = CATALOG_DATA.get("categories", [])
        if cat_index < len(categories):
            old_name = categories[cat_index]["name"]
            categories[cat_index]["name"] = new_name
            save_catalog()
            with _lock:
                pending_replies.pop(sender_id, None)
                save_state()
            send_message(
                user_id=sender_id,
                text=f"\u2705 Категория переименована!\nБыло: «{old_name}»\nСтало: «{new_name}»\n\n\U0001F447Что дальше?\U0001F447",
                keyboard=build_admin_edit_keyboard(),
            )
        else:
            send_message(user_id=sender_id, text="\u274C Категория не найдена.")
            with _lock:
                pending_replies.pop(sender_id, None)
                save_state()
        return True

    # --- admin_edit_field_value ---
    if step == "admin_edit_field_value":
        field = state.get("field", "")
        cat_index = state.get("cat_index", 0)
        item_id = state.get("item_id", "")
        new_val = (text or "").strip()

        if not new_val:
            send_message(user_id=sender_id, text="Значение не может быть пустым. Напишите новое значение:", keyboard=build_cancel_keyboard())
            return True

        item = find_item_by_id(item_id)
        if not item:
            send_message(user_id=sender_id, text="\u274C Товар не найден.")
            with _lock:
                pending_replies.pop(sender_id, None)
                save_state()
            return True

        old_val = item.get(field, "")

        if field == "price":
            try:
                new_val = int(new_val)
            except ValueError:
                send_message(user_id=sender_id, text="\u26A0\uFE0F Цена должна быть числом. Например: 7900", keyboard=build_cancel_keyboard())
                return True
        elif field == "photo_url":
            if new_val.lower() in ("нет", "no", "нету", "-"):
                new_val = ""

        item[field] = new_val
        save_catalog()
        rebuild_catalog_index()

        field_labels = {
            "name": "Название",
            "price": "Цена",
            "description": "Описание",
            "photo_url": "Фото",
        }
        old_display = f"{old_val} руб." if field == "price" else (old_val or "нет")
        new_display = f"{new_val} руб." if field == "price" else (new_val or "нет")

        with _lock:
            pending_replies.pop(sender_id, None)
            save_state()
        send_message(
            user_id=sender_id,
            text=f"\u2705 {field_labels.get(field, field)} изменён!\nБыло: {old_display}\nСтало: {new_display}\n\n\U0001F447Что дальше?\U0001F447",
            keyboard=build_admin_edit_keyboard(),
        )
        return True

    return False


# === ОБРАБОТКА СООБЩЕНИЙ ===

def handle_admin_reply(sender_id, text):
    """Перехват ответов админа (диалоги с клиентами)."""
    match = re.match(r"^#?(\d+)\s*[:.\u3001\s]\s*(.+)", text, re.DOTALL)
    if match:
        num = int(match.group(1))
        reply_text = match.group(2).strip()
        if num in active_dialogs:
            client_user_id = active_dialogs[num]["user_id"]
            send_message(user_id=client_user_id, text=reply_text, keyboard=build_main_menu_keyboard())
            send_message(user_id=sender_id, text=f"\u2705 Ответ #{num} отправлен клиенту.")
            with _lock:
                del active_dialogs[num]
                save_state()
        else:
            active_nums = list(active_dialogs.keys())
            send_message(user_id=sender_id, text=f"\u26A0\uFE0F Диалог #{num} не найден. Активные: {active_nums}")
        return True
    return False


def handle_pending_state(sender_id, text):
    """Обработка шагов (вопрос, оформление, быстрый заказ). Возвращает True если обработано."""
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
        send_message(user_id=sender_id, text="\u2705 Спасибо за заказ!\n\nМастер свяжется с вами в ближайшее время.", keyboard=build_main_menu_keyboard())
        with _lock:
            user_carts[sender_id] = []
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
        send_message(user_id=sender_id, text="\u2705 Спасибо! Мастер свяжется с вами в ближайшее время.", keyboard=build_main_menu_keyboard())
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
        send_message(user_id=sender_id, text="\u2705 Спасибо за заказ!\n\nМастер свяжется с вами в ближайшее время.", keyboard=build_main_menu_keyboard())
        with _lock:
            user_carts[sender_id] = []
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
        send_message(user_id=sender_id, text="\u2705 Спасибо! Мастер свяжется с вами в ближайшее время.", keyboard=build_main_menu_keyboard())
        return True

    return False


def handle_pending_reply_comment(sender_id, text):
    """Обработка ожидающего ответа на комментарий. Возвращает True если обработано."""
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
            send_message(user_id=sender_id, text="\u274C Не удалось отправить ответ. Проверьте, что бот — администратор канала.", keyboard=build_main_menu_keyboard())
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

    # Перехват ответов админа (диалоги с клиентами)
    if is_admin(sender_id) and text and not text.startswith("/"):
        # Сначала проверяем админ-режим (добавление/редактирование)
        if sender_id in pending_replies:
            state = pending_replies.get(sender_id)
            if isinstance(state, dict) and state.get("step", "").startswith("admin_"):
                if handle_admin_text(sender_id, text):
                    return

        # Затем проверяем ответ на комментарий
        if sender_id in pending_replies:
            state = pending_replies.get(sender_id)
            if isinstance(state, dict) and "post_id" in state:
                if handle_pending_reply_comment(sender_id, text):
                    return

        # Затем проверяем ответ на диалог с клиентом
        if handle_admin_reply(sender_id, text):
            return

    # Команда /cancel
    if cmd in ["/cancel", "/отмена"]:
        with _lock:
            had = pending_replies.pop(sender_id, None)
            if had:
                save_state()
        send_main_menu(sender_id)
        return

    # --- АДМИН-КОМАНДЫ ---
    if cmd == "/добавить" and is_admin(sender_id):
        send_message(
            user_id=sender_id,
            text="\U0001F527 Режим добавления в каталог\n\nЧто добавляем?",
            keyboard=build_admin_add_keyboard(),
        )
        return

    if cmd == "/редактировать" and is_admin(sender_id):
        send_message(
            user_id=sender_id,
            text="\u270F\uFE0F Редактирование каталога\n\nЧто делаем?",
            keyboard=build_admin_edit_keyboard(),
        )
        return

    if cmd == "/каталог_админ" and is_admin(sender_id):
        categories = CATALOG_DATA.get("categories", [])
        if not categories:
            send_message(user_id=sender_id, text="Каталог пуст.")
            return
        lines = []
        for cat in categories:
            lines.append(f"\U0001F4E6 {cat['name']}")
            for item in cat.get("items", []):
                lines.append(f"   \U0001FA91 {item['name']} — {item['price']} руб. (id: {item['id']})")
        send_message(user_id=sender_id, text="\U0001F4CB Текущий каталог:\n\n" + "\n".join(lines))
        return

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

    # Кнопки главного меню — очищаем pending state
    if text and text.strip() == "\U0001F4CB Каталог":
        with _lock:
            had = pending_replies.pop(sender_id, None)
            if had:
                save_state()
        show_catalog(sender_id)
        return
    if text and text.strip() == "\U0001F6D2 Корзина":
        with _lock:
            had = pending_replies.pop(sender_id, None)
            if had:
                save_state()
        show_cart(sender_id)
        return
    if text and text.strip() == "\U0001F4DE Мастер":
        with _lock:
            had = pending_replies.pop(sender_id, None)
            if had:
                save_state()
        send_message(
            user_id=sender_id,
            text="\U0001F4DE Мастер:\n\nЕвгений\n\u260E\uFE0F 8 (989) 622-37-32\n\n\U0001F449 Или закажите через \u00ab\U0001F4CB Каталог\u00bb",
            keyboard=build_catalog_keyboard(),
        )
        return
    if text and text.strip() == "\u2753 Задать вопрос":
        with _lock:
            had = pending_replies.pop(sender_id, None)
            pending_replies[sender_id] = {"step": "waiting_question", "first_name": first_name, "timestamp": time.time()}
            save_state()
        send_message(
            user_id=sender_id,
            text="\U0001F4AC Напишите ваш вопрос прямо здесь.\n\nМастер увидит его сразу и ответит в течение 30 минут.",
            keyboard=build_cancel_keyboard(),
        )
        return

    # Текстовые команды
    if cmd in ["/catalog", "/каталог"]:
        show_catalog(sender_id)
        return
    if cmd in ["/cart", "/корзина"]:
        show_cart(sender_id)
        return
    if cmd in ["/help", "/помощь"]:
        send_message(
            user_id=sender_id,
            text="\U0001FAB5 Мастерская Игнатьевых — помощь\n\n\U0001F4CB /каталог — открыть каталог\n\U0001F6D2 /корзина — посмотреть корзину\n\u2753 /помощь — эта справка\n\U0001F4A1 /cancel — отменить текущее действие\n\nТакже можно нажимать кнопки под сообщениями бота.",
            keyboard=build_main_menu_keyboard(),
        )
        return

    # Обработка шагов (вопрос, заказ, быстрый заказ)
    if sender_id in pending_replies:
        state = pending_replies.get(sender_id)
        if isinstance(state, dict) and "first_name" not in state and "step" in state:
            state["first_name"] = first_name

        # Проверяем админ-шаги
        if isinstance(state, dict) and state.get("step", "").startswith("admin_"):
            if handle_admin_text(sender_id, text):
                return

        if handle_pending_state(sender_id, text):
            return

        if text and not text.startswith("/"):
            if handle_pending_reply_comment(sender_id, text):
                return

    # /start
    if cmd and cmd.startswith("/start"):
        send_welcome(sender_id)
        return

    # Fallback
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
        if chat_id:
            send_welcome(chat_id, is_chat=True)
        elif sender_id:
            send_welcome(sender_id)
        return jsonify({"ok": True}), 200

    if update_type == "message_callback":
        return handle_callback(data)

    # Комментарии
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
