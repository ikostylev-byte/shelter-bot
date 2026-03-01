#!/usr/bin/env python3
"""
ялла, миклат! 🛡️  — Всеизраильская версия
Отправляешь геолокацию — получаешь карту с 5 ближайшими убежищами.

Источники данных:
1. Муниципальные ArcGIS (20+ городов) — пространственный запрос
2. GovMap (ags.govmap.gov.il) — государственный геопортал, вся страна
3. OpenStreetMap (Overpass API) — вся страна
4. ArcGIS Тель-Авива — дополнительные данные для ТА

Зависимости: pip install pyproj python-telegram-bot asyncpg requests staticmap Pillow
"""

import os, math, logging, asyncpg, requests
from io import BytesIO
from staticmap import StaticMap, CircleMarker
import PIL.Image
# staticmap использует устаревший ANTIALIAS — патчим
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS
from datetime import datetime, timedelta, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters, ContextTypes
from telegram.constants import ParseMode

# ─── ITM ↔ WGS84 координаты (pyproj) ────────────────────────────────────────
try:
    from pyproj import Transformer
    _itm_to_wgs = Transformer.from_crs("EPSG:2039", "EPSG:4326", always_xy=True)
    def itm_to_wgs84(x, y):
        lon, lat = _itm_to_wgs.transform(x, y)
        return lat, lon
except ImportError:
    logging.warning("pyproj не установлен — GovMap источник будет отключён. pip install pyproj")
    _itm_to_wgs = None
    def itm_to_wgs84(x, y):
        return None, None

