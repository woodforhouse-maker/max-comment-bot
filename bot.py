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
from collections import defaultdict, deque
from flask import Flask, request, jsonify
import requests

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

# Путь к корневому сертификату (если нужен для SSL). Оставьте пустым для проверки по умолчанию.
CA_CERT_PATH = os.environ.get("CA_CERT_PATH", "")

# === АДМИНЫ ===
ADMIN_IDS = set()
if NOTIFY_CHAT_ID:
    ADMIN_IDS.add(str(NOTIFY_CHAT_ID))
ADMIN_IDS.add("39193669")  # Евгений

def is_admin(user_id):
    return str(user_id) in ADMIN_IDS

# WEBHOOK_SECRET — обязательно
REQUIRED_ENV = ["MAX_BOT_TOKEN", "WEBHOOK_URL", "NOTIFY_CHAT_ID", "WEBHOOK_SECRET"]
_missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
if _missing:
    logger.critical(f"Критическая ошибка: не заданы переменные окружения: {', '.join(_missing)}.")
    if "WEBHOOK_SECRET" in _missing:
        logger.critical("WEBHOOK_SECRET обязателен! Без него webhook небезопасен.")
    logger.critical("Завершение работы.")
    os._exit(1)

# === RATE LIMITING ===
_rate_lock = threading.Lock()
_rate_data: dict = defaultdict(lambda: deque())
RATE_LIMIT_PER_SEC = 10
RATE_LIMIT_WINDOW = 1.0

def rate_limit_ok(ip):
    """Простой rate-limit: не более RATE_LIMIT_PER_SEC запросов в секунду с одного IP."""
    now = time.time()
    with _rate_lock:
        dq = _rate_data[ip]
        cutoff = now - RATE_LIMIT_WINDOW
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= RATE_LIMIT_PER_SEC:
            return False
        dq.append(now)
        return True

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


def cleanup_carts():
    """Удаляет из всех корзин товары, которых больше нет в каталоге."""
    changed = False
    with _lock:
        for uid, cart in list(user_carts.items()):
            new_cart = [iid for iid in cart if find_item_by_id(iid) is not None]
            if len(new_cart) != len(cart):
                user_carts[uid] = new_cart
                changed = True
                logger.info(f"Очищена корзина user_id={uid}: удалено {len(cart) - len(new_cart)} позиций")
    if changed:
        save_state()


# === КАТАЛОГ ===
CATALOG_FILE = os.path.join(os.path.dirname(__file__), "catalog.json")
CATALOG_BACKUP = os.path.join(os.path.dirname(__file__), "catalog_backup.json")

# Ограничения длины полей
MAX_NAME_LEN = 100
MAX_DESC_LEN = 500
MAX_PHOTO_URL_LEN = 500


