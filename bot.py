import os
import sys
import hmac
import base64
import json
import re
import time
import signal
import threading
import logging
import shutil
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# === Глобальная HTTP-сессия (Fix 7) ===
_HTTP_SESSION = requests.Session()

# === S3 (Yandex Object Storage) ===
S3_ENABLED = False
S3_BUCKET = os.environ.get("YANDEX_S3_BUCKET", "")
S3_ACCESS_KEY = os.environ.get("YANDEX_S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.environ.get("YANDEX_S3_SECRET_KEY", "")
S3_CATALOG_KEY = "catalog.json"
S3_CLIENT = None

if S3_BUCKET and S3_ACCESS_KEY and S3_SECRET_KEY:
    try:
        import boto3
        from botocore.config import Config
        S3_CLIENT = boto3.client(
            "s3",
            endpoint_url="https://storage.yandexcloud.net",
            aws_access_key_id=S3_ACCESS_KEY,
            aws_secret_access_key=S3_SECRET_KEY,
            region_name="ru-central1",
            config=Config(
                signature_version="s3v4",
                connect_timeout=5,
                read_timeout=10,
                retries={"max_attempts": 2},
            ),
        )
        S3_ENABLED = True
    except ImportError:
        logging.warning("boto3 не установлен — S3 отключён")
    except Exception as e:
        logging.warning(f"S3 инициализация не удалась: {e}")
else:
    logging.info("S3 переменные не заданы — S3 отключён")


def s3_upload_catalog():
    """Загружает catalog.json в Yandex Object Storage."""
    if not S3_ENABLED or not S3_CLIENT:
        return False, "S3 не настроен"
    try:
        with open(CATALOG_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        S3_CLIENT.put_object(
            Bucket=S3_BUCKET,
            Key=S3_CATALOG_KEY,
            Body=raw.encode("utf-8"),
            ContentType="application/json; charset=utf-8",
        )
        logging.info(f"Каталог загружен в S3: {S3_BUCKET}/{S3_CATALOG_KEY}")
        return True, "OK"
    except Exception as e:
        logging.error(f"Ошибка загрузки каталога в S3: {e}")
        return False, str(e)


def s3_download_catalog():
    """Скачивает catalog.json из Yandex Object Storage. Возвращает dict или None."""
    if not S3_ENABLED or not S3_CLIENT:
        return None
    try:
        resp = S3_CLIENT.get_object(Bucket=S3_BUCKET, Key=S3_CATALOG_KEY)
        data = resp["Body"].read().decode("utf-8")
        result = json.loads(data)
        logging.info(f"Каталог скачан из S3: {S3_BUCKET}/{S3_CATALOG_KEY}")
        return result
    except Exception as e:
        logging.warning(f"Не удалось скачать каталог из S3: {e}")
        return None


def s3_get_presigned_url(expires=3600):
    """Генерирует ссылку для скачивания catalog.json из S3 (действует expires секунд)."""
    if not S3_ENABLED or not S3_CLIENT:
        return None
    try:
        url = S3_CLIENT.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": S3_CATALOG_KEY},
            ExpiresIn=expires,
        )
        return url
    except Exception as e:
        logging.error(f"Ошибка генерации presigned URL: {e}")
        return None


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

# === АДМИНЫ ===
ADMIN_IDS = set()
if NOTIFY_CHAT_ID:
    ADMIN_IDS.add(str(NOTIFY_CHAT_ID))
ADMIN_IDS.add("39193669")  # Евгений

def is_admin(user_id):
    return str(user_id) in ADMIN_IDS

REQUIRED_ENV = ["MAX_BOT_TOKEN", "WEBHOOK_URL", "NOTIFY_CHAT_ID"]
_missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
if _missing:
    logger.warning(f"Не заданы переменные окружения: {', '.join(_missing)}.")

# === АТОМАРНАЯ ЗАПИСЬ ФАЙЛОВ (Fix 6) ===
def _atomic_write_file(filepath, content):
    """Атомарная запись: пишем во временный файл, затем переименовываем."""
    tmp = filepath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, filepath)

def _atomic_write_json(filepath, data):
    """Атомарная запись JSON в файл."""
    _atomic_write_file(filepath, json.dumps(data, ensure_ascii=False))

# === SQLite ДЛЯ ПЕРСИСТЕНТНОГО СОСТОЯНИЯ (Fix 5) ===
DB_PATH = os.environ.get("DB_PATH", os.path.join(STATE_DIR, "bot.db"))
_db_conn = None

def init_db():
    global _db_conn
    try:
        _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db_conn.execute("PRAGMA journal_mode=WAL")
        _db_conn.execute("PRAGMA busy_timeout=5000")
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        _db_conn.commit()
        logger.info(f"SQLite инициализирован: {DB_PATH}")
    except Exception as e:
        logger.error(f"Ошибка инициализации SQLite: {e}")
        _db_conn = None

def db_get(key, default=None):
    if not _db_conn:
        return default
    try:
        cur = _db_conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,))
        row = cur.fetchone()
        if row:
            return json.loads(row[0])
        return default
    except Exception as e:
        logger.error(f"db_get({key}): {e}")
        return default

def db_set(key, value):
    if not _db_conn:
        return
    try:
        now = datetime.now().isoformat()
        _db_conn.execute(
            "INSERT OR REPLACE INTO kv_store (key, value, updated_at) VALUES (?, ?, ?)",
            (key, json.dumps(value, ensure_ascii=False, default=str), now)
        )
        _db_conn.commit()
    except Exception as e:
        logger.error(f"db_set({key}): {e}")

# === СТАТИСТИКА ===
STATS_FILE = os.path.join(STATE_DIR, "stats.json")
UNIQUE_USERS_FILE = os.path.join(STATE_DIR, "unique_users.json")

stats = {
    "total_users": 0,
    "users_today": set(),
    "users_yesterday": set(),
    "today_date": "",
    "catalog_views": 0,
    "category_views": 0,
    "product_views": 0,
    "cart_adds": 0,
    "quick_orders": 0,
    "cart_orders": 0,
    "questions": 0,
    "comments_forwarded": 0,
    "deals_closed": 0,
    "first_contact": "",
    "last_contact": "",
    "daily": {},
}

unique_users = set()