BOT_TOKEN    = os.environ.get("BOT_TOKEN", "YOUR_TOKEN_HERE")
DATABASE_URL = os.environ.get("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)

# ─── ИСТОЧНИКИ ДАННЫХ ────────────────────────────────────────────────────────
# GovMap — государственный геопортал Израиля (POI слой, вся страна)
GOVMAP_SEARCH_URL = "https://ags.govmap.gov.il/Search/FreeSearch"
# Nominatim — обратное геокодирование для получения названия города
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
# Tel Aviv ArcGIS — дополнительные данные для ТА
ARCGIS_URL   = "https://gisn.tel-aviv.gov.il/arcgis/rest/services/WM/IView2WM/MapServer/592/query"
# Overpass API — OSM данные
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# ─── МУНИЦИПАЛЬНЫЕ ArcGIS ENDPOINTS ────────────────────────────────────────
# Каждый endpoint поддерживает spatial query (geometry + distance)
# bbox: (min_lat, min_lon, max_lat, max_lon) — грубый фильтр, чтобы не дёргать далёкие серверы
MUNICIPAL_ARCGIS = [
    {"name": "Holon",
     "url": "https://services2.arcgis.com/cjDo9oPmimdHxumn/arcgis/rest/services/%D7%9E%D7%A4%D7%AA_%D7%9E%D7%A7%D7%9C%D7%98%D7%99%D7%9D_%D7%A4%D7%AA%D7%95%D7%97%D7%99%D7%9D_WFL1/FeatureServer/0",
     "bbox": (31.98, 34.73, 32.06, 34.80)},
    {"name": "Herzliya",
     "url": "https://services3.arcgis.com/9qGhZGtb39XMVQyR/arcgis/rest/services/%D7%9E%D7%A7%D7%9C%D7%98%D7%99%D7%9D_2025/FeatureServer/0",
     "bbox": (32.13, 34.78, 32.20, 34.88)},
    {"name": "Petah Tikva",
     "url": "https://services2.arcgis.com/LRSgLpRWTkMT0jqN/arcgis/rest/services/miklat_bh/FeatureServer/0",
     "bbox": (32.06, 34.86, 32.13, 34.99)},
    {"name": "Ashkelon",
     "url": "https://services1.arcgis.com/yAQXemoDSgzdfV2A/arcgis/rest/services/public_shelter/FeatureServer/0",
     "bbox": (31.62, 34.52, 31.70, 34.60)},
    {"name": "Nahariya",
     "url": "https://services-eu1.arcgis.com/mFG6VsJiT6hDsVLu/arcgis/rest/services/%D7%A0%D7%94%D7%A8%D7%99%D7%94_%D7%9E%D7%A7%D7%9C%D7%98%D7%99%D7%9D_%D7%A6%D7%99%D7%91%D7%95%D7%A8%D7%99%D7%99%D7%9D/FeatureServer/0",
     "bbox": (32.97, 35.06, 33.05, 35.12)},
    {"name": "Akko",
     "url": "https://services8.arcgis.com/GY0eO9hmNflcIYdR/arcgis/rest/services/%D7%9E%D7%A7%D7%9C%D7%98%D7%99%D7%9D_%D7%A6%D7%99%D7%91%D7%95%D7%A8%D7%99%D7%99%D7%9D/FeatureServer/0",
     "bbox": (32.90, 35.05, 32.95, 35.10)},
    {"name": "Eilat",
     "url": "https://services9.arcgis.com/BtqYDIRT3FCK6rgL/arcgis/rest/services/miklatim/FeatureServer/0",
     "bbox": (29.52, 34.90, 29.60, 35.00)},
    {"name": "Nof HaGalil",
     "url": "https://services1.arcgis.com/aNzvrLxjvQddMgHb/arcgis/rest/services/miklatim/FeatureServer/0",
     "bbox": (32.68, 35.28, 32.74, 35.36)},
    {"name": "Nesher",
     "url": "https://services7.arcgis.com/EPYs7jIqde3L8ql3/arcgis/rest/services/%D7%9E%D7%A7%D7%9C%D7%98%D7%99%D7%9D_%D7%A0%D7%A9%D7%A8/FeatureServer/0",
     "bbox": (32.74, 35.02, 32.78, 35.07)},
    {"name": "Yerucham",
     "url": "https://services8.arcgis.com/o9mRsTJvcMg9lfkv/arcgis/rest/services/%D7%9E%D7%A7%D7%9C%D7%98%D7%99%D7%9D/FeatureServer/0",
     "bbox": (30.96, 34.90, 31.02, 34.96)},
    {"name": "Ofakim",
     "url": "https://services3.arcgis.com/iL7nIcXU1m2M6qTw/arcgis/rest/services/%D7%A9%D7%9B%D7%91%D7%AA_%D7%9E%D7%A7%D7%9C%D7%98%D7%99%D7%9D_View/FeatureServer/0",
     "bbox": (31.27, 34.59, 31.33, 34.65)},
    {"name": "Netivot",
     "url": "https://services7.arcgis.com/thA42rK5GLTTowD8/arcgis/rest/services/Netivot_GIS_Publish_WFL1/FeatureServer/43",
     "bbox": (31.40, 34.57, 31.45, 34.63)},
    {"name": "Nes Ziona",
     "url": "https://services-eu1.arcgis.com/1SaThKhnIOL6Cfhz/arcgis/rest/services/miklatim/FeatureServer/0",
     "bbox": (31.91, 34.78, 31.96, 34.83)},
    {"name": "Kfar Kasem",
     "url": "https://services6.arcgis.com/Ol4ENCL43hnNo9iM/arcgis/rest/services/miklatim/FeatureServer/0",
     "bbox": (32.10, 34.96, 32.14, 35.00)},
    {"name": "Beit Shean",
     "url": "https://services1.arcgis.com/yAQXemoDSgzdfV2A/arcgis/rest/services/_מקלטים_ינואר_2025_ביתשאן/FeatureServer/3",
     "bbox": (32.47, 35.47, 32.53, 35.53)},
    {"name": "Kiryat Malakhi",
     "url": "https://services3.arcgis.com/XBDMqmX1PKcVQCKG/arcgis/rest/services/%D7%9E%D7%A7%D7%9C%D7%98%D7%99%D7%9D_%D7%AA%D7%A6%D7%95%D7%92%D7%941/FeatureServer/0",
     "bbox": (31.71, 34.72, 31.76, 34.77)},
    {"name": "Emek Hefer",
     "url": "https://services5.arcgis.com/sifKFbbdIa4WOV8T/arcgis/rest/services/%D7%A2%D7%9E%D7%A7_%D7%97%D7%A4%D7%A8_%D7%9E%D7%A7%D7%9C%D7%98%D7%99%D7%9D/FeatureServer/24",
     "bbox": (32.28, 34.85, 32.45, 35.10)},
    {"name": "Misgav",
     "url": "https://services5.arcgis.com/L6dfICHVBGbPmyQq/arcgis/rest/services/msg_miklat_public/FeatureServer/0",
     "bbox": (32.78, 35.15, 32.95, 35.35)},
    {"name": "Ramat HaNegev",
     "url": "https://services9.arcgis.com/PwnRxbfXq19ftNRF/arcgis/rest/services/%D7%9E%D7%A7%D7%9C%D7%98%D7%99%D7%9D_%D7%A8%D7%9E%D7%AA_%D7%94%D7%A0%D7%92%D7%91/FeatureServer/0",
     "bbox": (30.40, 34.40, 31.10, 35.20)},
    {"name": "Eilot",
     "url": "https://services9.arcgis.com/czYNx0joeRNMma4B/arcgis/rest/services/%D7%9E%D7%A7%D7%9C%D7%98%D7%99%D7%9D/FeatureServer/0",
     "bbox": (29.45, 34.90, 29.65, 35.10)},
    {"name": "TA kindergartens",
     "url": "https://services3.arcgis.com/PcGFyTym9yKZBRgz/arcgis/rest/services/miklatim_ganim/FeatureServer/0",
     "bbox": (32.03, 34.74, 32.15, 34.82)},
]

SEARCH_RADIUS_M = 2000
MAX_RESULTS     = 5
CHECKIN_TTL_H   = 2

# Границы Тель-Авива (примерно) для определения нужен ли ArcGIS fallback
TA_BOUNDS = {"lat_min": 32.03, "lat_max": 32.15, "lon_min": 34.74, "lon_max": 34.82}

REVIEW_TEXT, REVIEW_PHOTO = range(2)

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── МУЛЬТИЯЗЫЧНОСТЬ ─────────────────────────────────────────────────────────
# Простейший i18n: юзер выбирает язык командой /lang
TEXTS = {
    "ru": {
        "welcome":      "🛡️ *ялла, миклат!*\n\nОтправь геолокацию — покажу ближайшие убежища.",
        "send_loc":     "📍 Отправить геолокацию",
        "no_shelters":  "😔 Убежищ в радиусе {radius} м не найдено.\nКоординаты: {lat:.5f}, {lon:.5f}\n\nПопробуй увеличить радиус /radius или поискать на Google Maps: מקלט ציבורי",
        "map_legend":   "🔵 ты   🔴 убежища",
        "shelter_type":  "🛡️ Убежище",
        "choose":       "*Выбери убежище:*\n",
        "distance":     "м",
        "checkin_done":  "✅ Отмечен в *{name}*\nЧекин активен {ttl} часа.",
        "buddies_here": "👥 Ещё здесь: {names}",
        "nobody_here":  "😶 Ты пока первый здесь.",
        "checkout_done": "🚪 Ты покинул убежище.",
        "not_checked":  "Ты не отмечен ни в одном убежище.",
        "review_ask":   "✍️ Отзыв для *{addr}*\n\nНапиши текст (или /skip → сразу к фото):",
        "review_photo": "📷 Фото убежища (или /skip):",
        "review_saved": "✅ Отзыв сохранён, спасибо!",
        "review_cancel": "Отменено.",
        "btn_going":    "🤝 Иду сюда",
        "btn_review":   "✍️ Отзыв",
        "btn_leave":    "🚪 Покинуть",
        "btn_back":     "← Назад",
        "going_header":  "🤝 *Идут сюда ({count}):*",
        "nobody_going": "🤝 *Пока никто не отметился*",
        "reviews_hdr":  "💬 *Отзывы:*",
        "send_loc_btn": "📍 Нажми кнопку внизу:",
        "choose_lang":  "Выбери язык / בחר שפה / Choose language:",
        "lang_set":     "✅ Язык: Русский",
        "error":        "❌ Ошибка: {err}",
        "search_error": "❌ Ошибка при поиске: {err}",
        "map_error":    "⚠️ Карта не загрузилась: {err}",
        "found":        "*Найдено {count} убежищ:*\n",
        "source_osm":   "📡 OSM",
        "source_ta":    "📡 ТА GIS",
        "photo_or_skip": "Фото или /skip:",
    },
    "he": {
        "welcome":      "🛡️ *יאללה, מקלט!*\n\nשלח מיקום — אראה לך את המקלטים הקרובים.",
        "send_loc":     "📍 שלח מיקום",
        "no_shelters":  "😔 לא נמצאו מקלטים ברדיוס {radius} מ'.\nקואורדינטות: {lat:.5f}, {lon:.5f}\n\nחפש ב-Google Maps: מקלט ציבורי",
        "map_legend":   "🔵 אתה   🔴 מקלטים",
        "shelter_type":  "🛡️ מקלט",
        "choose":       "*בחר מקלט:*\n",
        "distance":     "מ'",
        "checkin_done":  "✅ נרשמת ב-*{name}*\nהצ'ק-אין פעיל {ttl} שעות.",
        "buddies_here": "👥 גם כאן: {names}",
        "nobody_here":  "😶 אתה הראשון כאן.",
        "checkout_done": "🚪 יצאת מהמקלט.",
        "not_checked":  "לא רשום באף מקלט.",
        "review_ask":   "✍️ ביקורת על *{addr}*\n\nכתוב טקסט (או /skip → ישר לתמונה):",
        "review_photo": "📷 תמונה של המקלט (או /skip):",
        "review_saved": "✅ הביקורת נשמרה, תודה!",
        "review_cancel": "בוטל.",
        "btn_going":    "🤝 אני בדרך",
        "btn_review":   "✍️ ביקורת",
        "btn_leave":    "🚪 יציאה",
        "btn_back":     "← חזרה",
        "going_header":  "🤝 *בדרך לכאן ({count}):*",
        "nobody_going": "🤝 *אף אחד עדיין לא נרשם*",
        "reviews_hdr":  "💬 *ביקורות:*",
        "send_loc_btn": "📍 לחץ על הכפתור למטה:",
        "choose_lang":  "Выбери язык / בחר שפה / Choose language:",
        "lang_set":     "✅ שפה: עברית",
        "error":        "❌ שגיאה: {err}",
        "search_error": "❌ שגיאה בחיפוש: {err}",
        "map_error":    "⚠️ המפה לא נטענה: {err}",
        "found":        "*נמצאו {count} מקלטים:*\n",
        "source_osm":   "📡 OSM",
        "source_ta":    "📡 GIS ת\"א",
        "photo_or_skip": "תמונה או /skip:",
    },
    "en": {
        "welcome":      "🛡️ *Yalla, Miklat!*\n\nSend your location — I'll show the nearest shelters.",
        "send_loc":     "📍 Send location",
        "no_shelters":  "😔 No shelters within {radius} m.\nCoords: {lat:.5f}, {lon:.5f}\n\nTry Google Maps: מקלט ציבורי nearby",
        "map_legend":   "🔵 you   🔴 shelters",
        "shelter_type":  "🛡️ Shelter",
        "choose":       "*Choose a shelter:*\n",
        "distance":     "m",
        "checkin_done":  "✅ Checked in at *{name}*\nActive for {ttl} hours.",
        "buddies_here": "👥 Also here: {names}",
        "nobody_here":  "😶 You're the first one here.",
        "checkout_done": "🚪 You left the shelter.",
        "not_checked":  "Not checked in to any shelter.",
        "review_ask":   "✍️ Review for *{addr}*\n\nWrite text (or /skip for photo):",
        "review_photo": "📷 Photo of shelter (or /skip):",
        "review_saved": "✅ Review saved, thanks!",
        "review_cancel": "Cancelled.",
        "btn_going":    "🤝 Going here",
        "btn_review":   "✍️ Review",
        "btn_leave":    "🚪 Leave",
        "btn_back":     "← Back",
        "going_header":  "🤝 *Heading here ({count}):*",
        "nobody_going": "🤝 *No one checked in yet*",
        "reviews_hdr":  "💬 *Reviews:*",
        "send_loc_btn": "📍 Tap the button below:",
        "choose_lang":  "Выбери язык / בחר שפה / Choose language:",
        "lang_set":     "✅ Language: English",
        "error":        "❌ Error: {err}",
        "search_error": "❌ Search error: {err}",
        "map_error":    "⚠️ Map failed: {err}",
        "found":        "*Found {count} shelters:*\n",
        "source_osm":   "📡 OSM",
        "source_ta":    "📡 TA GIS",
        "photo_or_skip": "Photo or /skip:",
    },
}

def t(ctx, key, **kwargs):
    """Получить текст на языке юзера."""
    lang = (ctx.user_data or {}).get("lang", "ru")
    template = TEXTS.get(lang, TEXTS["ru"]).get(key, TEXTS["ru"].get(key, key))
    try:
        return template.format(**kwargs) if kwargs else template
    except (KeyError, IndexError):
        return template

def get_location_kb(ctx):
    lang = (ctx.user_data or {}).get("lang", "ru")
    label = TEXTS.get(lang, TEXTS["ru"])["send_loc"]
    return ReplyKeyboardMarkup(
        [[KeyboardButton(label, request_location=True)]],
        resize_keyboard=True, one_time_keyboard=False,
    )


_pool = None


# ─── БАЗА ДАННЫХ ──────────────────────────────────────────────────────────────

async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return _pool


async def db_init():
    pool = await get_pool()
    async with pool.acquire() as c:
        await c.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id SERIAL PRIMARY KEY,
                shelter_id TEXT NOT NULL,
                shelter_addr TEXT,
                user_id BIGINT NOT NULL,
                username TEXT,
                text TEXT,
                photo_id TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )""")
        await c.execute("""
            CREATE TABLE IF NOT EXISTS checkins (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                shelter_id TEXT NOT NULL,
                shelter_addr TEXT,
                shelter_name TEXT,
                lat DOUBLE PRECISION,
                lon DOUBLE PRECISION,
                checked_in_at TIMESTAMPTZ DEFAULT NOW()
            )""")
        # Таблица для хранения языковых настроек
        await c.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id BIGINT PRIMARY KEY,
                lang TEXT DEFAULT 'ru'
            )""")
    logger.info("DB ready")