def load_catalog():
    """ТОЛЬКО ЧТЕНИЕ. Ничего не пишет на диск."""
    try:
        with open(CATALOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        cats = data.get("categories", [])
        total_items = sum(len(c.get("items", [])) for c in cats)
        logger.info(f"Каталог загружен: {len(cats)} категорий, {total_items} товаров.")
        return data
    except FileNotFoundError:
        logger.error("catalog.json не найден! Создайте файл или добавьте товары через /добавить.")
        return {"categories": []}
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка в catalog.json: {e}")
        return {"categories": []}


def rebuild_catalog_index():
    """Перестраивает индекс товаров из CATALOG_DATA."""
    CATALOG_INDEX.clear()
    for cat in CATALOG_DATA.get("categories", []):
        for item in cat.get("items", []):
            CATALOG_INDEX[item["id"]] = item


def save_catalog():
    """Сохраняет каталог. ТОЛЬКО для админ-действий.
    Создаёт резервную копию. НЕ перезаписывает если каталог пуст (защита)."""
    cats = CATALOG_DATA.get("categories", [])
    if not cats:
        logger.warning("save_catalog: каталог пуст — НЕ сохраняю (защита от потери данных)!")
        return False
    if os.path.exists(CATALOG_FILE):
        try:
            shutil.copy2(CATALOG_FILE, CATALOG_BACKUP)
            logger.info("Создана резервная копия catalog_backup.json")
        except Exception as e:
            logger.error(f"Не удалось создать резервную копию: {e}")
    try:
        with open(CATALOG_FILE, "w", encoding="utf-8") as f:
            json.dump(CATALOG_DATA, f, ensure_ascii=False, indent=2)
        logger.info(f"Каталог сохранён: {len(cats)} категорий.")
        rebuild_catalog_index()
        return True
    except Exception as e:
        logger.error(f"Ошибка сохранения каталога: {e}")
        return False


CATALOG_DATA = load_catalog()
CATALOG_INDEX: dict = {}
rebuild_catalog_index()


def find_item_by_id(item_id):
    return CATALOG_INDEX.get(item_id)


def find_category_by_item_id(item_id):
    """Возвращает (cat_index, item_index) для товара."""
    for ci, cat in enumerate(CATALOG_DATA.get("categories", [])):
        for ii, item in enumerate(cat.get("items", [])):
            if item["id"] == item_id:
                return ci, ii
    return None, None


TRANSLIT = {
    'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'e','ж':'zh','з':'z',
    'и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r',
    'с':'s','т':'t','у':'u','ф':'f','х':'h','ц':'ts','ч':'ch','ш':'sh','щ':'sch',
    'ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
}

def make_slug(text):
    text = text.lower().strip()
    result = []
    for ch in text:
        if ch in TRANSLIT:
            result.append(TRANSLIT[ch])
        elif ch.isalnum() or ch == '_':
            result.append(ch)
        elif ch in ' -':
            result.append('_')
    slug = ''.join(result).strip('_')
    base = slug
    n = 2
    while base in CATALOG_INDEX:
        base = f"{slug}_{n}"
        n += 1
    return base


def validate_field_length(value, field, user_id):
    """Проверяет длину поля и отправляет сообщение об ошибке. Возвращает True если ОК."""
    limits = {
        "name": MAX_NAME_LEN,
        "description": MAX_DESC_LEN,
        "photo_url": MAX_PHOTO_URL_LEN,
    }
    limit = limits.get(field)
    if limit is None:
        return True
    if len(value) > limit:
        field_names = {"name": "Название", "description": "Описание", "photo_url": "Ссылка на фото"}
        send_message(
            user_id=user_id,
            text=f"⚠️ {field_names.get(field, field)} слишком длинное. Максимум {limit} символов, у вас {len(value)}.",
            keyboard=build_cancel_keyboard(),
        )
        return False
    return True


# === FLASK ===
app = Flask(__name__)


# === API MAX ===
def api_request(method, endpoint, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = TOKEN
    headers["Content-Type"] = "application/json"
    kwargs.setdefault("timeout", 10)
    # SSL: проверка включена. Если задан CA_CERT_PATH — используем его.
    if CA_CERT_PATH:
        kwargs.setdefault("verify", CA_CERT_PATH)
    # По умолчанию verify=True (не переопределяем)
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
        {"name": "добавить", "description": "Добавить категорию или товар (админ)"},
        {"name": "редактировать", "description": "Изменить или удалить товар (админ)"},
        {"name": "экспорт", "description": "Экспорт каталога (админ)"},
        {"name": "импорт", "description": "Импорт каталога (админ)"},
        {"name": "каталог_админ", "description": "Список каталога с id (админ)"},
    ]
    resp = api_request("PATCH", "/me/commands", json={"commands": commands})
    if resp:
        logger.info(f"Регистрация команд: {resp.text}")


def update_webhook_subscription():
    if not WEBHOOK_URL:
        logger.warning("WEBHOOK_URL не задан — пропускаем обновление подписки")
        return
    if not WEBHOOK_SECRET:
        logger.warning("WEBHOOK_SECRET не задан — подписка без секрета небезопасна, пропускаем")
        return
    update_types = [
        "message_created", "message_callback", "bot_started",
        "comment_created", "comment_edited", "comment_removed",
    ]
    body = {"url": WEBHOOK_URL, "update_types": update_types, "secret": WEBHOOK_SECRET}
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
    resp = api_request("POST", "/answers", params={"callback_id": callback_id}, json=body)
    if resp is not None:
        logger.info(f"answer_callback: status={resp.status_code}")


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


def build_catalog_keyboard():
    return [
        [{"type": "message", "text": "\U0001F4CB Каталог", "payload": "\U0001F4CB Каталог"}],
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


def build_back_to_menu_keyboard():
    return [
        [{"type": "message", "text": "\U0001F4CB Главное меню", "payload": "\U0001F4CB Главное меню"}],
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


# === ОТОБРАЖЕНИЕ ===

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
            {"type": "message", "text": "\U0001F4CB Каталог", "payload": "\U0001F4CB Каталог"},
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
            text="\U0001F6D2 Ваша корзина пуста.\n\n\U0001F449 Откройте «\U0001F4CB Каталог» — выберите изделие!",
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


# === АДМИН: КЛАВИАТУРЫ ===

def build_admin_add_keyboard():
    return [
        [
            {"type": "callback", "text": "\U0001F4E6 Категорию", "payload": "admin_add_category_start"},
            {"type": "callback", "text": "\U0001F4CB Товар", "payload": "admin_add_product_start"},
        ],
        [{"type": "callback", "text": "\u274C Отмена", "payload": "cancel_action"}],
    ]


def build_admin_edit_keyboard():
    return [
        [
            {"type": "callback", "text": "\u270F\uFE0F Категорию", "payload": "admin_edit_category_start"},
            {"type": "callback", "text": "\u270F\uFE0F Товар", "payload": "admin_edit_product_start"},
        ],
        [
            {"type": "callback", "text": "\U0001F4E6 Удалить категорию", "payload": "admin_del_category_start"},
            {"type": "callback", "text": "\U0001F4CB Удалить товар", "payload": "admin_del_product_start"},
        ],
        [{"type": "callback", "text": "\u274C Отмена", "payload": "cancel_action"}],
    ]


def build_admin_categories_keyboard(prefix):
    buttons = []
    for i, cat in enumerate(CATALOG_DATA.get("categories", [])):
        buttons.append([{"type": "callback", "text": f"\U0001F4E6 {cat['name']}", "payload": f"{prefix}:{i}"}])
    buttons.append([{"type": "callback", "text": "\u274C Отмена", "payload": "cancel_action"}])
    return buttons


def build_admin_products_keyboard(cat_index, prefix):
    cat = CATALOG_DATA["categories"][cat_index]
    buttons = []
    for i, item in enumerate(cat.get("items", [])):
        buttons.append([{"type": "callback", "text": f"\U0001FA91 {item['name']} — {item['price']} руб.", "payload": f"{prefix}:{cat_index}:{i}"}])
    buttons.append([{"type": "callback", "text": "\u274C Отмена", "payload": "cancel_action"}])
    return buttons


def build_admin_product_edit_fields_keyboard(cat_index, item_index):
    return [
        [
            {"type": "callback", "text": "\U0001FA91 Название", "payload": f"admin_edit_field:{cat_index}:{item_index}:name"},
            {"type": "callback", "text": "\U0001F4B0 Цену", "payload": f"admin_edit_field:{cat_index}:{item_index}:price"},
        ],
        [
            {"type": "callback", "text": "\U0001F4DD Описание", "payload": f"admin_edit_field:{cat_index}:{item_index}:description"},
            {"type": "callback", "text": "\U0001F4D8 Фото", "payload": f"admin_edit_field:{cat_index}:{item_index}:photo_url"},
        ],
        [
            {"type": "callback", "text": "\U0001F4CB Удалить товар", "payload": f"admin_del_product_confirm:{cat_index}:{item_index}"},
        ],
        [{"type": "callback", "text": "\u274C Отмена", "payload": "cancel_action"}],
    ]


# === АДМИН: КОМАНДЫ ===

def admin_show_catalog(user_id):
    if not is_admin(user_id):
        return
    cats = CATALOG_DATA.get("categories", [])
    if not cats:
        send_message(user_id=user_id, text="Каталог пуст. Используйте /добавить.")
        return
    lines = ["\U0001F4CB Текущий каталог:\n"]
    for ci, cat in enumerate(cats):
        lines.append(f"\n\U0001F4E6 {cat['name']} ({len(cat.get('items', []))} тов.):")
        for item in cat.get("items", []):
            lines.append(f"  \u2022 {item['name']} — {item['price']} руб. (id: {item['id']})")
    send_message(user_id=user_id, text="\n".join(lines))


def admin_export_catalog(user_id):
    if not is_admin(user_id):
        return
    try:
        with open(CATALOG_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        max_len = 4000
        if len(raw) <= max_len:
            send_message(
                user_id=user_id,
                text=f"\U0001F4E4 Экспорт каталога\n\nСкопируйте текст ниже и сохраните в файл catalog.json перед деплоем.\n\n```\n{raw}\n```",
            )
        else:
            parts = []
            chunk = ""
            for line in raw.split("\n"):
                if len(chunk) + len(line) + 1 > max_len:
                    parts.append(chunk)
                    chunk = ""
                chunk += line + "\n"
            if chunk:
                parts.append(chunk)
            for i, part in enumerate(parts):
                if i == 0:
                    send_message(user_id=user_id, text=f"\U0001F4E4 Экспорт каталога (часть {i+1}/{len(parts)})\n\n```\n{part}")
                elif i == len(parts) - 1:
                    send_message(user_id=user_id, text=f"\U0001F4E4 (часть {i+1}/{len(parts)})\n\n{part}\n```")
                else:
                    send_message(user_id=user_id, text=f"\U0001F4E4 (часть {i+1}/{len(parts)})\n\n{part}")
                if i < len(parts) - 1:
                    time.sleep(0.3)
    except FileNotFoundError:
        send_message(user_id=user_id, text="\u26A0\uFE0F catalog.json не найден на сервере.")


def _strip_markdown(text):
    """Убирает markdown-обёртку ```json ... ``` или ``` ... ``` из текста."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def admin_import_catalog(user_id):
    """Запускает режим импорта — ждёт текст catalog.json от админа (можно по частям)."""
    if not is_admin(user_id):
        return
    send_message(
        user_id=user_id,
        text="\U0001F4E5 Импорт каталога\n\n"
             "Отправьте содержимое catalog.json.\n\n"
             "Если каталог большой — отправляйте по частям, бот соберёт их вместе.\n"
             "Когда отправите всё — напишите «готово».\n\n"
             "Текущий каталог будет заменён. Создастся резервная копия.",
        keyboard=build_cancel_keyboard(),
    )
    with _lock:
        pending_replies[user_id] = {
            "step": "admin_import",
            "import_parts": [],
            "timestamp": time.time(),
        }
        save_state()


# === CALLBACK ОБРАБОТКА ===

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

    if payload == "cancel_action":
        with _lock:
            had = pending_replies.pop(sender_id, None)
            if had:
                save_state()
        answer_callback(callback_id, "Отменено")
        send_main_menu(sender_id)
        return jsonify({"ok": True}), 200

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
            send_message(user_id=sender_id, text="\u270D\uFE0F Напишите ответ следующим сообщением — бот отправит его как комментарий-ответ.\n\nИли нажмите «Отмена».", keyboard=build_cancel_keyboard())
        else:
            answer_callback(callback_id, "Ошибка: неверный формат")
        return jsonify({"ok": True}), 200

    if payload.startswith("show_category:"):
        cat_index = int(payload.split(":", 1)[1])
        categories = CATALOG_DATA.get("categories", [])
        if cat_index < len(categories):
            category = categories[cat_index]
            answer_callback(callback_id, f"Открываю: {category['name']}")
            items = category.get("items", [])
            if not items:
                send_message(user_id=sender_id, text="В этой категории пока нет товаров.", keyboard=build_catalog_keyboard())
                return jsonify({"ok": True}), 200
            for idx, item in enumerate(items):
                send_product_card(sender_id, item)
                if idx < len(items) - 1:
                    time.sleep(0.1)
        else:
            answer_callback(callback_id, "Категория не найдена")
        return jsonify({"ok": True}), 200

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
            text=f"\U0001F6D2 «{item['name']}» добавлен в корзину.\nВ корзине товаров: {count}\n\n\U0001F447Нажмите кнопку\U0001F447",
            keyboard=build_cart_catalog_keyboard(),
        )
        return jsonify({"ok": True}), 200

    if payload.startswith("quick_order:"):
        item_id = payload.split(":", 1)[1]
        item = find_item_by_id(item_id)
        if not item:
            answer_callback(callback_id, "Товар не найден")
            return jsonify({"ok": True}), 200
        answer_callback(callback_id, "Принято!")
        send_message(
            user_id=sender_id,
            text=f"\U0001F4D8 Быстрый заказ: \"{item['name']}\" (Цена: {item['price']} руб.)\n\nЧтобы мастер связался с вами \U0001F4A1\n\U0001F4DD Напишите, как вас зовут и номер вашего телефона (в любом формате)",
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

    if payload == "clear_cart":
        with _lock:
            user_carts[sender_id] = []
            save_state()
        answer_callback(callback_id, "Корзина очищена")
        send_message(
            user_id=sender_id,
            text="\U0001F5D1 Корзина очищена.\n\n\U0001F449 Откройте «\U0001F4CB Каталог» — выберите изделие!",
            keyboard=build_catalog_keyboard(),
        )
        return jsonify({"ok": True}), 200

    # ========================
    # === АДМИН CALLBACK'и ===
    # ========================
    if not is_admin(sender_id):
        answer_callback(callback_id, "Нет доступа")
        return jsonify({"ok": True}), 200

    if payload == "admin_add_category_start":
        answer_callback(callback_id, "Добавляем категорию")
        send_message(user_id=sender_id, text="\U0001F4E6 Напишите название новой категории:", keyboard=build_cancel_keyboard())
        with _lock:
            pending_replies[sender_id] = {"step": "admin_add_category", "timestamp": time.time()}
            save_state()
        return jsonify({"ok": True}), 200

    if payload == "admin_add_product_start":
        answer_callback(callback_id, "Добавляем товар")
        cats = CATALOG_DATA.get("categories", [])
        if not cats:
            send_message(user_id=sender_id, text="Сначала создайте хотя бы одну категорию.", keyboard=build_admin_add_keyboard())
            return jsonify({"ok": True}), 200
        send_message(user_id=sender_id, text="\U0001F4CB Выберите категорию для нового товара:", keyboard=build_admin_categories_keyboard("admin_add_product_cat"))
        return jsonify({"ok": True}), 200

    if payload.startswith("admin_add_product_cat:"):
        cat_index = int(payload.split(":", 1)[1])
        cat = CATALOG_DATA["categories"][cat_index]
        answer_callback(callback_id, f"Категория: {cat['name']}")
        send_message(user_id=sender_id, text="\U0001F4DD Напишите название изделия:", keyboard=build_cancel_keyboard())
        with _lock:
            pending_replies[sender_id] = {"step": "admin_add_product_name", "cat_index": cat_index, "timestamp": time.time()}
            save_state()
        return jsonify({"ok": True}), 200

    if payload == "admin_edit_category_start":
        answer_callback(callback_id, "Редактируем категорию")
        cats = CATALOG_DATA.get("categories", [])
        if not cats:
            send_message(user_id=sender_id, text="Категорий нет.", keyboard=build_admin_edit_keyboard())
            return jsonify({"ok": True}), 200
        send_message(user_id=sender_id, text="\u270F\uFE0F Выберите категорию для переименования:", keyboard=build_admin_categories_keyboard("admin_edit_category"))
        return jsonify({"ok": True}), 200

    if payload.startswith("admin_edit_category:"):
        cat_index = int(payload.split(":", 1)[1])
        cat = CATALOG_DATA["categories"][cat_index]
        answer_callback(callback_id, f"Редактируем: {cat['name']}")
        send_message(user_id=sender_id, text=f"\u270F\uFE0F Текущее название: {cat['name']}\n\nНапишите новое название:", keyboard=build_cancel_keyboard())
        with _lock:
            pending_replies[sender_id] = {"step": "admin_edit_category_name", "cat_index": cat_index, "timestamp": time.time()}
            save_state()
        return jsonify({"ok": True}), 200

    if payload == "admin_edit_product_start":
        answer_callback(callback_id, "Редактируем товар")
        cats = CATALOG_DATA.get("categories", [])
        if not cats:
            send_message(user_id=sender_id, text="Категорий нет.", keyboard=build_admin_edit_keyboard())
            return jsonify({"ok": True}), 200
        send_message(user_id=sender_id, text="\u270F\uFE0F Выберите категорию:", keyboard=build_admin_categories_keyboard("admin_edit_product_cat"))
        return jsonify({"ok": True}), 200

    if payload.startswith("admin_edit_product_cat:"):
        cat_index = int(payload.split(":", 1)[1])
        cat = CATALOG_DATA["categories"][cat_index]
        answer_callback(callback_id, f"Категория: {cat['name']}")
        if not cat.get("items"):
            send_message(user_id=sender_id, text="В этой категории нет товаров.", keyboard=build_admin_edit_keyboard())
            return jsonify({"ok": True}), 200
        send_message(user_id=sender_id, text="\U0001F4CB Товары в категории:", keyboard=build_admin_products_keyboard(cat_index, "admin_edit_product"))
        return jsonify({"ok": True}), 200

    if payload.startswith("admin_edit_product:"):
        parts = payload.split(":", 2)
        cat_index, item_index = int(parts[1]), int(parts[2])
        item = CATALOG_DATA["categories"][cat_index]["items"][item_index]
        answer_callback(callback_id, f"Редактируем: {item['name']}")
        text = (
            f"\u270F\uFE0F Редактирование товара\n\n"
            f"\U0001FA91 {item['name']}\n"
            f"\U0001F4B0 Цена: {item['price']} руб.\n"
            f"\U0001F4DD {item.get('description', '')}\n"
            f"\U0001F4D8 Фото: {'есть' if item.get('photo_url') else 'нет'}\n"
            f"\U0001F194 id: {item['id']}\n\n"
            f"Что изменить?"
        )
        send_message(user_id=sender_id, text=text, keyboard=build_admin_product_edit_fields_keyboard(cat_index, item_index))
        return jsonify({"ok": True}), 200

    if payload.startswith("admin_edit_field:"):
        parts = payload.split(":", 3)
        cat_index, item_index, field = int(parts[1]), int(parts[2]), parts[3]
        item = CATALOG_DATA["categories"][cat_index]["items"][item_index]
        field_names = {"name": "название", "price": "цену (только число, в рублях)", "description": "описание", "photo_url": "ссылку на фото (или «нет» чтобы убрать)"}
        answer_callback(callback_id, f"Изменяем: {field_names.get(field, field)}")
        current = item.get(field, "")
        send_message(
            user_id=sender_id,
            text=f"\u270F\uFE0F Изменение: {field_names.get(field, field)}\n\nТекущее значение: {current}\n\n\U0001F4DD Напишите новое значение:",
            keyboard=build_cancel_keyboard(),
        )
        with _lock:
            pending_replies[sender_id] = {"step": "admin_edit_field_value", "cat_index": cat_index, "item_index": item_index, "field": field, "timestamp": time.time()}
            save_state()
        return jsonify({"ok": True}), 200

    if payload == "admin_del_category_start":
        answer_callback(callback_id, "Удаляем категорию")
        cats = CATALOG_DATA.get("categories", [])
        if not cats:
            send_message(user_id=sender_id, text="Категорий нет.", keyboard=build_admin_edit_keyboard())
            return jsonify({"ok": True}), 200
        send_message(user_id=sender_id, text="\U0001F4E6 Выберите категорию для удаления:", keyboard=build_admin_categories_keyboard("admin_del_category"))
        return jsonify({"ok": True}), 200

    if payload.startswith("admin_del_category:"):
        cat_index = int(payload.split(":", 1)[1])
        cat = CATALOG_DATA["categories"][cat_index]
        answer_callback(callback_id, f"Удаляем: {cat['name']}")
        send_message(
            user_id=sender_id,
            text=f"\u26A0\uFE0F Удалить категорию «{cat['name']}»?\nБудут удалены все товары в ней ({len(cat.get('items', []))} шт.).",
            keyboard=[
                [{"type": "callback", "text": "\u2705 Да, удалить", "payload": f"admin_del_category_confirm:{cat_index}"}],
                [{"type": "callback", "text": "\u274C Отмена", "payload": "cancel_action"}],
            ],
        )
        return jsonify({"ok": True}), 200

    if payload.startswith("admin_del_category_confirm:"):
        cat_index = int(payload.split(":", 1)[1])
        cat = CATALOG_DATA["categories"][cat_index]
        answer_callback(callback_id, "Удалено")
        with _lock:
            del CATALOG_DATA["categories"][cat_index]
            save_catalog()
            cleanup_carts()
        send_message(user_id=sender_id, text=f"\u2705 Категория «{cat['name']}» удалена.", keyboard=build_admin_edit_keyboard())
        return jsonify({"ok": True}), 200

    if payload == "admin_del_product_start":
        answer_callback(callback_id, "Удаляем товар")
        cats = CATALOG_DATA.get("categories", [])
        if not cats:
            send_message(user_id=sender_id, text="Категорий нет.", keyboard=build_admin_edit_keyboard())
            return jsonify({"ok": True}), 200
        send_message(user_id=sender_id, text="\U0001F4CB Выберите категорию:", keyboard=build_admin_categories_keyboard("admin_del_product_cat"))
        return jsonify({"ok": True}), 200

    if payload.startswith("admin_del_product_cat:"):
        cat_index = int(payload.split(":", 1)[1])
        cat = CATALOG_DATA["categories"][cat_index]
        answer_callback(callback_id, f"Категория: {cat['name']}")
        if not cat.get("items"):
            send_message(user_id=sender_id, text="В этой категории нет товаров.", keyboard=build_admin_edit_keyboard())
            return jsonify({"ok": True}), 200
        send_message(user_id=sender_id, text="\U0001F4CB Выберите товар для удаления:", keyboard=build_admin_products_keyboard(cat_index, "admin_del_product"))
        return jsonify({"ok": True}), 200

    if payload.startswith("admin_del_product:") and not payload.startswith("admin_del_product_cat:") and not payload.startswith("admin_del_product_confirm:"):
        parts = payload.split(":", 2)
        cat_index, item_index = int(parts[1]), int(parts[2])
        item = CATALOG_DATA["categories"][cat_index]["items"][item_index]
        answer_callback(callback_id, f"Удаляем: {item['name']}")
        send_message(
            user_id=sender_id,
            text=f"\u26A0\uFE0F Удалить товар «{item['name']}»?",
            keyboard=[
                [{"type": "callback", "text": "\u2705 Да, удалить", "payload": f"admin_del_product_confirm:{cat_index}:{item_index}"}],
                [{"type": "callback", "text": "\u274C Отмена", "payload": "cancel_action"}],
            ],
        )
        return jsonify({"ok": True}), 200

    if payload.startswith("admin_del_product_confirm:"):
        parts = payload.split(":", 2)
        cat_index, item_index = int(parts[1]), int(parts[2])
        item = CATALOG_DATA["categories"][cat_index]["items"][item_index]
        answer_callback(callback_id, "Удалено")
        with _lock:
            del CATALOG_DATA["categories"][cat_index]["items"][item_index]
            save_catalog()
            cleanup_carts()
        send_message(user_id=sender_id, text=f"\u2705 Товар «{item['name']}» удалён.", keyboard=build_admin_edit_keyboard())
        return jsonify({"ok": True}), 200

    answer_callback(callback_id, "Ок")
    return jsonify({"ok": True}), 200


# === АДМИН: ОБРАБОТКА ТЕКСТОВЫХ ШАГОВ ===

def handle_admin_steps(sender_id, text):
    """Обработка админ-шагов. Возвращает True если обработано."""
    state = pending_replies.get(sender_id)
    if not state or not isinstance(state, dict):
        return False
    step = state.get("step", "")
    if not step or not step.startswith("admin_"):
        return False

    if "timestamp" in state and time.time() - state["timestamp"] > SESSION_TIMEOUT:
        with _lock:
            del pending_replies[sender_id]
            save_state()
        send_message(user_id=sender_id, text="\u23F1\uFE0F Время ожидания истекло. Начните заново.", keyboard=build_main_menu_keyboard())
        return True

    if step == "admin_add_category":
        name = (text or "").strip()
        if not name:
            send_message(user_id=sender_id, text="Название не может быть пустым. Напишите название:")
            return True
        if len(name) > MAX_NAME_LEN:
            send_message(user_id=sender_id, text=f"\u26A0\uFE0F Название слишком длинное. Максимум {MAX_NAME_LEN} символов, у вас {len(name)}.")
            return True
        for cat in CATALOG_DATA.get("categories", []):
            if cat["name"].lower() == name.lower():
                send_message(user_id=sender_id, text=f"\u26A0\uFE0F Категория «{name}» уже существует. Напишите другое название:")
                return True
        CATALOG_DATA.setdefault("categories", []).append({"name": name, "items": []})
        save_catalog()
        with _lock:
            del pending_replies[sender_id]
            save_state()
        send_message(
            user_id=sender_id,
            text=f"\u2705 Категория «{name}» добавлена!\n\nТеперь можно добавить в неё товары.",
            keyboard=build_admin_add_keyboard(),
        )
        return True

    if step == "admin_add_product_name":
        name = (text or "").strip()
        if not name:
            send_message(user_id=sender_id, text="Название не может быть пустым. Напишите название:")
            return True
        if len(name) > MAX_NAME_LEN:
            send_message(user_id=sender_id, text=f"\u26A0\uFE0F Название слишком длинное. Максимум {MAX_NAME_LEN} символов, у вас {len(name)}.")
            return True
        with _lock:
            pending_replies[sender_id]["name"] = name
            pending_replies[sender_id]["step"] = "admin_add_product_price"
            pending_replies[sender_id]["timestamp"] = time.time()
            save_state()
        send_message(user_id=sender_id, text="\U0001F4B0 Напишите цену в рублях (только число):", keyboard=build_cancel_keyboard())
        return True

    if step == "admin_add_product_price":
        price_text = (text or "").strip()
        try:
            price = int(price_text)
            if price <= 0:
                raise ValueError
        except ValueError:
            send_message(user_id=sender_id, text="\u26A0\uFE0F Некорректная цена. Напишите число, например: 7900")
            return True
        with _lock:
            pending_replies[sender_id]["price"] = price
            pending_replies[sender_id]["step"] = "admin_add_product_desc"
            pending_replies[sender_id]["timestamp"] = time.time()
            save_state()
        send_message(user_id=sender_id, text="\U0001F4DD Напишите краткое описание (2–3 строки):", keyboard=build_cancel_keyboard())
        return True

    if step == "admin_add_product_desc":
        desc = (text or "").strip()
        if not desc:
            send_message(user_id=sender_id, text="Описание не может быть пустым. Напишите описание:")
            return True
        if len(desc) > MAX_DESC_LEN:
            send_message(user_id=sender_id, text=f"\u26A0\uFE0F Описание слишком длинное. Максимум {MAX_DESC_LEN} символов, у вас {len(desc)}.")
            return True
        with _lock:
            pending_replies[sender_id]["description"] = desc
            pending_replies[sender_id]["step"] = "admin_add_product_photo"
            pending_replies[sender_id]["timestamp"] = time.time()
            save_state()
        send_message(user_id=sender_id, text="\U0001F4D8 Отправьте ссылку на фото. Если фото нет — напишите «нет»:", keyboard=build_cancel_keyboard())
        return True

    if step == "admin_add_product_photo":
        photo = (text or "").strip()
        if photo.lower() in ("нет", "no", "-", "нету"):
            photo = ""
        if photo and len(photo) > MAX_PHOTO_URL_LEN:
            send_message(user_id=sender_id, text=f"\u26A0\uFE0F Ссылка слишком длинная. Максимум {MAX_PHOTO_URL_LEN} символов, у вас {len(photo)}.")
            return True
        cat_index = state["cat_index"]
        name = state["name"]
        price = state["price"]
        desc = state["description"]
        item_id = make_slug(name)
        item = {
            "id": item_id,
            "name": name,
            "price": price,
            "description": desc,
            "photo_url": photo,
        }
        CATALOG_DATA["categories"][cat_index].setdefault("items", []).append(item)
        save_catalog()
        with _lock:
            del pending_replies[sender_id]
            save_state()
        preview = f"\U0001FA91 {name}\n\U0001F4B0 Цена: {price} руб.\n\U0001F4DD {desc}\n\U0001F4D8 Фото: {'есть' if photo else 'нет'}\n\U0001F194 id: {item_id}"
        send_message(
            user_id=sender_id,
            text=f"\u2705 Товар добавлен!\n\n{preview}",
            keyboard=build_admin_add_keyboard(),
        )
        return True

    if step == "admin_edit_category_name":
        name = (text or "").strip()
        if not name:
            send_message(user_id=sender_id, text="Название не может быть пустым. Напишите название:")
            return True
        if len(name) > MAX_NAME_LEN:
            send_message(user_id=sender_id, text=f"\u26A0\uFE0F Название слишком длинное. Максимум {MAX_NAME_LEN} символов, у вас {len(name)}.")
            return True
        cat_index = state["cat_index"]
        old_name = CATALOG_DATA["categories"][cat_index]["name"]
        CATALOG_DATA["categories"][cat_index]["name"] = name
        save_catalog()
        with _lock:
            del pending_replies[sender_id]
            save_state()
        send_message(
            user_id=sender_id,
            text=f"\u2705 Категория переименована!\nБыло: {old_name}\nСтало: {name}",
            keyboard=build_admin_edit_keyboard(),
        )
        return True

    if step == "admin_edit_field_value":
        value = (text or "").strip()
        if not value:
            send_message(user_id=sender_id, text="Значение не может быть пустым. Напишите значение:")
            return True
        cat_index = state["cat_index"]
        item_index = state["item_index"]
        field = state["field"]
        item = CATALOG_DATA["categories"][cat_index]["items"][item_index]

        if field == "price":
            try:
                value = int(value)
                if value <= 0:
                    raise ValueError
            except ValueError:
                send_message(user_id=sender_id, text="\u26A0\uFE0F Некорректная цена. Напишите число, например: 7900")
                return True

        if field == "photo_url" and value.lower() in ("нет", "no", "-", "нету"):
            value = ""

        if not validate_field_length(value, field, sender_id):
            return True

        old_value = item.get(field, "")
        item[field] = value
        save_catalog()
        with _lock:
            del pending_replies[sender_id]
            save_state()

        field_names = {"name": "Название", "price": "Цена", "description": "Описание", "photo_url": "Фото"}
        send_message(
            user_id=sender_id,
            text=f"\u2705 {field_names.get(field, field)} изменён!\nБыло: {old_value}\nСтало: {value}",
            keyboard=build_admin_edit_keyboard(),
        )
        return True

    if step == "admin_import":
        raw = (text or "").strip()

        if raw.lower() in ("готово", "done", "завершить", "/end"):
            parts = state.get("import_parts", [])
            if not parts:
                send_message(
                    user_id=sender_id,
                    text="Вы ещё не отправили ни одной части. "
                         "Отправьте текст catalog.json или нажмите «Отмена».",
                )
                return True
            full_text = _strip_markdown("\n".join(parts))
            try:
                data = json.loads(full_text)
                if not isinstance(data, dict) or "categories" not in data:
                    raise ValueError("Нет ключа 'categories'")
                with _lock:
                    CATALOG_DATA.clear()
                    CATALOG_DATA.update(data)
                    ok = save_catalog()
                    if ok:
                        cleanup_carts()
                if ok:
                    with _lock:
                        del pending_replies[sender_id]
                        save_state()
                    cats = data.get("categories", [])
                    total = sum(len(c.get("items", [])) for c in cats)
                    send_message(
                        user_id=sender_id,
                        text=f"\u2705 Каталог импортирован!\n\nКатегорий: {len(cats)}\nТоваров: {total}",
                        keyboard=build_main_menu_keyboard(),
                    )
                else:
                    send_message(
                        user_id=sender_id,
                        text="\u26A0\uFE0F Не удалось сохранить каталог. Проверьте данные.",
                    )
            except (json.JSONDecodeError, ValueError) as e:
                send_message(
                    user_id=sender_id,
                    text=f"\u274C Ошибка: не удалось разобрать JSON.\n\nОшибка: {e}\n\n"
                         "Проверьте, что отправили все части, и попробуйте снова «готово».\n"
                         "Или нажмите «Отмена».",
                )
            return True

        clean_part = _strip_markdown(raw)
        parts = state.get("import_parts", [])
        parts.append(clean_part)

        with _lock:
            pending_replies[sender_id]["import_parts"] = parts
            pending_replies[sender_id]["timestamp"] = time.time()
            save_state()

        full_text = _strip_markdown("\n".join(parts))
        try:
            data = json.loads(full_text)
            if isinstance(data, dict) and "categories" in data:
                with _lock:
                    CATALOG_DATA.clear()
                    CATALOG_DATA.update(data)
                    ok = save_catalog()
                    if ok:
                        cleanup_carts()
                if ok:
                    with _lock:
                        del pending_replies[sender_id]
                        save_state()
                    cats = data.get("categories", [])
                    total = sum(len(c.get("items", [])) for c in cats)
                    send_message(
                        user_id=sender_id,
                        text=f"\u2705 Каталог импортирован!\n\nКатегорий: {len(cats)}\nТоваров: {total}",
                        keyboard=build_main_menu_keyboard(),
                    )
                    return True
        except (json.JSONDecodeError, ValueError):
            pass

        total_chars = sum(len(p) for p in parts)
        send_message(
            user_id=sender_id,
            text=f"\u2705 Часть {len(parts)} получена. Собрано: {total_chars} символов.\n\n"
                 "Отправьте следующую часть или напишите «готово» для завершения.",
            keyboard=build_cancel_keyboard(),
        )
        return True

    return False


# === ОБРАБОТКА СООБЩЕНИЙ ===

def handle_admin_reply(sender_id, text):
    match = re.match(r"^#?(\d+)\s*[:.\u3001\s]\s*(.+)", text, re.DOTALL)
    if match:
        num = int(match.group(1))
        reply_text = match.group(2).strip()
        with _lock:
            if num in active_dialogs:
                client_user_id = active_dialogs[num]["user_id"]
                del active_dialogs[num]
                save_state()
            else:
                client_user_id = None
                active_nums = list(active_dialogs.keys())
        if client_user_id:
            send_message(user_id=client_user_id, text=reply_text)
            send_message(user_id=client_user_id, text="\U0001F447Главное меню\U0001F447", keyboard=build_main_menu_keyboard())
            send_message(user_id=sender_id, text=f"\u2705 Ответ #{num} отправлен клиенту.")
            logger.info(f"Ответ #{num} отправлен user_id={client_user_id}")
        else:
            send_message(user_id=sender_id, text=f"\u26A0\uFE0F Диалог #{num} не найден. Активные: {active_nums}")
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
            send_message(user_id=sender_id, text="Пожалуйста, напишите ваш вопрос:")
            return True
        with _lock:
            pending_replies.pop(sender_id, None)
            global question_counter
            question_counter += 1
            num = question_counter
            item_name = state.get("item_name")
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

    if step == "waiting_name":
        name = (text or "").strip()
        if not name:
            send_message(user_id=sender_id, text="Пожалуйста, напишите имя:")
            return True
        with _lock:
            pending_replies[sender_id]["name"] = name
            pending_replies[sender_id]["step"] = "waiting_phone"
            pending_replies[sender_id]["timestamp"] = time.time()
            save_state()
        send_message(user_id=sender_id, text=f"{name}, спасибо! \U0001F4DE Напишите ваш номер телефона (в любом формате)")
        return True

    if step == "waiting_phone":
        phone = (text or "").strip()
        if not phone:
            send_message(user_id=sender_id, text="Пожалуйста, напишите номер телефона:")
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
            send_message(user_id=sender_id, text="Пожалуйста, напишите номер телефона:")
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
    with _lock:
        state = pending_replies.get(sender_id)
        if not isinstance(state, dict) or "post_id" not in state:
            return False
        reply_data = pending_replies.pop(sender_id, None)
        if reply_data:
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


def handle_message_created(data):
    message = data.get("message", {})
    sender_id = str(message.get("sender", {}).get("user_id", ""))
    text = message.get("body", {}).get("text", "")
    first_name = message.get("sender", {}).get("first_name", "Пользователь")
    logger.info(f"Сообщение от user_id={sender_id}: {text}")
    cmd = text.lower().strip() if text else ""

    # Перехват ответов админа
    if is_admin(sender_id) and text and not text.startswith("/"):
        if handle_admin_reply(sender_id, text):
            return

    # Команда /cancel
    if cmd in ["/cancel", "/отмена"]:
        with _lock:
            had = pending_replies.pop(sender_id, None)
            if had:
                save_state()
        if had:
            send_message(user_id=sender_id, text="\u274C Действие отменено.", keyboard=build_main_menu_keyboard())
        else:
            send_message(user_id=sender_id, text="Нечего отменять.", keyboard=build_main_menu_keyboard())
        return

    # Админ-команды
    if is_admin(sender_id):
        if cmd == "/добавить":
            send_message(user_id=sender_id, text="\U0001F527 Режим добавления в каталог\n\nЧто добавляем?", keyboard=build_admin_add_keyboard())
            return
        if cmd == "/редактировать":
            send_message(user_id=sender_id, text="\u270F\uFE0F Редактирование каталога\n\nЧто делаем?", keyboard=build_admin_edit_keyboard())
            return
        if cmd == "/каталог_админ":
            admin_show_catalog(sender_id)
            return
        if cmd == "/экспорт":
            admin_export_catalog(sender_id)
            return
        if cmd == "/импорт":
            admin_import_catalog(sender_id)
            return

    # Кнопки главного меню — очищаем pending state
    if text and text.strip() == "\U0001F4CB Каталог":
        with _lock:
            if sender_id in pending_replies:
                del pending_replies[sender_id]
                save_state()
        show_catalog(sender_id)
        return
    if text and text.strip() == "\U0001F6D2 Корзина":
        with _lock:
            if sender_id in pending_replies:
                del pending_replies[sender_id]
                save_state()
        show_cart(sender_id)
        return
    if text and text.strip() == "\U0001F4DE Мастер":
        with _lock:
            if sender_id in pending_replies:
                del pending_replies[sender_id]
                save_state()
        send_message(
            user_id=sender_id,
            text="\U0001F4DE Мастер:\n\nЕвгений\n\u260E\uFE0F 8 (989) 622-37-32\n\n\U0001F449 Или закажите через «\U0001F4CB Каталог»",
            keyboard=build_catalog_keyboard(),
        )
        return
    if text and text.strip() == "\u2753 Задать вопрос":
        with _lock:
            if sender_id in pending_replies:
                del pending_replies[sender_id]
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
        send_message(
            user_id=sender_id,
            text="\U0001FAB5 Мастерская Игнатьевых — помощь\n\n\U0001F4CB /каталог — открыть каталог\n\U0001F6D2 /корзина — посмотреть корзину\n\u2753 /помощь — эта справка\n\U0001F4A1 /cancel — отменить текущее действие\n\nТакже можно нажимать кнопки под сообщениями бота.",
            keyboard=build_main_menu_keyboard(),
        )
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

    # Обработка шагов
    if sender_id in pending_replies:
        with _lock:
            state = pending_replies.get(sender_id)
            if isinstance(state, dict) and "first_name" not in state and "step" in state:
                state["first_name"] = first_name

        if is_admin(sender_id) and handle_admin_steps(sender_id, text):
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

    # Rate limiting
    client_ip = request.remote_addr or "unknown"
    if not rate_limit_ok(client_ip):
        logger.warning(f"Rate limit превышен для IP={client_ip}")
        return jsonify({"error": "rate limit"}), 429

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
        "webhook_secret_configured": bool(WEBHOOK_SECRET),
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