def load_stats():
    global stats, unique_users
    # Сначала пробуем SQLite
    data = db_get("stats")
    if data is not None:
        try:
            raw = data
            raw["users_today"] = set(raw.get("users_today", []))
            raw["users_yesterday"] = set(raw.get("users_yesterday", []))
            for date, d in raw.get("daily", {}).items():
                d["users"] = set(d.get("users", []))
            stats.update(raw)
            logger.info(f"Статистика загружена из БД: {stats['total_users']} пользователей")
        except Exception as e:
            logger.error(f"Ошибка восстановления статистики из БД: {e}")
    else:
        # Fallback на JSON
        try:
            if os.path.exists(STATS_FILE):
                with open(STATS_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                raw["users_today"] = set(raw.get("users_today", []))
                raw["users_yesterday"] = set(raw.get("users_yesterday", []))
                for date, d in raw.get("daily", {}).items():
                    d["users"] = set(d.get("users", []))
                stats.update(raw)
                logger.info(f"Статистика загружена из файла: {stats['total_users']} пользователей")
        except Exception as e:
            logger.error(f"Ошибка загрузки статистики: {e}")
    # Unique users
    data = db_get("unique_users")
    if data is not None:
        try:
            unique_users = set(data)
            logger.info(f"Уникальных пользователей загружено из БД: {len(unique_users)}")
        except Exception as e:
            logger.error(f"Ошибка загрузки unique_users из БД: {e}")
    else:
        try:
            if os.path.exists(UNIQUE_USERS_FILE):
                with open(UNIQUE_USERS_FILE, "r", encoding="utf-8") as f:
                    unique_users = set(json.load(f))
                logger.info(f"Уникальных пользователей загружено из файла: {len(unique_users)}")
        except Exception as e:
            logger.error(f"Ошибка загрузки unique_users: {e}")

def save_stats():
    try:
        save_data = dict(stats)
        save_data["users_today"] = list(stats.get("users_today", set()))
        save_data["users_yesterday"] = list(stats.get("users_yesterday", set()))
        save_data["daily"] = {}
        for date, d in stats.get("daily", {}).items():
            save_data["daily"][date] = {
                "users": list(d.get("users", set())),
                "views": d.get("views", 0),
                "orders": d.get("orders", 0),
                "questions": d.get("questions", 0),
            }
        # SQLite (primary)
        db_set("stats", save_data)
        db_set("unique_users", list(unique_users))
        # JSON (backup, atomic)
        _atomic_write_json(STATS_FILE, save_data)
        _atomic_write_json(UNIQUE_USERS_FILE, list(unique_users))
    except Exception as e:
        logger.error(f"Ошибка сохранения статистики: {e}")

def _ensure_today():
    """Сбрасывает дневные счётчики если дата сменилась."""
    today = datetime.now().strftime("%Y-%m-%d")
    if stats["today_date"] != today:
        stats["users_yesterday"] = stats.get("users_today", set())
        stats["users_today"] = set()
        stats["today_date"] = today
        cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        for date in list(stats.get("daily", {}).keys()):
            if date < cutoff:
                del stats["daily"][date]

def track_user(user_id):
    """Учитывает уникального пользователя."""
    uid = str(user_id)
    _ensure_today()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not stats["first_contact"]:
        stats["first_contact"] = now_str
    stats["last_contact"] = now_str
    if uid not in unique_users:
        unique_users.add(uid)
        stats["total_users"] = len(unique_users)
    stats["users_today"].add(uid)
    today = stats["today_date"]
    if today not in stats["daily"]:
        stats["daily"][today] = {"users": set(), "views": 0, "orders": 0, "questions": 0}
    stats["daily"][today]["users"].add(uid)

def track_event(event_type, count=1):
    """Учитывает событие."""
    _ensure_today()
    today = stats["today_date"]
    if today not in stats["daily"]:
        stats["daily"][today] = {"users": set(), "views": 0, "orders": 0, "questions": 0}
    if event_type == "catalog_view":
        stats["catalog_views"] += count
        stats["daily"][today]["views"] += count
    elif event_type == "category_view":
        stats["category_views"] += count
        stats["daily"][today]["views"] += count
    elif event_type == "product_view":
        stats["product_views"] += count
        stats["daily"][today]["views"] += count
    elif event_type == "cart_add":
        stats["cart_adds"] += count
    elif event_type == "quick_order":
        stats["quick_orders"] += count
        stats["daily"][today]["orders"] += count
    elif event_type == "cart_order":
        stats["cart_orders"] += count
        stats["daily"][today]["orders"] += count
    elif event_type == "question":
        stats["questions"] += count
        stats["daily"][today]["questions"] += count
    elif event_type == "comment_forwarded":
        stats["comments_forwarded"] += count
    elif event_type == "deal_closed":
        stats["deals_closed"] += count
# === СОСТОЯНИЕ ===
# Fix 2: RLock для защиты CATALOG_DATA (рекурсивная блокировка)
_lock = threading.RLock()
pending_replies: dict = {}
user_carts: dict = {}
question_counter = 0
active_dialogs: dict = {}
pending_orders: dict = {}
order_counter = 0

STATE_FILES = {
    "pending_replies": os.path.join(STATE_DIR, "pending_replies.json"),
    "user_carts": os.path.join(STATE_DIR, "user_carts.json"),
    "active_dialogs": os.path.join(STATE_DIR, "active_dialogs.json"),
    "question_counter": os.path.join(STATE_DIR, "question_counter.json"),
    "pending_orders": os.path.join(STATE_DIR, "pending_orders.json"),
    "order_counter": os.path.join(STATE_DIR, "order_counter.json"),
}


def save_state():
    try:
        # SQLite (primary)
        db_set("pending_replies", _strip_for_save(pending_replies))
        db_set("user_carts", user_carts)
        db_set("active_dialogs", {str(k): v for k, v in active_dialogs.items()})
        db_set("question_counter", {"value": question_counter})
        db_set("pending_orders", {str(k): v for k, v in pending_orders.items()})
        db_set("order_counter", {"value": order_counter})
        # JSON (backup, atomic writes — Fix 6)
        _atomic_write_json(STATE_FILES["pending_replies"], _strip_for_save(pending_replies))
        _atomic_write_json(STATE_FILES["user_carts"], user_carts)
        _atomic_write_json(STATE_FILES["active_dialogs"], {str(k): v for k, v in active_dialogs.items()})
        _atomic_write_json(STATE_FILES["question_counter"], {"value": question_counter})
        _atomic_write_json(STATE_FILES["pending_orders"], {str(k): v for k, v in pending_orders.items()})
        _atomic_write_json(STATE_FILES["order_counter"], {"value": order_counter})
        save_stats()
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


def _load_state_from_json():
    """Fallback: загрузка состояния из JSON-файлов."""
    global question_counter, pending_replies, user_carts, active_dialogs
    global pending_orders, order_counter
    try:
        if os.path.exists(STATE_FILES["pending_replies"]):
            with open(STATE_FILES["pending_replies"], "r", encoding="utf-8") as f:
                pending_replies = json.load(f)
            logger.info(f"Загружено pending_replies из файла: {len(pending_replies)} записей")
    except Exception as e:
        logger.error(f"Ошибка загрузки pending_replies: {e}")

    try:
        if os.path.exists(STATE_FILES["user_carts"]):
            with open(STATE_FILES["user_carts"], "r", encoding="utf-8") as f:
                user_carts = json.load(f)
            logger.info(f"Загружено user_carts из файла: {len(user_carts)} записей")
    except Exception as e:
        logger.error(f"Ошибка загрузки user_carts: {e}")

    try:
        if os.path.exists(STATE_FILES["active_dialogs"]):
            with open(STATE_FILES["active_dialogs"], "r", encoding="utf-8") as f:
                raw = json.load(f)
                active_dialogs = {int(k): v for k, v in raw.items()}
            logger.info(f"Загружено active_dialogs из файла: {len(active_dialogs)} записей")
    except Exception as e:
        logger.error(f"Ошибка загрузки active_dialogs: {e}")

    try:
        if os.path.exists(STATE_FILES["question_counter"]):
            with open(STATE_FILES["question_counter"], "r", encoding="utf-8") as f:
                question_counter = json.load(f).get("value", 0)
            logger.info(f"Загружен question_counter из файла: {question_counter}")
    except Exception as e:
        logger.error(f"Ошибка загрузки question_counter: {e}")

    try:
        if os.path.exists(STATE_FILES["pending_orders"]):
            with open(STATE_FILES["pending_orders"], "r", encoding="utf-8") as f:
                raw = json.load(f)
                pending_orders = {int(k): v for k, v in raw.items()}
            logger.info(f"Загружено pending_orders из файла: {len(pending_orders)} записей")
    except Exception as e:
        logger.error(f"Ошибка загрузки pending_orders: {e}")

    try:
        if os.path.exists(STATE_FILES["order_counter"]):
            with open(STATE_FILES["order_counter"], "r", encoding="utf-8") as f:
                order_counter = json.load(f).get("value", 0)
            logger.info(f"Загружен order_counter из файла: {order_counter}")
    except Exception as e:
        logger.error(f"Ошибка загрузки order_counter: {e}")


def load_state():
    global question_counter, pending_replies, user_carts, active_dialogs
    global pending_orders, order_counter

    loaded_from_db = False

    # Сначала пробуем SQLite (Fix 5)
    if _db_conn:
        data = db_get("pending_replies")
        if data is not None:
            pending_replies = data
            logger.info(f"Загружено pending_replies из БД: {len(pending_replies)} записей")
            loaded_from_db = True

        data = db_get("user_carts")
        if data is not None:
            user_carts = data
            logger.info(f"Загружено user_carts из БД: {len(user_carts)} записей")

        data = db_get("active_dialogs")
        if data is not None:
            active_dialogs = {int(k): v for k, v in data.items()}
            logger.info(f"Загружено active_dialogs из БД: {len(active_dialogs)} записей")

        data = db_get("question_counter")
        if data is not None:
            question_counter = data.get("value", 0)
            logger.info(f"Загружен question_counter из БД: {question_counter}")

        data = db_get("pending_orders")
        if data is not None:
            pending_orders = {int(k): v for k, v in data.items()}
            logger.info(f"Загружено pending_orders из БД: {len(pending_orders)} записей")

        data = db_get("order_counter")
        if data is not None:
            order_counter = data.get("value", 0)
            logger.info(f"Загружен order_counter из БД: {order_counter}")

    if not loaded_from_db:
        _load_state_from_json()

    # Восстановление item для waiting_contact_quick
    for uid, state in pending_replies.items():
        if isinstance(state, dict) and state.get("step") == "waiting_contact_quick" and "item_id" in state:
            item = find_item_by_id(state["item_id"])
            if item:
                state["item"] = item
                logger.info(f"Восстановлен item для pending_replies[{uid}]")
            else:
                # Fix 3: товар удалён — помечаем сессию как невалидную
                logger.warning(f"Товар {state['item_id']} не найден для pending_replies[{uid}] — сессия будет очищена")
                state["item"] = None


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
CATALOG_FILE = os.path.join(os.path.dirname(__file__), "catalog.json")
CATALOG_BACKUP = os.path.join(os.path.dirname(__file__), "catalog_backup.json")


def load_catalog():
    """ТОЛЬКО ЧТЕНИЕ. Сначала пробует S3, потом локальный файл."""
    if S3_ENABLED:
        s3_data = s3_download_catalog()
        if s3_data and isinstance(s3_data, dict) and "categories" in s3_data:
            try:
                content = json.dumps(s3_data, ensure_ascii=False, indent=2)
                _atomic_write_file(CATALOG_FILE, content)
                logger.info("Каталог из S3 сохранён локально")
            except Exception as e:
                logger.warning(f"Не удалось сохранить S3-каталог локально: {e}")
            cats = s3_data.get("categories", [])
            total_items = sum(len(c.get("items", [])) for c in cats)
            logger.info(f"Каталог загружен из S3: {len(cats)} категорий, {total_items} товаров.")
            return s3_data
        else:
            logger.warning("S3: каталог не найден или невалиден — fallback на локальный файл")
    try:
        with open(CATALOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        cats = data.get("categories", [])
        total_items = sum(len(c.get("items", [])) for c in cats)
        logger.info(f"Каталог загружен из файла: {len(cats)} категорий, {total_items} товаров.")
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
    Создаёт резервную копию. НЕ перезаписывает если каталог пуст (защита).
    Также загружает в S3 если настроен.
    Fix 2: использует _lock для защиты от гонок.
    Fix 6: атомарная запись."""
    with _lock:
        cats = CATALOG_DATA.get("categories", [])
        if not cats:
            logger.warning("save_catalog: каталог пуст — НЕ сохраняю (защита от потери данных)!")
            return False
        # Резервная копия
        if os.path.exists(CATALOG_FILE):
            try:
                shutil.copy2(CATALOG_FILE, CATALOG_BACKUP)
                logger.info("Создана резервная копия catalog_backup.json")
            except Exception as e:
                logger.error(f"Не удалось создать резервную копию: {e}")
        try:
            content = json.dumps(CATALOG_DATA, ensure_ascii=False, indent=2)
            _atomic_write_file(CATALOG_FILE, content)
            logger.info(f"Каталог сохранён: {len(cats)} категорий.")
            rebuild_catalog_index()
            if S3_ENABLED:
                ok, msg = s3_upload_catalog()
                if ok:
                    logger.info("Каталог синхронизирован с S3")
                else:
                    logger.warning(f"Не удалось загрузить в S3: {msg}")
            return True
        except Exception as e:
            logger.error(f"Ошибка сохранения каталога: {e}")
            return False


# Загружаем каталог
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


# Транслитерация для генерации id
TRANSLIT = {
    'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'e','ж':'zh','з':'z',
    'и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r',
    'с':'s','т':'t','у':'u','ф':'f','х':'h','ц':'ts','ч':'ch','ш':'sh','щ':'sch',
    'ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
}

def make_slug(text):
    """Генерирует slug из русского текста."""
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


def get_stock_text(item):
    """Возвращает текст наличия для карточки товара."""
    stock = item.get("stock", 0)
    production_days = item.get("production_days", 0)
    if "stock" not in item and "production_days" not in item:
        return ""
    if stock > 0:
        return f"\n\U00002705 В наличии: {stock} \u0448\u0442."
    elif production_days > 0:
        return f"\n\U000023F3 \u041F\u043E\u0434 \u0437\u0430\u043A\u0430\u0437. \u0421\u0440\u043E\u043A \u0438\u0437\u0433\u043E\u0442\u043E\u0432\u043B\u0435\u043D\u0438\u044F: \u043E\u0442 {production_days} \u0434\u043D."
    else:
        return f"\n\U000023F3 \u041F\u043E\u0434 \u0437\u0430\u043A\u0430\u0437"

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
        resp = _HTTP_SESSION.request(method, f"{API_URL}{endpoint}", headers=headers, **kwargs)
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
        {"name": "синхронизировать", "description": "Загрузить каталог в облако (админ)"},
        {"name": "статистика", "description": "Статистика бота (админ)"},
        {"name": "сделка", "description": "Закрыть или отменить заказ (админ)"},
        {"name": "отменить", "description": "Отменить заказ, вернуть товар (админ)"},
    ]
    resp = api_request("PATCH", "/me/commands", json={"commands": commands})
    if resp:
        logger.info(f"Регистрация команд: {resp.text}")


def update_webhook_subscription():
    if not WEBHOOK_URL:
        logger.warning("WEBHOOK_URL не задан — пропускаем обновление подписки")
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

def send_product_card(user_id, item, cat_index=None):
    track_event("product_view")
    stock_text = get_stock_text(item)
    text = (
        f"\U0001FA91 \"{item['name']}\"\n"
        f"\U0001F4B0 Цена: {item['price']} руб.\n"
        f"\U0001F4DD {item['description']}"
        f"{stock_text}\n\n"
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
        ],
    ]
    # Fix 14: кнопка возврата к категориям
    keyboard_buttons[1].append(
        {"type": "callback", "text": "\u21A9\uFE0F К категориям", "payload": "back_to_categories"}
    )
    send_message(user_id=user_id, text=text, attachments=attachments, keyboard=keyboard_buttons)


def show_catalog(user_id):
    track_event("catalog_view")
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
    from collections import Counter
    item_counts = Counter(cart)
    cart_text = "\U0001F6D2 Ваша корзина:\n\n"
    total = 0
    for item_id, qty in item_counts.items():
        item = find_item_by_id(item_id)
        if item:
            stock_text = get_stock_text(item)
            cart_text += f"\u2022 \"{item['name']}\" — {item['price']} руб. \u00d7 {qty}{stock_text}\n"
            total += item["price"] * qty
    cart_text += f"\n\U0001F4B0 Итого: {total} руб.\n\n"
    keyboard_buttons = []
    # Кнопки + и - для каждого товара
    for item_id in item_counts:
        item = find_item_by_id(item_id)
        if item:
            keyboard_buttons.append([
                {"type": "callback", "text": "\u2795", "payload": f"cart_add_one:{item_id}"},
                {"type": "callback", "text": f"{item['name']} \u00d7 {item_counts[item_id]}", "payload": f"noop:{item_id}"},
                {"type": "callback", "text": "\u2796", "payload": f"cart_remove_one:{item_id}"},
            ])
    # Кнопки управления
    keyboard_buttons.append([
        {"type": "callback", "text": "\u2705 Оформить заказ", "payload": "start_checkout"},
        {"type": "callback", "text": "\U0001F5D1 Очистить", "payload": "clear_cart"},
    ])
    keyboard_buttons.append([
        {"type": "message", "text": "\U0001F4CB Каталог", "payload": "\U0001F4CB Каталог"},
    ])
    send_message(user_id=user_id, text=cart_text + "\U0001F447Чтобы продолжить, нажмите кнопку\U0001F447", keyboard=keyboard_buttons)


# === ВАЛИДАЦИЯ ===
def validate_phone(text):
    if not text or len(text.strip()) < 3:
        return False, "Слишком короткое сообщение. Напишите номер телефона."
    phone_match = re.search(r"[\d\+\-\(\)\s]{7,}", text)
    if not phone_match:
        return False, "Не вижу номер телефона. Напишите номер, например: 89001234567."
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
        stock = item.get("stock", 0)
        stock_icon = "\U00002705" if stock > 0 else "\U000023F3"
        buttons.append([{"type": "callback", "text": f"{stock_icon} {item['name']} — {item['price']} руб.", "payload": f"{prefix}:{cat_index}:{i}"}])
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
            {"type": "callback", "text": "\U00002705 Наличие", "payload": f"admin_edit_field:{cat_index}:{item_index}:stock"},
            {"type": "callback", "text": "\U000023F3 Срок изг.", "payload": f"admin_edit_field:{cat_index}:{item_index}:production_days"},
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
            stock = item.get("stock", 0)
            pdays = item.get("production_days", 0)
            if "stock" in item:
                stock_info = f" | \U00002705 {stock} шт." if stock > 0 else (f" | \U000023F3 от {pdays} дн." if pdays > 0 else " | \U000023F3 под заказ")
            else:
                stock_info = ""
            lines.append(f"  \u2022 {item['name']} — {item['price']} руб. (id: {item['id']}){stock_info}")
    send_message(user_id=user_id, text="\n".join(lines))


def admin_export_catalog(user_id):
    """Отправляет содержимое catalog.json. Если S3 настроен — ссылку, иначе текстом частями."""
    if not is_admin(user_id):
        return
    if S3_ENABLED:
        url = s3_get_presigned_url(expires=3600)
        if url:
            send_message(
                user_id=user_id,
                text=f"\U0001F4E4 Экспорт каталога\n\nСсылка для скачивания catalog.json (действительна 1 час):\n\n{url}",
            )
            return
        else:
            send_message(user_id=user_id, text="\u26A0\uFE0F Не удалось сгенерировать ссылку. Отправляю текстом:")
    try:
        with open(CATALOG_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        max_len = 3000
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
                logger.info(f"Экспорт: часть {i+1}/{len(parts)}, длина={len(part)}")
                if i == 0:
                    send_message(user_id=user_id, text=f"\U0001F4E4 Экспорт каталога (часть {i+1}/{len(parts)})\n\n```\n{part}")
                elif i == len(parts) - 1:
                    send_message(user_id=user_id, text=f"\U0001F4E4 (часть {i+1}/{len(parts)})\n\n{part}\n```")
                else:
                    send_message(user_id=user_id, text=f"\U0001F4E4 (часть {i+1}/{len(parts)})\n\n{part}")
                if i < len(parts) - 1:
                    time.sleep(0.8)
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


def validate_catalog(data):
    """Fix 17: валидация структуры импортируемого каталога."""
    if not isinstance(data, dict) or "categories" not in data:
        return False, "Нет ключа 'categories'"
    for cat in data.get("categories", []):
        if not isinstance(cat, dict) or "name" not in cat:
            return False, "Категория без названия"
        for item in cat.get("items", []):
            if not isinstance(item, dict) or "name" not in item:
                return False, f"Товар без названия в категории {cat.get('name','?')}"
            if "price" not in item or not isinstance(item["price"], (int, float)):
                return False, f"Товар '{item.get('name','?')}' без корректной цены"
            if "id" not in item:
                item["id"] = make_slug(item["name"])
    return True, ""


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


def admin_sync_catalog(user_id):
    """Ручная синхронизация каталога с S3."""
    if not is_admin(user_id):
        return
    if not S3_ENABLED:
        send_message(user_id=user_id, text="\u26A0\uFE0F S3 не настроен. Задайте переменные YANDEX_S3_BUCKET, YANDEX_S3_ACCESS_KEY, YANDEX_S3_SECRET_KEY.")
        return
    ok, msg = s3_upload_catalog()
    if ok:
        send_message(user_id=user_id, text="\u2705 Каталог загружен в Yandex Object Storage.", keyboard=build_main_menu_keyboard())
    else:
        send_message(user_id=user_id, text=f"\u274C Не удалось загрузить каталог в S3: {msg}", keyboard=build_main_menu_keyboard())


def admin_show_stats(user_id):
    """Показывает статистику бота."""
    if not is_admin(user_id):
        return
    _ensure_today()
    today = stats["today_date"]
    yesterday_users = len(stats.get("users_yesterday", set()))
    today_users = len(stats.get("users_today", set()))
    total_orders = stats["quick_orders"] + stats["cart_orders"]

    lines = [
        "\U0001F4CA Статистика бота\n",
        "\U0001F465 Пользователи:",
        f"  Уникальных всего: {stats['total_users']}",
        f"  Активных сегодня: {today_users}",
        f"  Активных вчера: {yesterday_users}",
        "",
        "\U0001F4CB Каталог:",
        f"  Просмотры каталога: {stats['catalog_views']}",
        f"  Просмотры категорий: {stats['category_views']}",
        f"  Просмотры товаров: {stats['product_views']}",
        "",
        "\U0001F6D2 Корзина и заказы:",
        f"  Добавлений в корзину: {stats['cart_adds']}",
        f"  Быстрых заказов: {stats['quick_orders']}",
        f"  Заказов из корзины: {stats['cart_orders']}",
        f"  Всего заказов: {total_orders}",
        f"  Сделок закрыто: {stats['deals_closed']}",
        "",
        "\u2753 Вопросы и комментарии:",
        f"  Вопросов мастеру: {stats['questions']}",
        f"  Активных диалогов: {len(active_dialogs)}",
        f"  Переслано комментариев: {stats['comments_forwarded']}",
        "",
        "\U0001F4C5 Период работы:",
        f"  Первый контакт: {stats['first_contact'] or '—'}",
        f"  Последний контакт: {stats['last_contact'] or '—'}",
    ]

    lines.append("\n\U0001F4C8 Последние 7 дней:")
    lines.append("  Дата        | Польз. | Просм. | Заказ. | Вопр.")
    lines.append("  " + "-" * 45)
    for i in range(6, -1, -1):
        date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        d = stats.get("daily", {}).get(date, {})
        u = len(d.get("users", set()))
        v = d.get("views", 0)
        o = d.get("orders", 0)
        q = d.get("questions", 0)
        lines.append(f"  {date} | {u:6d} | {v:6d} | {o:6d} | {q:5d}")

    send_message(user_id=user_id, text="\n".join(lines))


def admin_close_deal_start(user_id):
    """Показывает список pending-заказов для закрытия сделки."""
    if not is_admin(user_id):
        return
    if not pending_orders:
        send_message(user_id=user_id, text="\u26A0\uFE0F Нет открытых заказов. Заказы появятся, когда клиенты оформят заказ через бота.", keyboard=build_main_menu_keyboard())
        return
    buttons = []
    for num, order in sorted(pending_orders.items()):
        if order.get("status") != "pending":
            continue
        items_summary = ", ".join(f"{it['name']} x{it['qty']}" for it in order.get("items", []))
        preview = items_summary[:40] + ("..." if len(items_summary) > 40 else "")
        buttons.append([{"type": "callback", "text": f"#{num} — {preview}", "payload": f"admin_close_deal:{num}"}])
    buttons.append([{"type": "callback", "text": "\u274C Отмена", "payload": "cancel_action"}])
    send_message(user_id=user_id, text="\U0001F91D Закрытие сделки\n\nВыберите заказ для закрытия:", keyboard=buttons)


def admin_close_deal_confirm(user_id, order_id):
    """Показывает детали заказа и просит подтверждения."""
    if not is_admin(user_id):
        return
    order = pending_orders.get(order_id)
    if not order or order.get("status") != "pending":
        send_message(user_id=user_id, text=f"\u26A0\uFE0F Заказ #{order_id} не найден или уже обработан.")
        return
    lines = [f"\U0001F4D8 Заказ #{order_id}\n"]
    for it in order.get("items", []):
        item = find_item_by_id(it["id"])
        stock = item.get("stock", 0) if item else 0
        stock_note = f" (на складе: {stock})" if item else ""
        lines.append(f"  \u2022 \"{it['name']}\" x{it['qty']} — {it['price']} руб.{stock_note}")
    lines.append(f"\n\U0001F4DD Телефон: {order.get('contact', '—')}")
    lines.append(f"\u23F1 {datetime.fromtimestamp(order.get('timestamp', 0)).strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"\n\U00002754 Закрыть сделку? Корзина клиента будет очищена.")
    keyboard = [
        [{"type": "callback", "text": "\u2705 Да, закрыть", "payload": f"admin_close_deal_yes:{order_id}"}],
        [{"type": "callback", "text": "\u274C Отменить заказ", "payload": f"admin_cancel_order:{order_id}"}],
        [{"type": "callback", "text": "\u2B05\uFE0F Назад", "payload": "admin_close_deal_start"}],
    ]
    send_message(user_id=user_id, text="\n".join(lines), keyboard=keyboard)


def admin_close_deal_execute(user_id, order_id):
    """Закрывает сделку: помечает заказ закрытым, очищает корзину клиента.
    Товар уже списан при оформлении заказа (_create_order)."""
    if not is_admin(user_id):
        return
    order = pending_orders.get(order_id)
    if not order or order.get("status") != "pending":
        send_message(user_id=user_id, text=f"\u26A0\uFE0F Заказ #{order_id} не найден или уже закрыт.", keyboard=build_main_menu_keyboard())
        return
    client_id = str(order.get("user_id", ""))
    with _lock:
        # Очищаем корзину клиента
        if client_id in user_carts:
            user_carts[client_id] = []
        order["status"] = "closed"
        save_state()
    track_event("deal_closed")
    # Уведомляем админа
    result_lines = [f"\u2705 Сделка #{order_id} закрыта!\n\n\U0001F4E6 Товар уже списан со склада при оформлении заказа."]
    result_lines.append(f"\n\U0001F6D2 Корзина клиента очищена.")
    send_message(user_id=user_id, text="\n".join(result_lines), keyboard=build_main_menu_keyboard())
    # Уведомляем клиента
    if client_id:
        send_message(
            user_id=client_id,
            text="\u2705 Ваш заказ оформлен! Мастерская Игнатьевых благодарит вас.\n\nЕсли захотите заказать что-то ещё — откройте каталог.",
            keyboard=build_main_menu_keyboard(),
        )
    logger.info(f"Сделка #{order_id} закрыта, клиент {client_id}")


def admin_cancel_order_execute(user_id, order_id):
    """Отменяет заказ: возвращает товар на склад, помечает заказ отменённым."""
    if not is_admin(user_id):
        return
    order = pending_orders.get(order_id)
    if not order or order.get("status") != "pending":
        send_message(user_id=user_id, text=f"\u26A0\uFE0F Заказ #{order_id} не найден или уже обработан.", keyboard=build_main_menu_keyboard())
        return
    client_id = str(order.get("user_id", ""))
    stock_changes = []
    with _lock:
        # Возвращаем товар на склад
        for it in order.get("items", []):
            item = find_item_by_id(it["id"])
            if item:
                current = item.get("stock", 0)
                new_stock = current + it["qty"]
                item["stock"] = new_stock
                stock_changes.append(f"  \u2022 \"{it['name']}\": {current} \u2192 {new_stock} \u0448\u0442.")
                logger.info(f"Возврат при отмене: {it['name']} +{it['qty']} (было {current}, стало {new_stock})")
        save_catalog()
        order["status"] = "cancelled"
        save_state()
    # Уведомляем админа
    result_lines = [f"\u274C Заказ #{order_id} отменён!\n\n\U0001F4E6 Товар возвращён на склад:"]
    if stock_changes:
        result_lines.extend(stock_changes)
    else:
        result_lines.append("  (товары не найдены в каталоге)")
    send_message(user_id=user_id, text="\n".join(result_lines), keyboard=build_main_menu_keyboard())
    # Уведомляем клиента
    if client_id:
        send_message(
            user_id=client_id,
            text="\u26A0\uFE0F Ваш заказ отменён. Если у вас есть вопросы — задайте их через бота.",
            keyboard=build_main_menu_keyboard(),
        )
    logger.info(f"Заказ #{order_id} отменён, клиент {client_id}")

# === CALLBACK ОБРАБОТКА ===
# Fix 4: обёртка try/except для защиты от битых/устаревших callback'ов

def handle_callback(data):
    """Точка входа callback-обработки с защитой от ошибок парсинга."""
    try:
        return _handle_callback_inner(data)
    except (IndexError, ValueError, KeyError, TypeError) as e:
        logger.warning(f"Callback error ({type(e).__name__}): {e}")
        callback = data.get("callback", {})
        callback_id = callback.get("callback_id", "")
        sender_id = str(
            callback.get("user", {}).get("user_id", "")
            or data.get("sender", {}).get("user_id", "")
            or data.get("user", {}).get("user_id", "")
        )
        if sender_id:
            answer_callback(callback_id, "\u26A0\uFE0F Данные устарели или недоступны")
            send_main_menu(sender_id)
        else:
            answer_callback(callback_id, "Ошибка")
        return jsonify({"ok": True}), 200


def _handle_callback_inner(data):
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

    track_user(sender_id)

    # --- admin_cancel_order:<order_id> ---
    if payload.startswith("admin_cancel_order:"):
        order_id = int(payload.split(":", 1)[1])
        answer_callback(callback_id, "Отменяю заказ...")
        admin_cancel_order_execute(sender_id, order_id)
        return jsonify({"ok": True}), 200

    # --- cancel_action ---
    if payload == "cancel_action":
        with _lock:
            had = pending_replies.pop(sender_id, None)
            if had:
                save_state()
        answer_callback(callback_id, "Отменено")
        send_main_menu(sender_id)
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
            send_message(user_id=sender_id, text="\u270D\uFE0F Напишите ответ следующим сообщением — бот отправит его как комментарий-ответ.\n\nИли нажмите «Отмена».", keyboard=build_cancel_keyboard())
        else:
            answer_callback(callback_id, "Ошибка: неверный формат")
        return jsonify({"ok": True}), 200

    # --- show_category:<index> ---
    if payload.startswith("show_category:"):
        cat_index = int(payload.split(":", 1)[1])
        categories = CATALOG_DATA.get("categories", [])
        if cat_index < 0 or cat_index >= len(categories):
            answer_callback(callback_id, "Категория не найдена")
            send_main_menu(sender_id)
            return jsonify({"ok": True}), 200
        category = categories[cat_index]
        answer_callback(callback_id, f"Открываю: {category['name']}")
        track_event("category_view")
        items = category.get("items", [])
        if not items:
            send_message(user_id=sender_id, text="В этой категории пока нет товаров.", keyboard=build_catalog_keyboard())
            return jsonify({"ok": True}), 200
        for idx, item in enumerate(items):
            send_product_card(sender_id, item, cat_index=cat_index)
            if idx < len(items) - 1:
                time.sleep(0.3)
        return jsonify({"ok": True}), 200

    # --- back_to_categories ---
    if payload == "back_to_categories":
        answer_callback(callback_id, "К категориям")
        show_catalog(sender_id)
        return jsonify({"ok": True}), 200

    # --- add_to_cart:<item_id> ---
    if payload.startswith("add_to_cart:"):
        item_id = payload.split(":", 1)[1]
        item = find_item_by_id(item_id)
        if not item:
            answer_callback(callback_id, "Товар не найден")
            return jsonify({"ok": True}), 200
        # Fix 13: проверка наличия при добавлении в корзину
        stock = item.get("stock", 0)
        current_in_cart = user_carts.get(sender_id, []).count(item_id)
        if stock > 0 and current_in_cart >= stock:
            answer_callback(callback_id, f"В наличии только {stock} шт.")
            return jsonify({"ok": True}), 200
        with _lock:
            if sender_id not in user_carts:
                user_carts[sender_id] = []
            user_carts[sender_id].append(item_id)
            save_state()
        track_event("cart_add")
        answer_callback(callback_id, "\u2705 Добавлено в корзину!")
        count = len(user_carts[sender_id])
        send_message(
            user_id=sender_id,
            text=f"\U0001F6D2 «{item['name']}» добавлен в корзину.\nВ корзине товаров: {count}\n\n\U0001F447Нажмите кнопку\U0001F447",
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
        stock_text = get_stock_text(item)
        send_message(
            user_id=sender_id,
            text=f"\U0001F4D8 Быстрый заказ: \"{item['name']}\" (Цена: {item['price']} руб.){stock_text}\n\nЧтобы мастер связался с вами \U0001F4A1\n\U0001F4DD Напишите ваш номер телефона — мастер свяжется с вами",
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
        from collections import Counter
        item_counts = Counter(cart)
        total = 0
        items_text = ""
        for item_id, qty in item_counts.items():
            item = find_item_by_id(item_id)
            if item:
                items_text += f"\u2022 \"{item['name']}\" — {item['price']} руб. \u00d7 {qty}\n"
                total += item["price"] * qty
        answer_callback(callback_id, "Начинаем оформление")
        send_message(
            user_id=sender_id,
            text=f"\U0001F6D2 Оформляем заказ:\n\n{items_text}\U0001F4B0 Итого: {total} руб.\n\n\U0001F4DD Напишите ваш номер телефона — мастер свяжется с вами",
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

    # --- clear_cart (показ подтверждения) ---
    if payload == "clear_cart":
        answer_callback(callback_id, "Подтвердите очистку")
        send_message(
            user_id=sender_id,
            text="\u26A0\uFE0F Вы уверены, что хотите очистить корзину?",
            keyboard=[
                [
                    {"type": "callback", "text": "\u2705 Да", "payload": "clear_cart_confirm"},
                    {"type": "callback", "text": "\u274C Нет", "payload": "clear_cart_cancel"},
                ],
            ],
        )
        return jsonify({"ok": True}), 200

    # --- clear_cart_confirm (фактическая очистка) ---
    if payload == "clear_cart_confirm":
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

    # --- clear_cart_cancel (отмена очистки — показать корзину) ---
    if payload == "clear_cart_cancel":
        answer_callback(callback_id, "Отмена")
        show_cart(sender_id)
        return jsonify({"ok": True}), 200

    # --- cart_add_one:<item_id> (добавить ещё один товар в корзину) ---
    if payload.startswith("cart_add_one:"):
        item_id = payload.split(":", 1)[1]
        item = find_item_by_id(item_id)
        if not item:
            answer_callback(callback_id, "Товар не найден")
            return jsonify({"ok": True}), 200
        stock = item.get("stock", 0)
        current_in_cart = user_carts.get(sender_id, []).count(item_id)
        if stock > 0 and current_in_cart >= stock:
            answer_callback(callback_id, f"В наличии только {stock} шт.")
            return jsonify({"ok": True}), 200
        with _lock:
            if sender_id not in user_carts:
                user_carts[sender_id] = []
            user_carts[sender_id].append(item_id)
            save_state()
        answer_callback(callback_id, "\u2795 Добавлено")
        show_cart(sender_id)
        return jsonify({"ok": True}), 200

    # --- cart_remove_one:<item_id> (убрать один товар из корзины) ---
    if payload.startswith("cart_remove_one:"):
        item_id = payload.split(":", 1)[1]
        item = find_item_by_id(item_id)
        if not item:
            answer_callback(callback_id, "Товар не найден")
            return jsonify({"ok": True}), 200
        with _lock:
            cart = user_carts.get(sender_id, [])
            if item_id in cart:
                cart.remove(item_id)
                save_state()
        answer_callback(callback_id, "\u2796 Убрано")
        if not user_carts.get(sender_id, []):
            send_message(
                user_id=sender_id,
                text="\U0001F6D2 Ваша корзина пуста.\n\n\U0001F449 Откройте «\U0001F4CB Каталог» — выберите изделие!",
                keyboard=build_catalog_keyboard(),
            )
        else:
            show_cart(sender_id)
        return jsonify({"ok": True}), 200

    # --- noop:<item_id> (заглушка для кнопки с названием товара) ---
    if payload.startswith("noop:"):
        answer_callback(callback_id, "")
        return jsonify({"ok": True}), 200

    # ========================
    # === АДМИН CALLBACK'и ===
    # ========================
    if not is_admin(sender_id):
        answer_callback(callback_id, "Нет доступа")
        return jsonify({"ok": True}), 200

    # --- admin_close_deal_start ---
    if payload == "admin_close_deal_start":
        answer_callback(callback_id, "Управление заказами")
        admin_close_deal_start(sender_id)
        return jsonify({"ok": True}), 200

    # --- admin_close_deal:<order_id> ---
    if payload.startswith("admin_close_deal:") and not payload.startswith("admin_close_deal_yes:"):
        order_id = int(payload.split(":", 1)[1])
        answer_callback(callback_id, f"Заказ #{order_id}")
        admin_close_deal_confirm(sender_id, order_id)
        return jsonify({"ok": True}), 200

    # --- admin_close_deal_yes:<order_id> ---
    if payload.startswith("admin_close_deal_yes:"):
        order_id = int(payload.split(":", 1)[1])
        answer_callback(callback_id, "Закрываю...")
        admin_close_deal_execute(sender_id, order_id)
        return jsonify({"ok": True}), 200

    # --- admin_add_category_start ---
    if payload == "admin_add_category_start":
        answer_callback(callback_id, "Добавляем категорию")
        send_message(user_id=sender_id, text="\U0001F4E6 Напишите название новой категории:", keyboard=build_cancel_keyboard())
        with _lock:
            pending_replies[sender_id] = {"step": "admin_add_category", "timestamp": time.time()}
            save_state()
        return jsonify({"ok": True}), 200

    # --- admin_add_product_start ---
    if payload == "admin_add_product_start":
        answer_callback(callback_id, "Добавляем товар")
        cats = CATALOG_DATA.get("categories", [])
        if not cats:
            send_message(user_id=sender_id, text="Сначала создайте хотя бы одну категорию.", keyboard=build_admin_add_keyboard())
            return jsonify({"ok": True}), 200
        send_message(user_id=sender_id, text="\U0001F4CB Выберите категорию для нового товара:", keyboard=build_admin_categories_keyboard("admin_add_product_cat"))
        return jsonify({"ok": True}), 200

    # --- admin_add_product_cat:<index> ---
    if payload.startswith("admin_add_product_cat:"):
        cat_index = int(payload.split(":", 1)[1])
        cats = CATALOG_DATA.get("categories", [])
        if cat_index < 0 or cat_index >= len(cats):
            answer_callback(callback_id, "Категория не найдена")
            return jsonify({"ok": True}), 200
        cat = cats[cat_index]
        answer_callback(callback_id, f"Категория: {cat['name']}")
        send_message(user_id=sender_id, text="\U0001F4DD Напишите название изделия:", keyboard=build_cancel_keyboard())
        with _lock:
            pending_replies[sender_id] = {"step": "admin_add_product_name", "cat_index": cat_index, "timestamp": time.time()}
            save_state()
        return jsonify({"ok": True}), 200

    # --- admin_edit_category_start ---
    if payload == "admin_edit_category_start":
        answer_callback(callback_id, "Редактируем категорию")
        cats = CATALOG_DATA.get("categories", [])
        if not cats:
            send_message(user_id=sender_id, text="Категорий нет.", keyboard=build_admin_edit_keyboard())
            return jsonify({"ok": True}), 200
        send_message(user_id=sender_id, text="\u270F\uFE0F Выберите категорию для переименования:", keyboard=build_admin_categories_keyboard("admin_edit_category"))
        return jsonify({"ok": True}), 200

    # --- admin_edit_category:<index> ---
    if payload.startswith("admin_edit_category:"):
        cat_index = int(payload.split(":", 1)[1])
        cats = CATALOG_DATA.get("categories", [])
        if cat_index < 0 or cat_index >= len(cats):
            answer_callback(callback_id, "Категория не найдена")
            return jsonify({"ok": True}), 200
        cat = cats[cat_index]
        answer_callback(callback_id, f"Редактируем: {cat['name']}")
        send_message(user_id=sender_id, text=f"\u270F\uFE0F Текущее название: {cat['name']}\n\nНапишите новое название:", keyboard=build_cancel_keyboard())
        with _lock:
            pending_replies[sender_id] = {"step": "admin_edit_category_name", "cat_index": cat_index, "timestamp": time.time()}
            save_state()
        return jsonify({"ok": True}), 200

    # --- admin_edit_product_start ---
    if payload == "admin_edit_product_start":
        answer_callback(callback_id, "Редактируем товар")
        cats = CATALOG_DATA.get("categories", [])
        if not cats:
            send_message(user_id=sender_id, text="Категорий нет.", keyboard=build_admin_edit_keyboard())
            return jsonify({"ok": True}), 200
        send_message(user_id=sender_id, text="\u270F\uFE0F Выберите категорию:", keyboard=build_admin_categories_keyboard("admin_edit_product_cat"))
        return jsonify({"ok": True}), 200

    # --- admin_edit_product_cat:<index> ---
    if payload.startswith("admin_edit_product_cat:"):
        cat_index = int(payload.split(":", 1)[1])
        cats = CATALOG_DATA.get("categories", [])
        if cat_index < 0 or cat_index >= len(cats):
            answer_callback(callback_id, "Категория не найдена")
            return jsonify({"ok": True}), 200
        cat = cats[cat_index]
        answer_callback(callback_id, f"Категория: {cat['name']}")
        if not cat.get("items"):
            send_message(user_id=sender_id, text="В этой категории нет товаров.", keyboard=build_admin_edit_keyboard())
            return jsonify({"ok": True}), 200
        send_message(user_id=sender_id, text="\U0001F4CB Товары в категории:", keyboard=build_admin_products_keyboard(cat_index, "admin_edit_product"))
        return jsonify({"ok": True}), 200

    # --- admin_edit_product:<cat_index>:<item_index> ---
    if payload.startswith("admin_edit_product:"):
        parts = payload.split(":", 2)
        cat_index, item_index = int(parts[1]), int(parts[2])
        cats = CATALOG_DATA.get("categories", [])
        if cat_index < 0 or cat_index >= len(cats):
            answer_callback(callback_id, "Категория не найдена")
            return jsonify({"ok": True}), 200
        items = cats[cat_index].get("items", [])
        if item_index < 0 or item_index >= len(items):
            answer_callback(callback_id, "Товар не найден")
            return jsonify({"ok": True}), 200
        item = items[item_index]
        answer_callback(callback_id, f"Редактируем: {item['name']}")
        stock_text = get_stock_text(item)
        text = (
            f"\u270F\uFE0F Редактирование товара\n\n"
            f"\U0001FA91 {item['name']}\n"
            f"\U0001F4B0 Цена: {item['price']} руб.\n"
            f"\U0001F4DD {item.get('description', '')}\n"
            f"\U0001F4D8 Фото: {'есть' if item.get('photo_url') else 'нет'}"
            f"{stock_text}\n"
            f"\U0001F194 id: {item['id']}\n\n"
            f"Что изменить?"
        )
        # Fix 15: отправляем текущее фото как attachment
        attachments = []
        if item.get("photo_url"):
            attachments.append({"type": "image", "payload": {"url": item["photo_url"]}})
        send_message(user_id=sender_id, text=text, attachments=attachments, keyboard=build_admin_product_edit_fields_keyboard(cat_index, item_index))
        return jsonify({"ok": True}), 200

    # --- admin_edit_field:<cat_index>:<item_index>:<field> ---
    if payload.startswith("admin_edit_field:"):
        parts = payload.split(":", 3)
        cat_index, item_index, field = int(parts[1]), int(parts[2]), parts[3]
        cats = CATALOG_DATA.get("categories", [])
        if cat_index < 0 or cat_index >= len(cats):
            answer_callback(callback_id, "Категория не найдена")
            return jsonify({"ok": True}), 200
        items = cats[cat_index].get("items", [])
        if item_index < 0 or item_index >= len(items):
            answer_callback(callback_id, "Товар не найден")
            return jsonify({"ok": True}), 200
        item = items[item_index]
        field_names = {
            "name": "название",
            "price": "цену (только число, в рублях)",
            "description": "описание",
            "photo_url": "ссылку на фото (или «нет» чтобы убрать)",
            "stock": "количество в наличии (число, 0 = под заказ)",
            "production_days": "срок изготовления в днях (число, 0 = не указан)",
        }
        answer_callback(callback_id, f"Изменяем: {field_names.get(field, field)}")
        current = item.get(field, "")
        if field == "stock" and "stock" not in item:
            current = "не задано (0)"
        if field == "production_days" and "production_days" not in item:
            current = "не задано (0)"
        send_message(
            user_id=sender_id,
            text=f"\u270F\uFE0F Изменение: {field_names.get(field, field)}\n\nТекущее значение: {current}\n\n\U0001F4DD Напишите новое значение:",
            keyboard=build_cancel_keyboard(),
        )
        with _lock:
            pending_replies[sender_id] = {"step": "admin_edit_field_value", "cat_index": cat_index, "item_index": item_index, "field": field, "timestamp": time.time()}
            save_state()
        return jsonify({"ok": True}), 200

    # --- admin_del_category_start ---
    if payload == "admin_del_category_start":
        answer_callback(callback_id, "Удаляем категорию")
        cats = CATALOG_DATA.get("categories", [])
        if not cats:
            send_message(user_id=sender_id, text="Категорий нет.", keyboard=build_admin_edit_keyboard())
            return jsonify({"ok": True}), 200
        send_message(user_id=sender_id, text="\U0001F4E6 Выберите категорию для удаления:", keyboard=build_admin_categories_keyboard("admin_del_category"))
        return jsonify({"ok": True}), 200

    # --- admin_del_category:<index> ---
    if payload.startswith("admin_del_category:"):
        cat_index = int(payload.split(":", 1)[1])
        cats = CATALOG_DATA.get("categories", [])
        if cat_index < 0 or cat_index >= len(cats):
            answer_callback(callback_id, "Категория не найдена")
            return jsonify({"ok": True}), 200
        cat = cats[cat_index]
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

    # --- admin_del_category_confirm:<index> ---
    if payload.startswith("admin_del_category_confirm:"):
        cat_index = int(payload.split(":", 1)[1])
        cats = CATALOG_DATA.get("categories", [])
        if cat_index < 0 or cat_index >= len(cats):
            answer_callback(callback_id, "Категория не найдена")
            return jsonify({"ok": True}), 200
        cat = cats[cat_index]
        answer_callback(callback_id, "Удалено")
        # Fix 2: блокировка при модификации каталога
        with _lock:
            del CATALOG_DATA["categories"][cat_index]
            save_catalog()
        send_message(user_id=sender_id, text=f"\u2705 Категория «{cat['name']}» удалена.", keyboard=build_admin_edit_keyboard())
        return jsonify({"ok": True}), 200

    # --- admin_del_product_start ---
    if payload == "admin_del_product_start":
        answer_callback(callback_id, "Удаляем товар")
        cats = CATALOG_DATA.get("categories", [])
        if not cats:
            send_message(user_id=sender_id, text="Категорий нет.", keyboard=build_admin_edit_keyboard())
            return jsonify({"ok": True}), 200
        send_message(user_id=sender_id, text="\U0001F4CB Выберите категорию:", keyboard=build_admin_categories_keyboard("admin_del_product_cat"))
        return jsonify({"ok": True}), 200

    # --- admin_del_product_cat:<index> ---
    if payload.startswith("admin_del_product_cat:"):
        cat_index = int(payload.split(":", 1)[1])
        cats = CATALOG_DATA.get("categories", [])
        if cat_index < 0 or cat_index >= len(cats):
            answer_callback(callback_id, "Категория не найдена")
            return jsonify({"ok": True}), 200
        cat = cats[cat_index]
        answer_callback(callback_id, f"Категория: {cat['name']}")
        if not cat.get("items"):
            send_message(user_id=sender_id, text="В этой категории нет товаров.", keyboard=build_admin_edit_keyboard())
            return jsonify({"ok": True}), 200
        send_message(user_id=sender_id, text="\U0001F4CB Выберите товар для удаления:", keyboard=build_admin_products_keyboard(cat_index, "admin_del_product"))
        return jsonify({"ok": True}), 200

    # --- admin_del_product:<cat_index>:<item_index> ---
    if payload.startswith("admin_del_product:") and not payload.startswith("admin_del_product_cat:") and not payload.startswith("admin_del_product_confirm:"):
        parts = payload.split(":", 2)
        cat_index, item_index = int(parts[1]), int(parts[2])
        cats = CATALOG_DATA.get("categories", [])
        if cat_index < 0 or cat_index >= len(cats):
            answer_callback(callback_id, "Категория не найдена")
            return jsonify({"ok": True}), 200
        items = cats[cat_index].get("items", [])
        if item_index < 0 or item_index >= len(items):
            answer_callback(callback_id, "Товар не найден")
            return jsonify({"ok": True}), 200
        item = items[item_index]
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

    # --- admin_del_product_confirm:<cat_index>:<item_index> ---
    if payload.startswith("admin_del_product_confirm:"):
        parts = payload.split(":", 2)
        cat_index, item_index = int(parts[1]), int(parts[2])
        cats = CATALOG_DATA.get("categories", [])
        if cat_index < 0 or cat_index >= len(cats):
            answer_callback(callback_id, "Категория не найдена")
            return jsonify({"ok": True}), 200
        items = cats[cat_index].get("items", [])
        if item_index < 0 or item_index >= len(items):
            answer_callback(callback_id, "Товар не найден")
            return jsonify({"ok": True}), 200
        item = items[item_index]
        answer_callback(callback_id, "Удалено")
        # Fix 2: блокировка при модификации каталога
        with _lock:
            del CATALOG_DATA["categories"][cat_index]["items"][item_index]
            save_catalog()
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

    # Проверка timeout
    if "timestamp" in state and time.time() - state["timestamp"] > SESSION_TIMEOUT:
        with _lock:
            del pending_replies[sender_id]
            save_state()
        send_message(user_id=sender_id, text="\u23F1\uFE0F Время ожидания истекло. Начните заново.", keyboard=build_main_menu_keyboard())
        return True

    # --- admin_add_category ---
    if step == "admin_add_category":
        name = (text or "").strip()
        if not name:
            send_message(user_id=sender_id, text="Название не может быть пустым. Напишите название:")
            return True
        # Fix 2: блокировка при работе с каталогом
        with _lock:
            for cat in CATALOG_DATA.get("categories", []):
                if cat["name"].lower() == name.lower():
                    send_message(user_id=sender_id, text=f"\u26A0\uFE0F Категория «{name}» уже существует. Напишите другое название:")
                    return True
            CATALOG_DATA.setdefault("categories", []).append({"name": name, "items": []})
            save_catalog()
            del pending_replies[sender_id]
            save_state()
        send_message(
            user_id=sender_id,
            text=f"\u2705 Категория «{name}» добавлена!\n\nТеперь можно добавить в неё товары.",
            keyboard=build_admin_add_keyboard(),
        )
        return True

    # --- admin_add_product_name ---
    if step == "admin_add_product_name":
        name = (text or "").strip()
        if not name:
            send_message(user_id=sender_id, text="Название не может быть пустым. Напишите название:")
            return True
        with _lock:
            pending_replies[sender_id]["name"] = name
            pending_replies[sender_id]["step"] = "admin_add_product_price"
            pending_replies[sender_id]["timestamp"] = time.time()
            save_state()
        send_message(user_id=sender_id, text="\U0001F4B0 Напишите цену в рублях (только число):", keyboard=build_cancel_keyboard())
        return True

    # --- admin_add_product_price ---
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

    # --- admin_add_product_desc ---
    if step == "admin_add_product_desc":
        desc = (text or "").strip()
        if not desc:
            send_message(user_id=sender_id, text="Описание не может быть пустым. Напишите описание:")
            return True
        with _lock:
            pending_replies[sender_id]["description"] = desc
            pending_replies[sender_id]["step"] = "admin_add_product_photo"
            pending_replies[sender_id]["timestamp"] = time.time()
            save_state()
        send_message(user_id=sender_id, text="\U0001F4D8 Отправьте ссылку на фото. Если фото нет — напишите «нет»:", keyboard=build_cancel_keyboard())
        return True

    # --- admin_add_product_photo ---
    if step == "admin_add_product_photo":
        photo = (text or "").strip()
        if photo.lower() in ("нет", "no", "-", "нету"):
            photo = ""
        with _lock:
            pending_replies[sender_id]["photo"] = photo
            pending_replies[sender_id]["step"] = "admin_add_product_stock"
            pending_replies[sender_id]["timestamp"] = time.time()
            save_state()
        send_message(user_id=sender_id, text="\U00002705 Напишите количество товара в наличии (число).\n\n0 — товар под заказ.", keyboard=build_cancel_keyboard())
        return True

    # --- admin_add_product_stock ---
    if step == "admin_add_product_stock":
        stock_text = (text or "").strip()
        try:
            stock = int(stock_text)
            if stock < 0:
                raise ValueError
        except ValueError:
            send_message(user_id=sender_id, text="\u26A0\uFE0F Некорректное количество. Напишите число, например: 5 или 0 (под заказ).")
            return True
        with _lock:
            pending_replies[sender_id]["stock"] = stock
            pending_replies[sender_id]["step"] = "admin_add_product_production_days"
            pending_replies[sender_id]["timestamp"] = time.time()
            save_state()
        send_message(user_id=sender_id, text="\U000023F3 Напишите срок изготовления в днях (число).\n\nНапример: 7 — «от 7 дней». Если не указано — напишите 0.", keyboard=build_cancel_keyboard())
        return True

    # --- admin_add_product_production_days ---
    if step == "admin_add_product_production_days":
        pdays_text = (text or "").strip()
        try:
            production_days = int(pdays_text)
            if production_days < 0:
                raise ValueError
        except ValueError:
            send_message(user_id=sender_id, text="\u26A0\uFE0F Некорректный срок. Напишите число, например: 7 или 0.")
            return True
        # Fix 2: блокировка при модификации каталога
        with _lock:
            cat_index = state["cat_index"]
            name = state["name"]
            price = state["price"]
            desc = state["description"]
            photo = state.get("photo", "")
            stock = state["stock"]
            item_id = make_slug(name)
            item = {
                "id": item_id,
                "name": name,
                "price": price,
                "description": desc,
                "photo_url": photo,
                "stock": stock,
                "production_days": production_days,
            }
            CATALOG_DATA["categories"][cat_index].setdefault("items", []).append(item)
            save_catalog()
            del pending_replies[sender_id]
            save_state()
        stock_display = f"\U00002705 В наличии: {stock} \u0448\u0442." if stock > 0 else (f"\U000023F3 \u041F\u043E\u0434 \u0437\u0430\u043A\u0430\u0437. \u0421\u0440\u043E\u043A: \u043E\u0442 {production_days} \u0434\u043D." if production_days > 0 else "\U000023F3 \u041F\u043E\u0434 \u0437\u0430\u043A\u0430\u0437")
        preview = f"\U0001FA91 {name}\n\U0001F4B0 Цена: {price} руб.\n\U0001F4DD {desc}\n\U0001F4D8 Фото: {'есть' if photo else 'нет'}\n{stock_display}\n\U0001F194 id: {item_id}"
        send_message(
            user_id=sender_id,
            text=f"\u2705 Товар добавлен!\n\n{preview}",
            keyboard=build_admin_add_keyboard(),
        )
        return True

    # --- admin_edit_category_name ---
    if step == "admin_edit_category_name":
        name = (text or "").strip()
        if not name:
            send_message(user_id=sender_id, text="Название не может быть пустым. Напишите название:")
            return True
        # Fix 2: блокировка при модификации каталога
        with _lock:
            cat_index = state["cat_index"]
            old_name = CATALOG_DATA["categories"][cat_index]["name"]
            CATALOG_DATA["categories"][cat_index]["name"] = name
            save_catalog()
            del pending_replies[sender_id]
            save_state()
        send_message(
            user_id=sender_id,
            text=f"\u2705 Категория переименована!\nБыло: {old_name}\nСтало: {name}",
            keyboard=build_admin_edit_keyboard(),
        )
        return True

    # --- admin_edit_field_value ---
    if step == "admin_edit_field_value":
        value = (text or "").strip()
        if not value:
            send_message(user_id=sender_id, text="Значение не может быть пустым. Напишите значение:")
            return True
        cat_index = state["cat_index"]
        item_index = state["item_index"]
        field = state["field"]

        if field == "price":
            try:
                value = int(value)
                if value <= 0:
                    raise ValueError
            except ValueError:
                send_message(user_id=sender_id, text="\u26A0\uFE0F Некорректная цена. Напишите число, например: 7900")
                return True

        if field == "stock":
            try:
                value = int(value)
                if value < 0:
                    raise ValueError
            except ValueError:
                send_message(user_id=sender_id, text="\u26A0\uFE0F Некорректное количество. Напишите число, например: 5 или 0.")
                return True

        if field == "production_days":
            try:
                value = int(value)
                if value < 0:
                    raise ValueError
            except ValueError:
                send_message(user_id=sender_id, text="\u26A0\uFE0F Некорректный срок. Напишите число, например: 7 или 0.")
                return True

        if field == "photo_url" and value.lower() in ("нет", "no", "-", "нету"):
            value = ""

        # Fix 2: блокировка при модификации каталога
        with _lock:
            cats = CATALOG_DATA.get("categories", [])
            if cat_index < 0 or cat_index >= len(cats):
                with _lock:
                    del pending_replies[sender_id]
                    save_state()
                send_message(user_id=sender_id, text="\u26A0\uFE0F Категория не найдена. Возможно, каталог изменился.", keyboard=build_admin_edit_keyboard())
                return True
            items = cats[cat_index].get("items", [])
            if item_index < 0 or item_index >= len(items):
                with _lock:
                    del pending_replies[sender_id]
                    save_state()
                send_message(user_id=sender_id, text="\u26A0\uFE0F Товар не найден. Возможно, каталог изменился.", keyboard=build_admin_edit_keyboard())
                return True
            item = items[item_index]
            old_value = item.get(field, "")
            if field == "stock" and "stock" not in item:
                old_value = "не задано (0)"
            if field == "production_days" and "production_days" not in item:
                old_value = "не задано (0)"
            item[field] = value
            save_catalog()
            del pending_replies[sender_id]
            save_state()

        field_names = {
            "name": "Название",
            "price": "Цена",
            "description": "Описание",
            "photo_url": "Фото",
            "stock": "Наличие",
            "production_days": "Срок изготовления",
        }
        send_message(
            user_id=sender_id,
            text=f"\u2705 {field_names.get(field, field)} изменён!\nБыло: {old_value}\nСтало: {value}",
            keyboard=build_admin_edit_keyboard(),
        )
        return True

    # --- admin_import ---
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
                valid, vmsg = validate_catalog(data)
                if not valid:
                    raise ValueError(vmsg)
                # Fix 2: блокировка при замене каталога
                with _lock:
                    CATALOG_DATA.clear()
                    CATALOG_DATA.update(data)
                    ok = save_catalog()
                    if ok:
                        del pending_replies[sender_id]
                        save_state()
                if ok:
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
            valid, vmsg = validate_catalog(data)
            if valid:
                # Fix 2: блокировка при замене каталога
                with _lock:
                    CATALOG_DATA.clear()
                    CATALOG_DATA.update(data)
                    ok = save_catalog()
                    if ok:
                        del pending_replies[sender_id]
                        save_state()
                if ok:
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
    match = re.match(r"^#?(\d+)\s*[:.]\s*(.+)", text, re.DOTALL)
    if match:
        num = int(match.group(1))
        reply_text = match.group(2).strip()
        if num in active_dialogs:
            client_user_id = active_dialogs[num]["user_id"]
            send_message(user_id=client_user_id, text=reply_text)
            send_message(user_id=client_user_id, text="\U0001F447Главное меню\U0001F447", keyboard=build_main_menu_keyboard())
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


def _create_order(sender_id, items_list, contact_text):
    """Создаёт запись заказа в pending_orders и списывает товар со склада."""
    global order_counter
    order_counter += 1
    num = order_counter
    with _lock:
        # Списываем товар со склада сразу при оформлении заказа
        for it in items_list:
            item = find_item_by_id(it["id"])
            if item:
                current = item.get("stock", 0)
                if current > 0:
                    new_stock = max(0, current - it["qty"])
                    item["stock"] = new_stock
                    logger.info(f"Списание при заказе: {it['name']} -{it['qty']} (было {current}, стало {new_stock})")
        save_catalog()
        pending_orders[num] = {
            "user_id": sender_id,
            "items": items_list,
            "contact": contact_text,
            "timestamp": time.time(),
            "status": "pending",
        }
        save_state()
    logger.info(f"Создан заказ #{num} от user_id={sender_id}, товаров: {len(items_list)}, товар списан со склада")
    return num


def handle_pending_state(sender_id, text):
    state = pending_replies.get(sender_id)
    if not state or not isinstance(state, dict):
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

    if step == "waiting_question":
        question_text = (text or "").strip()
        if not question_text:
            send_message(user_id=sender_id, text="Пожалуйста, напишите ваш вопрос:")
            return True
        with _lock:
            pending_replies.pop(sender_id, None)
            save_state()
        global question_counter
        question_counter += 1
        num = question_counter
        item_name = state.get("item_name")
        track_event("question")
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
        send_message(user_id=sender_id, text="\u2705 Спасибо, вопрос передан мастеру!\n\n\u23F0\uFE0F Ответим в течение 30 минут.", keyboard=build_main_menu_keyboard())
        logger.info(f"Вопрос #{num} от user_id={sender_id}: {question_text}")
        return True

    if step == "waiting_contact":
        contact_text = (text or "").strip()
        valid, msg = validate_phone(contact_text)
        if not valid:
            send_message(user_id=sender_id, text=msg, keyboard=build_cancel_keyboard())
            return True
        with _lock:
            pending_replies.pop(sender_id, None)
            save_state()
        cart = state.get("cart", [])
        total = state.get("total", 0)
        track_event("cart_order")
        from collections import Counter
        item_counts = Counter(cart)
        items_list = []
        order_text = f"\U0001F4D8 Новый заказ!\n\nТелефон: {contact_text}\nТовары:\n"
        for item_id, qty in item_counts.items():
            item = find_item_by_id(item_id)
            if item:
                items_list.append({"id": item_id, "name": item["name"], "price": item["price"], "qty": qty})
                order_text += f"\u2022 \"{item['name']}\" — {item['price']} руб. x{qty}\n"
        order_text += f"\n\U0001F4B0 Итого: {total} руб."
        order_num = _create_order(sender_id, items_list, contact_text)
        order_text += f"\n\U0001F194 Заказ #{order_num} — /сделка для закрытия"
        send_message(user_id=NOTIFY_CHAT_ID, text=order_text)
        send_message(user_id=sender_id, text=f"\u2705 Спасибо за заказ! Номер заказа: #{order_num}\n\nМастер свяжется с вами в ближайшее время.", keyboard=build_main_menu_keyboard())
        with _lock:
            user_carts[sender_id] = []
            save_state()
        return True

    if step == "waiting_contact_quick":
        contact_text = (text or "").strip()
        valid, msg = validate_phone(contact_text)
        if not valid:
            send_message(user_id=sender_id, text=msg, keyboard=build_cancel_keyboard())
            return True
        with _lock:
            pending_replies.pop(sender_id, None)
            save_state()
        item = state.get("item")
        # Fix 3: товар мог быть удалён из каталога
        if not item:
            send_message(
                user_id=sender_id,
                text="\u26A0\uFE0F Этот товар больше не доступен. Откройте каталог заново.",
                keyboard=build_main_menu_keyboard(),
            )
            return True
        track_event("quick_order")
        items_list = [{"id": item["id"], "name": item["name"], "price": item["price"], "qty": 1}]
        order_num = _create_order(sender_id, items_list, contact_text)
        order_text = f"\U0001F4D8 Быстрый заказ!\n\nТовар: \"{item['name']}\"\nЦена: {item['price']} руб.\nТелефон: {contact_text}\n\U0001F194 Заказ #{order_num} — /сделка для закрытия"
        send_message(user_id=NOTIFY_CHAT_ID, text=order_text)
        send_message(user_id=sender_id, text=f"\u2705 Спасибо за заказ! Номер заказа: #{order_num}\n\nМастер свяжется с вами в ближайшее время.", keyboard=build_main_menu_keyboard())
        return True

    # Совместимость со старыми шагами
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
        track_event("cart_order")
        from collections import Counter
        item_counts = Counter(cart)
        items_list = []
        order_text = f"\U0001F4D8 Новый заказ!\nИмя: {name}\nТелефон: {phone}\nТовары:\n"
        for item_id, qty in item_counts.items():
            item = find_item_by_id(item_id)
            if item:
                items_list.append({"id": item_id, "name": item["name"], "price": item["price"], "qty": qty})
                order_text += f"\u2022 \"{item['name']}\" — {item['price']} руб. x{qty}\n"
        order_text += f"\n\U0001F4B0 Итого: {total} руб."
        order_num = _create_order(sender_id, items_list, f"{name}, {phone}")
        order_text += f"\n\U0001F194 Заказ #{order_num} — /сделка для закрытия"
        send_message(user_id=NOTIFY_CHAT_ID, text=order_text)
        send_message(user_id=sender_id, text=f"\u2705 Спасибо за заказ! Номер заказа: #{order_num}\n\nМастер свяжется с вами в ближайшее время.", keyboard=build_main_menu_keyboard())
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
        # Fix 3: товар мог быть удалён
        if not item:
            send_message(
                user_id=sender_id,
                text="\u26A0\uFE0F Этот товар больше не доступен. Откройте каталог заново.",
                keyboard=build_main_menu_keyboard(),
            )
            return True
        track_event("quick_order")
        items_list = [{"id": item["id"], "name": item["name"], "price": item["price"], "qty": 1}]
        order_num = _create_order(sender_id, items_list, phone)
        order_text = f"\U0001F4D8 Быстрый заказ!\nТовар: \"{item['name']}\"\nЦена: {item['price']} руб.\nТелефон: {phone}\n\U0001F194 Заказ #{order_num} — /сделка для закрытия"
        send_message(user_id=NOTIFY_CHAT_ID, text=order_text)
        send_message(user_id=sender_id, text=f"\u2705 Спасибо за заказ! Номер заказа: #{order_num}\n\nМастер свяжется с вами в ближайшее время.", keyboard=build_main_menu_keyboard())
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

    track_user(sender_id)

    # Fix 1: ПЕРЕХВАТ ОТВЕТОВ АДМИНА — ТОЛЬКО если нет активного pending state
    # Раньше стоял ДО проверки pending state, что блокировало админ-флоу

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
        if cmd in ["/экспорт", "/export"]:
            admin_export_catalog(sender_id)
            return
        if cmd in ["/импорт", "/import"]:
            admin_import_catalog(sender_id)
            return
        if cmd in ["/синхронизировать", "/sync", "/синхронизация"]:
            admin_sync_catalog(sender_id)
            return
        if cmd in ["/статистика", "/стата", "/stats"]:
            admin_show_stats(sender_id)
            return
        if cmd in ["/сделка", "/сделки", "/закрыть", "/отменить", "/отмена_заказа"]:
            admin_close_deal_start(sender_id)
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
                save_state()
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

    # /вопросы — только для админа
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

    # Fix 1: Обработка шагов (pending state) — ПРИОРИТЕТ над перехватом админ-ответов
    if sender_id in pending_replies:
        state = pending_replies.get(sender_id)
        if isinstance(state, dict) and "first_name" not in state and "step" in state:
            state["first_name"] = first_name

        # Сначала админ-шаги
        if is_admin(sender_id) and handle_admin_steps(sender_id, text):
            return

        if handle_pending_state(sender_id, text):
            return

        if text and not text.startswith("/"):
            if handle_pending_reply_comment(sender_id, text):
                return

    # Fix 1: Перехват ответов админа — ТОЛЬКО если нет активного pending state
    if is_admin(sender_id) and text and not text.startswith("/"):
        if handle_admin_reply(sender_id, text):
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
        track_event("comment_forwarded")
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
        "pending_orders": len(pending_orders),
        "carts": len(user_carts),
        "s3_enabled": S3_ENABLED,
        "db_enabled": _db_conn is not None,
        "db_path": DB_PATH,
        "total_users": stats["total_users"],
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "token_configured": bool(TOKEN),
        "webhook_url_configured": bool(WEBHOOK_URL),
        "notify_chat_configured": bool(NOTIFY_CHAT_ID),
        "catalog_items": len(CATALOG_INDEX),
        "s3_enabled": S3_ENABLED,
        "db_enabled": _db_conn is not None,
        "pending_orders": len(pending_orders),
    }), 200


# === ИНИЦИАЛИЗАЦИЯ ===
# Fix 5: инициализация SQLite ДО загрузки состояния
init_db()
load_stats()
load_state()

# Fix 10: register_commands и update_webhook_subscription только при основном запуске
# (не при импорте модуля в Gunicorn с несколькими worker'ами)
if __name__ == "__main__":
    register_commands()
    update_webhook_subscription()
else:
    # В продакшене (Gunicorn) — регистрируем с защитой от ошибок
    try:
        register_commands()
        update_webhook_subscription()
    except Exception as e:
        logger.warning(f"Не удалось зарегистрировать команды/подписку при старте: {e}")


# === GRACEFUL SHUTDOWN ===
def on_shutdown(signum, frame):
    logger.info(f"Получен сигнал {signum}, сохраняю состояние...")
    try:
        save_state()
    except Exception as e:
        logger.error(f"Ошибка при сохранении: {e}")
    logger.info("Состояние сохранено. Завершаю работу.")
    sys.exit(0)  # Fix 8: sys.exit вместо os._exit — сбрасывает буферы


signal.signal(signal.SIGTERM, on_shutdown)
signal.signal(signal.SIGINT, on_shutdown)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