async def save_review(shelter_id, shelter_addr, user_id, username, text, photo_id):
    pool = await get_pool()
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO reviews (shelter_id,shelter_addr,user_id,username,text,photo_id) VALUES($1,$2,$3,$4,$5,$6)",
            shelter_id, shelter_addr, user_id, username, text, photo_id)


async def get_reviews(shelter_id, limit=3):
    pool = await get_pool()
    async with pool.acquire() as c:
        return await c.fetch(
            "SELECT * FROM reviews WHERE shelter_id=$1 ORDER BY created_at DESC LIMIT $2",
            shelter_id, limit)


async def do_checkin(user_id, username, first_name, shelter):
    pool = await get_pool()
    async with pool.acquire() as c:
        await c.execute("""
            INSERT INTO checkins (user_id,username,first_name,shelter_id,shelter_addr,shelter_name,lat,lon,checked_in_at)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,NOW())
            ON CONFLICT(user_id) DO UPDATE SET
              shelter_id=EXCLUDED.shelter_id, shelter_addr=EXCLUDED.shelter_addr,
              shelter_name=EXCLUDED.shelter_name, lat=EXCLUDED.lat, lon=EXCLUDED.lon,
              checked_in_at=NOW(), username=EXCLUDED.username, first_name=EXCLUDED.first_name
        """, user_id, username, first_name,
            shelter["id"], shelter["address"], shelter["name"], shelter["lat"], shelter["lon"])


async def do_checkout(user_id):
    pool = await get_pool()
    async with pool.acquire() as c:
        await c.execute("DELETE FROM checkins WHERE user_id=$1", user_id)


async def get_buddies(shelter_id, exclude_user_id):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=CHECKIN_TTL_H)
    pool = await get_pool()
    async with pool.acquire() as c:
        return await c.fetch(
            "SELECT * FROM checkins WHERE shelter_id=$1 AND user_id!=$2 AND checked_in_at>$3 ORDER BY checked_in_at DESC",
            shelter_id, exclude_user_id, cutoff)


async def get_my_checkin(user_id):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=CHECKIN_TTL_H)
    pool = await get_pool()
    async with pool.acquire() as c:
        return await c.fetchrow(
            "SELECT * FROM checkins WHERE user_id=$1 AND checked_in_at>$2", user_id, cutoff)


async def save_user_lang(user_id, lang):
    pool = await get_pool()
    async with pool.acquire() as c:
        await c.execute("""
            INSERT INTO user_settings (user_id, lang) VALUES($1, $2)
            ON CONFLICT(user_id) DO UPDATE SET lang=EXCLUDED.lang
        """, user_id, lang)


async def load_user_lang(user_id):
    pool = await get_pool()
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT lang FROM user_settings WHERE user_id=$1", user_id)
        return row["lang"] if row else "ru"


# ─── GIS ──────────────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl  = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def is_in_tel_aviv(lat, lon):
    """Проверяем, попадает ли точка в примерные границы ТА."""
    return (TA_BOUNDS["lat_min"] <= lat <= TA_BOUNDS["lat_max"] and
            TA_BOUNDS["lon_min"] <= lon <= TA_BOUNDS["lon_max"])


def shelter_type_label(raw_type, lang="ru"):
    """Универсальная типизация убежищ."""
    if not raw_type:
        return TEXTS.get(lang, TEXTS["ru"])["shelter_type"]

    # Ивритские типы (из ArcGIS ТА)
    he_map = {
        "חניון מחסה לציבור":          ("🅿️", "Паркинг-убежище", "חניון מחסה", "Parking shelter"),
        "מקלט ציבורי במוסדות חינוך":  ("🏫", "Убежище (школа)", "מקלט בית ספר", "School shelter"),
        "מקלט ציבורי":                ("🏗️", "Общ. убежище", "מקלט ציבורי", "Public shelter"),
        "מקלט ציבורי נגיש":           ("♿", "Доступное убежище", "מקלט נגיש", "Accessible shelter"),
        "מקלט בשטח חניון":            ("🅿️", "Убежище (парковка)", "מקלט חניון", "Parking shelter"),
        "מקלט פנימי בשטח בית ספר":    ("🏫", "Убежище (школа)", "מקלט בית ספר", "School shelter"),
        "מרחב מוגן קהילתי":           ("🏢", "Общ. убежище", "מרחב מוגן", "Community shelter"),
        "מתקן מגון מני ילדים":        ("👶", "Убежище (дети)", "מקלט ילדים", "Children shelter"),
        "מתקן מגון רווחה":            ("🏥", "Убежище (соцслужба)", "מקלט רווחה", "Welfare shelter"),
        'ממ"ד':                        ("🏠", "Мамад", "ממ\"ד", "Mamad"),
        "ממד":                         ("🏠", "Мамад", "ממ\"ד", "Mamad"),
    }
    lang_idx = {"ru": 1, "he": 2, "en": 3}[lang] if lang in ("ru", "he", "en") else 1
    for h, labels in he_map.items():
        if h in raw_type:
            return f"{labels[0]} {labels[lang_idx]}"

    # OSM типы
    osm_map = {
        "bomb_shelter":  ("🛡️", "Бомбоубежище", "מקלט", "Bomb shelter"),
        "bunker":        ("🏗️", "Бункер", "בונקר", "Bunker"),
        "public":        ("🏢", "Общ. убежище", "מקלט ציבורי", "Public shelter"),
    }
    for key, labels in osm_map.items():
        if key in raw_type.lower():
            return f"{labels[0]} {labels[lang_idx]}"

    return f"🛡️ {raw_type}"


# ── GOVMAP — ГОСУДАРСТВЕННЫЙ ГЕОПОРТАЛ (ВСЯ СТРАНА) ──────────────────────────

def reverse_geocode_names(lat, lon):
    """Получаем список возможных названий населённого пункта на иврите.
    Возвращает list[str] — кандидаты для поиска в GovMap.
    """
    names = []
    try:
        r = requests.get(NOMINATIM_URL, params={
            "lat": lat, "lon": lon, "format": "json",
            "zoom": 14, "accept-language": "he",
        }, headers={"User-Agent": "YallaMiklat/1.0"}, timeout=5)
        r.raise_for_status()
        addr = r.json().get("address", {})

        # Собираем все подходящие названия (кроме мо'ацот)
        for key in ("city", "town", "village", "suburb", "neighbourhood",
                     "hamlet", "municipality"):
            v = (addr.get(key) or "").strip()
            if v and v not in names and not v.startswith("מועצה"):
                names.append(v)

        # Если Nominatim вернул только мо'ацу — ищем имя поселения через Overpass
        is_regional = all(
            (addr.get(k, "").startswith("מועצה") or not addr.get(k))
            for k in ("city", "town")
        )
        if is_regional and not names:
            osm_names = _settlement_names_osm(lat, lon)
            for n in osm_names:
                if n not in names:
                    names.append(n)

    except Exception as e:
        logger.warning("Nominatim error: %s", e)

    return names


def _settlement_names_osm(lat, lon, radius=3000):
    """Fallback: находим имя ближайшего поселения через Overpass (place=*).
    Для кибуцев/мошавов где Nominatim возвращает только «мо'ацу эзорит».
    """
    query = (
        f'[out:json][timeout:5];'
        f'(node["place"~"village|town|city|hamlet|kibbutz|moshav|neighbourhood"]'
        f'(around:{radius},{lat},{lon}););out body;'
    )
    try:
        r = requests.post(OVERPASS_URL, data={"data": query}, timeout=8)
        r.raise_for_status()
        elements = r.json().get("elements", [])
    except Exception:
        return []

    places = []
    for el in elements:
        name_he = el.get("tags", {}).get("name", "")
        if name_he:
            d = haversine(lat, lon, el["lat"], el["lon"])
            places.append((name_he, d))
    places.sort(key=lambda x: x[1])
    # Возвращаем до 3 ближайших
    return [n for n, _ in places[:3]]


# Варианты написания ивритских названий (Nominatim и GovMap иногда разходятся)
_HE_SPELLING_VARIANTS = [
    ("קרית", "קריית"),   # Кирьят — с/без юд
    ("קריית", "קרית"),
    ("גבעת", "גבעות"),   # Гиват/Гивот
    ("גבעות", "גבעת"),
    ("–", "-"),           # разные тире
    ("-", "–"),
    ("תל אביב–יפו", "תל אביב-יפו"),
]

# In-memory кэш убежищ по городам: {"חיפה": {oid: shelter_dict, ...}}
# Заполняется при первом запросе из города, потом переиспользуется
_govmap_cache = {}


def fetch_shelters_govmap(lat, lon, radius_m=None):
    """Ищем убежища через GovMap (государственный геопортал Израиля).
    Кэшируем все убежища города — при повторном запросе из того же города моментально.
    """
    if _itm_to_wgs is None:
        return []  # pyproj не установлен

    if radius_m is None:
        radius_m = SEARCH_RADIUS_M

    place_names = reverse_geocode_names(lat, lon)
    if not place_names:
        logger.warning("GovMap: не удалось определить населённый пункт")
        return []

    # Добавляем варианты написания (קרית ↔ קריית и т.п.)
    expanded = []
    for name in place_names:
        expanded.append(name)
        for orig, alt in _HE_SPELLING_VARIANTS:
            if orig in name:
                variant = name.replace(orig, alt)
                if variant not in expanded:
                    expanded.append(variant)
    place_names = expanded

    logger.info("GovMap: кандидаты = %s", place_names)

    # Собираем все убежища из кэша или API
    all_cached = {}
    cities_to_fetch = []
    for city in place_names:
        if city in _govmap_cache:
            for oid, data in _govmap_cache[city].items():
                if oid not in all_cached:
                    all_cached[oid] = data
        else:
            cities_to_fetch.append(city)

    # Запрашиваем GovMap для некэшированных городов
    for city in cities_to_fetch:
        city_shelters = {}
        # 4 паттерна запросов для максимального покрытия
        seen_queries = set()
        for prefix in ["מקלט", "מקלט ציבורי", "מקלט מספר", "מקלט מס"]:
            query_text = f"{prefix} {city}"
            if query_text in seen_queries:
                continue
            seen_queries.add(query_text)
            try:
                r = requests.post(GOVMAP_SEARCH_URL,
                    json={"keyword": query_text, "type": "all"},
                    headers={"Content-Type": "application/json"},
                    timeout=10)
                r.raise_for_status()
                results = (r.json().get("data") or {}).get("Result", [])
            except Exception as e:
                logger.warning("GovMap search error (%s): %s", query_text, e)
                continue

            for item in results:
                oid = item.get("ObjectID")
                if oid in city_shelters or oid in all_cached:
                    continue
                itm_x = item.get("X")
                itm_y = item.get("Y")
                if not itm_x or not itm_y:
                    continue
                slat, slon = itm_to_wgs84(itm_x, itm_y)
                if slat is None:
                    continue

                label = item.get("ResultLable", "")
                parts = label.split("|")
                name = parts[0].strip() if parts else "מקלט"
                loc = parts[1].strip() if len(parts) > 1 else city

                city_shelters[oid] = {
                    "id":       f"gov:{oid}",
                    "lat":      slat,
                    "lon":      slon,
                    "address":  f"{name}, {loc}",
                    "name":     name,
                    "type_raw": "bomb_shelter",
                    "hours":    "", "phone": "", "notes": "",
                    "source":   "gov",
                }

        _govmap_cache[city] = city_shelters
        logger.info("GovMap: cached %d shelters for '%s'", len(city_shelters), city)
        all_cached.update(city_shelters)

    # Фильтруем по радиусу и добавляем расстояние
    shelters = []
    for oid, s in all_cached.items():
        dist = haversine(lat, lon, s["lat"], s["lon"])
        if dist <= radius_m:
            shelter = dict(s)
            shelter["distance"] = round(dist)
            shelters.append(shelter)

    shelters.sort(key=lambda x: x["distance"])
    return shelters


# ── OVERPASS API (OSM) — ВСЕИЗРАИЛЬСКИЙ ИСТОЧНИК ──────────────────────────────

def fetch_shelters_osm(lat, lon, radius_m=None):
    """Ищем убежища через Overpass API в радиусе вокруг точки."""
    if radius_m is None:
        radius_m = SEARCH_RADIUS_M

    query = f"""
    [out:json][timeout:15];
    (
      nwr["amenity"="shelter"]["shelter_type"="bomb_shelter"](around:{radius_m},{lat},{lon});
      nwr["military"="bunker"](around:{radius_m},{lat},{lon});
      nwr["building"="bunker"](around:{radius_m},{lat},{lon});
      nwr["bunker_type"="bomb_shelter"](around:{radius_m},{lat},{lon});
      nwr["amenity"="shelter"]["shelter_type"="public_transport"!~"."](around:{radius_m},{lat},{lon});
    );
    out center body;
    """
    # Последняя строка: shelter без shelter_type=public_transport (чтобы не грести автобусные остановки)
    # Но это слишком широко. Убираем последнюю строку, оставляем только bomb_shelter и bunker
    query = f"""
    [out:json][timeout:15];
    (
      nwr["amenity"="shelter"]["shelter_type"="bomb_shelter"](around:{radius_m},{lat},{lon});
      nwr["military"="bunker"](around:{radius_m},{lat},{lon});
      nwr["building"="bunker"](around:{radius_m},{lat},{lon});
      nwr["bunker_type"="bomb_shelter"](around:{radius_m},{lat},{lon});
    );
    out center body;
    """

    try:
        r = requests.post(OVERPASS_URL, data={"data": query}, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("Overpass API error: %s", e)
        return []

    shelters = []
    for el in data.get("elements", []):
        # Для way/relation берём center, для node — lat/lon напрямую
        slat = el.get("lat") or (el.get("center", {}).get("lat"))
        slon = el.get("lon") or (el.get("center", {}).get("lon"))
        if not slat or not slon:
            continue

        tags = el.get("tags", {})
        name = tags.get("name", "").strip()
        addr = tags.get("addr:street", "")
        house = tags.get("addr:housenumber", "")
        if addr and house:
            addr = f"{addr} {house}"
        elif not addr:
            addr = tags.get("addr:full", "") or name

        # Определяем тип
        raw_type = (tags.get("shelter_type", "") or
                    tags.get("bunker_type", "") or
                    tags.get("building", "") or "bomb_shelter")

        shelters.append({
            "id":       f"osm:{el['type']}:{el['id']}",
            "lat":      slat,
            "lon":      slon,
            "address":  addr.strip() or tags.get("description", "") or "מקלט",
            "name":     name,
            "type_raw": raw_type,
            "hours":    tags.get("opening_hours", "").strip(),
            "phone":    tags.get("phone", "").strip(),
            "notes":    tags.get("note", "").strip() or tags.get("description", "").strip(),
            "distance": round(haversine(lat, lon, slat, slon)),
            "source":   "osm",
        })

    shelters.sort(key=lambda x: x["distance"])
    return shelters


# ── ARCGIS ТЕЛЬ-АВИВА — ДОПОЛНИТЕЛЬНЫЙ ИСТОЧНИК ──────────────────────────────

def parse_shelter_arcgis(feat, ulat, ulon):
    g = feat.get("geometry", {}); a = feat.get("attributes", {})
    slat = g.get("y") or a.get("lat")
    slon = g.get("x") or a.get("lon")
    addr = (a.get("Full_Address") or "").strip()
    if not addr:
        addr = f"{(a.get('shem_recho') or '').strip()} {str(a.get('ms_bait') or '').strip()}".strip() or "כתובת לא ידועה"
    return {
        "id":       f"ta:{a.get('UniqueId') or str(a.get('oid_mitkan', ''))}",
        "lat": slat, "lon": slon,
        "address":  addr,
        "name":     (a.get("shem") or "").strip(),
        "type_raw": a.get("t_sug", ""),
        "hours":    (a.get("opening_times") or "").strip(),
        "phone":    (a.get("telephone_henion") or a.get("celolar") or "").strip(),
        "notes":    (a.get("hearot") or "").strip(),
        "distance": round(haversine(ulat, ulon, slat, slon)),
        "source":   "ta",
    }


def fetch_shelters_arcgis(lat, lon):
    """Оригинальный запрос к ArcGIS Тель-Авива."""
    params = {
        "where": "1=1", "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint", "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "distance": SEARCH_RADIUS_M, "units": "esriSRUnit_Meter",
        "outFields": "*", "outSR": "4326", "returnGeometry": "true",
        "f": "json", "resultRecordCount": 100,
    }
    try:
        r = requests.get(ARCGIS_URL, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(data["error"])
        features = data.get("features", [])

        # Fallback: если spatial вернул 0
        if not features:
            params2 = {
                "where": "1=1", "outFields": "*", "outSR": "4326",
                "returnGeometry": "true", "f": "json", "resultRecordCount": 500,
            }
            r2 = requests.get(ARCGIS_URL, params=params2, timeout=15)
            r2.raise_for_status()
            data2 = r2.json()
            features = [
                f for f in data2.get("features", [])
                if f.get("geometry") and
                   haversine(lat, lon, f["geometry"].get("y", 0), f["geometry"].get("x", 0)) <= SEARCH_RADIUS_M
            ]

        return [parse_shelter_arcgis(f, lat, lon) for f in features if f.get("geometry")]
    except Exception as e:
        logger.warning("ArcGIS error: %s", e)
        return []


# ── МУНИЦИПАЛЬНЫЕ ArcGIS: ПРОСТРАНСТВЕННЫЙ ЗАПРОС ────────────────────────────

def _in_bbox(lat, lon, bbox, margin=0.05):
    """Проверяем, попадает ли точка в bbox с запасом (margin в градусах ≈ 5 км)."""
    return (bbox[0] - margin <= lat <= bbox[2] + margin and
            bbox[1] - margin <= lon <= bbox[3] + margin)


def _parse_municipal_feature(feat, ulat, ulon, source_name):
    """Универсальный парсер для муниципальных ArcGIS (разные схемы полей)."""
    g = feat.get("geometry", {})
    a = feat.get("attributes", {})
    slat = g.get("y")
    slon = g.get("x")
    if not slat or not slon or abs(slat) < 1:
        return None

    dist = haversine(ulat, ulon, slat, slon)

    # Адрес — пробуем разные поля
    addr = ""
    for key in ("Full_Address", "כתובת", "Adress", "adress", "ADDRESS",
                "ShelterAddress", "o_adress"):
        v = (a.get(key) or "").strip()
        if v and v != " ":
            addr = v
            break
    if not addr:
        street = (a.get("shem_recho") or a.get("STREETNAME") or "").strip()
        house = str(a.get("ms_bait") or a.get("HOUSE") or "").strip()
        addr = f"{street} {house}".strip()

    # Название
    name = ""
    for key in ("name", "Name", "shem", "ShelterName", "ID", "מקלט",
                "o_name", "place", "Miklat_Num"):
        v = str(a.get(key) or "").strip()
        if v and v != " " and v != "None":
            name = v
            break
    if not name:
        name = f"מקלט ({source_name})"

    # Тип
    type_raw = (a.get("t_sug") or a.get("LayerNa") or a.get("שימוש_ראשי")
                or a.get("place") or "bomb_shelter")

    # ID — стабильный
    oid = a.get("OBJECTID") or a.get("OBJECTID_1") or a.get("FID") or ""
    shelter_id = f"muni:{source_name}:{oid}"

    return {
        "id":       shelter_id,
        "lat":      slat,
        "lon":      slon,
        "address":  addr or f"{name}, {source_name}",
        "name":     name,
        "type_raw": type_raw,
        "hours":    (a.get("opening_times") or "").strip() if isinstance(a.get("opening_times"), str) else "",
        "phone":    "",
        "notes":    (a.get("הערות") or a.get("hearot") or a.get("Comments") or "").strip() if isinstance(a.get("הערות") or a.get("hearot") or a.get("Comments"), str) else "",
        "distance": round(dist),
        "source":   "muni",
    }


def fetch_shelters_municipal(lat, lon, radius_m=None):
    """Запрашиваем все релевантные муниципальные ArcGIS endpoints (spatial query)."""
    if radius_m is None:
        radius_m = SEARCH_RADIUS_M

    # Определяем, какие endpoints могут дать результаты
    relevant = [ep for ep in MUNICIPAL_ARCGIS if _in_bbox(lat, lon, ep["bbox"])]
    if not relevant:
        return []

    logger.info("Municipal ArcGIS: querying %d endpoints: %s",
                len(relevant), [ep["name"] for ep in relevant])

    shelters = []
    for ep in relevant:
        params = {
            "where": "1=1",
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "distance": radius_m,
            "units": "esriSRUnit_Meter",
            "outFields": "*",
            "outSR": "4326",
            "returnGeometry": "true",
            "f": "json",
            "resultRecordCount": 100,
        }
        try:
            r = requests.get(ep["url"] + "/query", params=params, timeout=8)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                logger.warning("Municipal %s error: %s", ep["name"], data["error"])
                continue
            features = data.get("features", [])
            for feat in features:
                s = _parse_municipal_feature(feat, lat, lon, ep["name"])
                if s and s["distance"] <= radius_m:
                    shelters.append(s)
            if features:
                logger.info("Municipal %s: %d shelters", ep["name"], len(features))
        except Exception as e:
            logger.warning("Municipal %s error: %s", ep["name"], e)

    return shelters


# ── ОБЪЕДИНЁННЫЙ ПОИСК ────────────────────────────────────────────────────────

def deduplicate_shelters(shelters, threshold_m=50):
    """Убираем дубли — если два убежища ближе threshold_m друг к другу, берём с большим приоритетом."""
    # Приоритет: ta > muni > gov > osm
    priority = {"ta": 4, "muni": 3, "gov": 2, "osm": 1}
    result = []
    for s in shelters:
        is_dup = False
        for i, existing in enumerate(result):
            if haversine(s["lat"], s["lon"], existing["lat"], existing["lon"]) < threshold_m:
                # Оставляем тот, у которого выше приоритет
                if priority.get(s["source"], 0) > priority.get(existing["source"], 0):
                    result[i] = s
                is_dup = True
                break
        if not is_dup:
            result.append(s)
    return result


def fetch_shelters(lat, lon):
    """Главная функция поиска: Municipal + GovMap + OSM + ArcGIS (ТА), дедупликация, топ-N.
    Если результатов мало — автоматически расширяем радиус.
    """
    base_radius = SEARCH_RADIUS_M  # 2000m

    # 1. Муниципальные ArcGIS — пространственный запрос (самый точный)
    shelters_muni = fetch_shelters_municipal(lat, lon, radius_m=5000)
    shelters_muni_near = [s for s in shelters_muni if s["distance"] <= base_radius]
    logger.info("Municipal: found %d (≤%dm) / %d (≤5km)",
                len(shelters_muni_near), base_radius, len(shelters_muni))

    # 2. GovMap — государственный геопортал (текстовый поиск, до 5 км)
    shelters_gov_all = fetch_shelters_govmap(lat, lon, radius_m=5000)
    shelters_gov = [s for s in shelters_gov_all if s["distance"] <= base_radius + 1000]
    logger.info("GovMap: found %d (≤3km) / %d (≤5km)", len(shelters_gov), len(shelters_gov_all))

    # 3. OSM Overpass — дополнительный
    shelters_osm = fetch_shelters_osm(lat, lon, radius_m=base_radius)
    logger.info("OSM (r=%dm): found %d shelters", base_radius, len(shelters_osm))

    # 4. ArcGIS Тель-Авив
    shelters_ta = []
    if is_in_tel_aviv(lat, lon):
        shelters_ta = fetch_shelters_arcgis(lat, lon)
        logger.info("ArcGIS TA: found %d shelters", len(shelters_ta))

    # Объединяем и дедуплицируем (приоритет: TA > Muni > GovMap > OSM)
    all_shelters = shelters_ta + shelters_muni_near + shelters_gov + shelters_osm
    all_shelters = deduplicate_shelters(all_shelters)

    # Прогрессивное расширение: если мало — ищем шире
    for expanded_r in (3000, 5000):
        if len(all_shelters) >= 3:
            break
        logger.info("Expanding radius to %dm (have %d shelters)", expanded_r, len(all_shelters))
        shelters_osm_ext = fetch_shelters_osm(lat, lon, radius_m=expanded_r)
        shelters_gov_ext = [s for s in shelters_gov_all if s["distance"] <= expanded_r]
        shelters_muni_ext = [s for s in shelters_muni if s["distance"] <= expanded_r]
        extra = shelters_ta + shelters_muni_ext + shelters_gov_ext + shelters_osm_ext
        all_shelters = deduplicate_shelters(extra)

    all_shelters.sort(key=lambda x: x["distance"])

    return all_shelters[:MAX_RESULTS]


# ─── КАРТА ────────────────────────────────────────────────────────────────────

def generate_map(user_lat, user_lon, shelters) -> BytesIO:
    from PIL import ImageDraw, ImageFont
    m = StaticMap(900, 700, url_template="https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png")
    for s in shelters:
        m.add_marker(CircleMarker((s["lon"], s["lat"]), "#C0392B", 30))
        m.add_marker(CircleMarker((s["lon"], s["lat"]), "white", 18))
    m.add_marker(CircleMarker((user_lon, user_lat), "#2471A3", 22))
    m.add_marker(CircleMarker((user_lon, user_lat), "white", 12))
    image = m.render()
    w, h = image.size

    def to_px(lon, lat):
        import math
        n = 2 ** m.zoom
        x = (lon + 180) / 360 * n
        lat_r = math.radians(lat)
        y = (1 - math.log(math.tan(lat_r) + 1/math.cos(lat_r)) / math.pi) / 2 * n
        return int((x - m.x_center) * 256 + w/2), int((y - m.y_center) * 256 + h/2)

    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
    except:
        font = ImageFont.load_default()

    for i, s in enumerate(shelters, 1):
        px, py = to_px(s["lon"], s["lat"])
        draw.ellipse([px-14, py-38, px+14, py-10], fill="white", outline="#C0392B", width=2)
        bb = draw.textbbox((0, 0), str(i), font=font)
        tw, th = bb[2]-bb[0], bb[3]-bb[1]
        draw.text((px - tw//2, py - 38 + (28-th)//2), str(i), fill="#C0392B", font=font)

    buf = BytesIO()
    image.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ─── HANDLERS ─────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Загружаем язык пользователя из БД
    try:
        lang = await load_user_lang(update.effective_user.id)
        ctx.user_data["lang"] = lang
    except:
        ctx.user_data.setdefault("lang", "ru")

    # Кнопки языков в одну строку + кнопка геолокации внизу
    lang_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🇷🇺 RU", callback_data="lang:ru"),
        InlineKeyboardButton("🇮🇱 עב", callback_data="lang:he"),
        InlineKeyboardButton("🇬🇧 EN", callback_data="lang:en"),
    ]])

    await update.message.reply_text(
        "🛡️ *Yalla, Miklat! · !יאללה, מקלט · Ялла, миклат!*\n\n"
        "🇷🇺 Выбери язык\n🇮🇱 בחר שפה\n🇬🇧 Choose language",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=lang_kb,
    )


async def cmd_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Выбор языка."""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang:ru")],
        [InlineKeyboardButton("🇮🇱 עברית", callback_data="lang:he")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang:en")],
    ])
    await update.message.reply_text(t(ctx, "choose_lang"), reply_markup=kb)


async def cb_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = query.data.split(":")[1]
    ctx.user_data["lang"] = lang
    try:
        await save_user_lang(query.from_user.id, lang)
    except:
        pass
    # После выбора языка — приветствие + кнопка геолокации
    await query.message.reply_text(
        t(ctx, "lang_set") + "\n\n" + t(ctx, "welcome"),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_location_kb(ctx),
    )


async def handle_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lat = update.message.location.latitude
    lon = update.message.location.longitude
    logger.info("Location: %s %s", lat, lon)

    # Загружаем язык если ещё не загружен
    if "lang" not in ctx.user_data:
        try:
            ctx.user_data["lang"] = await load_user_lang(update.effective_user.id)
        except:
            ctx.user_data["lang"] = "ru"

    lang = ctx.user_data.get("lang", "ru")

    # Ищем убежища
    try:
        shelters = fetch_shelters(lat, lon)
    except Exception as e:
        logger.error("Search error: %s", e, exc_info=True)
        await update.message.reply_text(t(ctx, "search_error", err=e))
        return

    if not shelters:
        await update.message.reply_text(
            t(ctx, "no_shelters", radius=5000, lat=lat, lon=lon),
        )
        return

    # Добавляем тип на нужном языке
    for s in shelters:
        s["type"] = shelter_type_label(s["type_raw"], lang)

    ctx.user_data["shelters"] = shelters
    ctx.user_data["user_lat"] = lat
    ctx.user_data["user_lon"] = lon

    dist_unit = t(ctx, "distance")

    # Кнопки выбора
    buttons = []
    for i, s in enumerate(shelters, 1):
        waze_url = f"https://waze.com/ul?ll={s['lat']},{s['lon']}&navigate=yes"
        gmaps_url = f"https://maps.google.com/maps?daddr={s['lat']},{s['lon']}"
        label = s['address'][:28] if s['address'] else s['name'][:28]
        buttons.append([
            InlineKeyboardButton(f"#{i} {label}", callback_data=f"select:{i-1}"),
        ])
        buttons.append([
            InlineKeyboardButton("🚗 Waze", url=waze_url),
            InlineKeyboardButton("🗺️ Google Maps", url=gmaps_url),
        ])
    kb = InlineKeyboardMarkup(buttons)

    # Карта + кнопки
    try:
        map_buf = generate_map(lat, lon, shelters)
        caption_lines = [t(ctx, "map_legend") + "\n"]
        for i, s in enumerate(shelters, 1):
            src = {"ta": "🟢", "gov": "🏛️", "osm": "🌐"}.get(s["source"], "")
            caption_lines.append(f"#{i} {s['address']} — {s['distance']} {dist_unit} {src}")
        await update.message.reply_photo(
            photo=map_buf,
            caption="\n".join(caption_lines),
            reply_markup=kb,
        )
    except Exception as e:
        logger.error("Map error: %s", e, exc_info=True)
        await update.message.reply_text(t(ctx, "map_error", err=e))
        lines = [t(ctx, "found", count=len(shelters))]
        for i, s in enumerate(shelters, 1):
            line = f"*#{i}* {s['type']}\n📍 {s['address']} — _{s['distance']} {dist_unit}_"
            if s["hours"]: line += f"\n🕐 {s['hours']}"
            if s["phone"]: line += f"\n📞 {s['phone']}"
            lines.append(line)
        await update.message.reply_text(
            "\n\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )


async def cb_select_shelter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Пользователь выбрал убежище из списка."""
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(":")[1])

    shelters = ctx.user_data.get("shelters", [])
    if not shelters or idx >= len(shelters):
        await query.message.reply_text("📍", reply_markup=get_location_kb(ctx))
        return

    s = shelters[idx]
    lang = ctx.user_data.get("lang", "ru")
    user_id = query.from_user.id
    dist_unit = t(ctx, "distance")

    # Кто уже идёт
    buddies = await get_buddies(s["id"], user_id)
    reviews = await get_reviews(s["id"], limit=3)

    lines = [f"*{s['type']}*", f"📍 {s['address']}", f"📏 {s['distance']} {dist_unit}"]
    if s["hours"]: lines.append(f"🕐 {s['hours']}")
    if s["phone"]: lines.append(f"📞 {s['phone']}")
    if s["notes"]:
        note = s["notes"][:120] + "…" if len(s["notes"]) > 120 else s["notes"]
        lines.append(f"\nℹ️ _{note}_")

    lines.append("")
    if buddies:
        def buddy_link(b):
            name = b["first_name"] or b["username"] or "?"
            return f"[{name}](tg://user?id={b['user_id']})"
        names = [buddy_link(b) for b in buddies]
        lines.append(t(ctx, "going_header", count=len(buddies)))
        lines.append("  ".join(names))
    else:
        lines.append(t(ctx, "nobody_going"))

    if reviews:
        lines.append("")
        lines.append(t(ctx, "reviews_hdr"))
        for r in reviews:
            txt = (r["text"] or "📷")[:80]
            lines.append(f"• *{r['username'] or '?'}:* {txt}")

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(t(ctx, "btn_going"), callback_data=f"checkin:{s['id']}:{idx}"),
            InlineKeyboardButton(t(ctx, "btn_review"), callback_data=f"review:{s['id']}:{s['address'][:30]}"),
        ],
        [InlineKeyboardButton(t(ctx, "btn_back"), callback_data="back")],
    ])

    await query.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )

    # Фотки людей в убежище
    if buddies:
        from telegram import InputMediaPhoto
        media = []
        for b in buddies:
            try:
                photos = await ctx.bot.get_user_profile_photos(b["user_id"], limit=1)
                if photos.total_count > 0:
                    file_id = photos.photos[0][-1].file_id
                    name = b["first_name"] or b["username"] or "?"
                    media.append(InputMediaPhoto(media=file_id, caption=name))
            except Exception:
                pass
        if media:
            await query.message.reply_media_group(media=media)


async def cb_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    shelters = ctx.user_data.get("shelters", [])
    if not shelters:
        await query.message.reply_text("📍", reply_markup=get_location_kb(ctx))
        return

    lang = ctx.user_data.get("lang", "ru")
    dist_unit = t(ctx, "distance")

    lines = [t(ctx, "choose")]
    for i, s in enumerate(shelters, 1):
        line = f"*#{i}* {s['type']}\n📍 {s['address']} — _{s['distance']} {dist_unit}_"
        lines.append(line)
    buttons = [[InlineKeyboardButton(f"#{i} — {s['address'][:35]}", callback_data=f"select:{i-1}")]
               for i, s in enumerate(shelters, 1)]
    await query.message.reply_text(
        "\n\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ── ЧЕКИН ─────────────────────────────────────────────────────────────────────

async def cb_checkin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("✅")
    _, shelter_id, idx = query.data.split(":", 2)
    shelters = ctx.user_data.get("shelters", [])
    shelter  = next((s for s in shelters if s["id"] == shelter_id), None)
    if not shelter: return

    user = query.from_user
    await do_checkin(user.id, user.username, user.first_name, shelter)

    buddies = await get_buddies(shelter_id, user.id)
    if buddies:
        names = [f"@{b['username']}" if b["username"] else (b["first_name"] or "?") for b in buddies]
        buddy_text = t(ctx, "buddies_here", names=", ".join(names))
    else:
        buddy_text = t(ctx, "nobody_here")

    name = shelter['name'] or shelter['address']
    await query.message.reply_text(
        t(ctx, "checkin_done", name=name, ttl=CHECKIN_TTL_H) + f"\n\n{buddy_text}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(t(ctx, "btn_leave"), callback_data="checkout")
        ]]),
    )


async def cb_checkout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await do_checkout(query.from_user.id)
    await query.message.reply_text(t(ctx, "checkout_done"), reply_markup=get_location_kb(ctx))


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if "lang" not in ctx.user_data:
        try:
            ctx.user_data["lang"] = await load_user_lang(update.effective_user.id)
        except:
            ctx.user_data["lang"] = "ru"

    ci = await get_my_checkin(update.effective_user.id)
    if not ci:
        await update.message.reply_text(t(ctx, "not_checked"), reply_markup=get_location_kb(ctx))
        return
    buddies = await get_buddies(ci["shelter_id"], update.effective_user.id)
    names = [f"@{b['username']}" if b["username"] else (b["first_name"] or "?") for b in buddies]
    await update.message.reply_text(
        f"📍 *{ci['shelter_name'] or ci['shelter_addr']}*\n"
        f"👥 {', '.join(names) if names else '—'}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(t(ctx, "btn_leave"), callback_data="checkout")
        ]]),
    )


# ── ОТЗЫВ ─────────────────────────────────────────────────────────────────────

async def cb_review_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, shelter_id, shelter_addr = query.data.split(":", 2)
    ctx.user_data["rv_id"]   = shelter_id
    ctx.user_data["rv_addr"] = shelter_addr
    await query.message.reply_text(
        t(ctx, "review_ask", addr=shelter_addr),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )
    return REVIEW_TEXT


async def review_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["rv_text"] = update.message.text if update.message.text != "/skip" else None
    await update.message.reply_text(t(ctx, "review_photo"))
    return REVIEW_PHOTO


async def review_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    photo_id = None
    if update.message.photo:
        photo_id = update.message.photo[-1].file_id
    elif not (update.message.text and "/skip" in update.message.text):
        await update.message.reply_text(t(ctx, "photo_or_skip"))
        return REVIEW_PHOTO
    user = update.effective_user
    await save_review(ctx.user_data["rv_id"], ctx.user_data["rv_addr"],
                      user.id, user.username or user.first_name,
                      ctx.user_data.get("rv_text"), photo_id)
    await update.message.reply_text(t(ctx, "review_saved"), reply_markup=get_location_kb(ctx))
    return ConversationHandler.END


async def review_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(t(ctx, "review_cancel"), reply_markup=get_location_kb(ctx))
    return ConversationHandler.END


# ── ДИАГНОСТИКА ───────────────────────────────────────────────────────────────

async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot alive!")
    # DB check
    try:
        pool = await get_pool()
        async with pool.acquire() as c:
            await c.fetchval("SELECT 1")
        await update.message.reply_text("✅ DB connected")
    except Exception as e:
        await update.message.reply_text(f"❌ DB: {e}")
    # ArcGIS check
    try:
        r = requests.get(ARCGIS_URL,
            params={"where":"1=1","outFields":"OBJECTID","f":"json","resultRecordCount":1},
            timeout=10)
        cnt = len(r.json().get("features", []))
        await update.message.reply_text(f"✅ TA GIS API (features: {cnt})")
    except Exception as e:
        await update.message.reply_text(f"❌ TA GIS: {e}")
    # GovMap check
    try:
        r = requests.post(GOVMAP_SEARCH_URL,
            json={"keyword": "מקלט תל אביב", "type": "all"},
            headers={"Content-Type": "application/json"}, timeout=10)
        cnt = len(r.json().get("data", {}).get("Result", []))
        pyproj_ok = "✅" if _itm_to_wgs is not None else "⚠️ no pyproj"
        await update.message.reply_text(f"✅ GovMap API (results: {cnt}) {pyproj_ok}")
    except Exception as e:
        await update.message.reply_text(f"❌ GovMap: {e}")
    # Overpass check
    try:
        r = requests.post(OVERPASS_URL, data={"data": '[out:json][timeout:5];node["amenity"="shelter"](32.08,34.77,32.09,34.78);out count;'}, timeout=10)
        data = r.json()
        total = data.get("elements", [{}])[0].get("tags", {}).get("total", "?")
        await update.message.reply_text(f"✅ Overpass API (sample count: {total})")
    except Exception as e:
        await update.message.reply_text(f"❌ Overpass: {e}")


async def global_error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error("Error: %s", ctx.error, exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(f"❌ {ctx.error}")


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if "lang" not in ctx.user_data:
        try:
            ctx.user_data["lang"] = await load_user_lang(update.effective_user.id)
        except:
            ctx.user_data["lang"] = "ru"
    await update.message.reply_text(t(ctx, "send_loc_btn"), reply_markup=get_location_kb(ctx))


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    if BOT_TOKEN == "YOUR_TOKEN_HERE":
        print("❌ Set BOT_TOKEN"); return
    if not DATABASE_URL:
        print("❌ Set DATABASE_URL"); return

    import asyncio
    try:
        asyncio.get_event_loop().run_until_complete(db_init())
    except Exception as e:
        logger.error("DB init failed: %s", e)

    app = Application.builder().token(BOT_TOKEN).build()

    review_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_review_start, pattern=r"^review:")],
        states={
            REVIEW_TEXT:  [MessageHandler(filters.TEXT, review_text)],
            REVIEW_PHOTO: [MessageHandler(filters.PHOTO | filters.TEXT, review_photo)],
        },
        fallbacks=[CommandHandler("cancel", review_cancel)],
        per_message=False,
    )

    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("ping",   cmd_ping))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("lang",   cmd_lang))
    app.add_handler(review_conv)
    app.add_handler(CallbackQueryHandler(cb_lang,           pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(cb_select_shelter, pattern=r"^select:"))
    app.add_handler(CallbackQueryHandler(cb_back,           pattern=r"^back$"))
    app.add_handler(CallbackQueryHandler(cb_checkin,        pattern=r"^checkin:"))
    app.add_handler(CallbackQueryHandler(cb_checkout,       pattern=r"^checkout$"))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(global_error_handler)

    print("🚀 ялла, миклат! (nationwide) запущен.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
