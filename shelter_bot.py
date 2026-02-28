#!/usr/bin/env python3
"""
🛡️ Tel Aviv Shelter Finder Bot v2
- Поиск ближайших убежищ по геолокации
- Отзывы и фото к убежищам
- Поиск собеседников (чекин в убежище)
"""

import os
import math
import logging
import aiosqlite
import requests
from datetime import datetime, timedelta
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "YOUR_TOKEN_HERE")
DB_PATH     = os.environ.get("DB_PATH", "shelter_data.db")
ARCGIS_URL  = (
    "https://gisn.tel-aviv.gov.il/arcgis/rest/services/"
    "WM/IView2WM/MapServer/592/query"
)
MAX_RESULTS      = 5
SEARCH_RADIUS_M  = 1000
CHECKIN_TTL_H    = 2          # чекин живёт 2 часа

# Состояния ConversationHandler
REVIEW_CHOOSE_SHELTER, REVIEW_TEXT, REVIEW_PHOTO = range(3)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

LOCATION_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("📍 Отправить геолокацию", request_location=True)]],
    resize_keyboard=True, one_time_keyboard=False,
)


# ─── DATABASE ─────────────────────────────────────────────────────────────────

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                shelter_id  TEXT NOT NULL,
                shelter_addr TEXT,
                user_id     INTEGER NOT NULL,
                username    TEXT,
                text        TEXT,
                photo_id    TEXT,
                created_at  TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS checkins (
                user_id      INTEGER PRIMARY KEY,
                username     TEXT,
                first_name   TEXT,
                shelter_id   TEXT NOT NULL,
                shelter_addr TEXT,
                shelter_name TEXT,
                lat          REAL,
                lon          REAL,
                checked_in_at TEXT NOT NULL
            )
        """)
        await db.commit()


async def save_review(shelter_id, shelter_addr, user_id, username, text, photo_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO reviews (shelter_id, shelter_addr, user_id, username, text, photo_id, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (shelter_id, shelter_addr, user_id, username, text, photo_id,
             datetime.utcnow().isoformat())
        )
        await db.commit()


async def get_reviews(shelter_id, limit=5):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reviews WHERE shelter_id=? ORDER BY created_at DESC LIMIT ?",
            (shelter_id, limit)
        ) as cur:
            return await cur.fetchall()


async def checkin(user_id, username, first_name, shelter):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO checkins (user_id, username, first_name, shelter_id, shelter_addr,
                                  shelter_name, lat, lon, checked_in_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                shelter_id=excluded.shelter_id,
                shelter_addr=excluded.shelter_addr,
                shelter_name=excluded.shelter_name,
                lat=excluded.lat, lon=excluded.lon,
                checked_in_at=excluded.checked_in_at,
                username=excluded.username,
                first_name=excluded.first_name
        """, (
            user_id, username, first_name,
            shelter["id"], shelter["address"], shelter["name"],
            shelter["lat"], shelter["lon"],
            datetime.utcnow().isoformat()
        ))
        await db.commit()


async def checkout(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM checkins WHERE user_id=?", (user_id,))
        await db.commit()


async def get_buddies(shelter_id, exclude_user_id):
    """Люди в том же убежище, чекин не старше CHECKIN_TTL_H часов."""
    cutoff = (datetime.utcnow() - timedelta(hours=CHECKIN_TTL_H)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM checkins
               WHERE shelter_id=? AND user_id!=? AND checked_in_at>?
               ORDER BY checked_in_at DESC""",
            (shelter_id, exclude_user_id, cutoff)
        ) as cur:
            return await cur.fetchall()


async def get_my_checkin(user_id):
    cutoff = (datetime.utcnow() - timedelta(hours=CHECKIN_TTL_H)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM checkins WHERE user_id=? AND checked_in_at>?",
            (user_id, cutoff)
        ) as cur:
            return await cur.fetchone()


# ─── GIS HELPERS ──────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6_371_000
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(dλ/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def shelter_type_ru(t_sug: str) -> str:
    if not t_sug:
        return "🛡️ Убежище"
    m = {
        "חניון מחסה לציבור":            "🅿️ Паркинг-убежище",
        "מקלט ציבורי במוסדות חינוך":    "🏫 Убежище (школа)",
        "מקלט ציבורי":                  "🏗️ Общественное убежище",
        "מרחב מוגן קהילתי":             "🏢 Общественное убежище",
        'ממ"ד': "🏠 Мамад",
        "ממד":  "🏠 Мамад",
    }
    for heb, rus in m.items():
        if heb in t_sug:
            return rus
    return f"🛡️ {t_sug}"


def parse_shelter(feat, user_lat, user_lon):
    geom = feat.get("geometry", {})
    a    = feat.get("attributes", {})
    slat = geom.get("y") or a.get("lat")
    slon = geom.get("x") or a.get("lon")

    addr = (a.get("Full_Address") or "").strip()
    if not addr:
        st = (a.get("shem_recho") or "").strip()
        hn = str(a.get("ms_bait") or "").strip()
        addr = f"{st} {hn}".strip() or "адрес не указан"

    uid = a.get("UniqueId") or str(a.get("oid_mitkan", ""))

    return {
        "id":       uid,
        "lat":      slat,
        "lon":      slon,
        "address":  addr,
        "name":     (a.get("shem") or "").strip(),
        "type":     shelter_type_ru(a.get("t_sug", "")),
        "hours":    (a.get("opening_times") or "").strip(),
        "phone":    (a.get("telephone_henion") or a.get("celolar") or "").strip(),
        "notes":    (a.get("hearot") or "").strip(),
        "distance": round(haversine(user_lat, user_lon, slat, slon)),
    }


def fetch_shelters(lat, lon):
    params = {
        "where": "1=1",
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "distance": SEARCH_RADIUS_M,
        "units": "esriSRUnit_Meter",
        "outFields": "*",
        "outSR": "4326",
        "returnGeometry": "true",
        "f": "json",
        "resultRecordCount": 50,
    }
    r = requests.get(ARCGIS_URL, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    shelters = [parse_shelter(f, lat, lon)
                for f in data.get("features", []) if f.get("geometry")]
    shelters.sort(key=lambda x: x["distance"])
    return shelters[:MAX_RESULTS]


# ─── KEYBOARDS ────────────────────────────────────────────────────────────────

def shelter_list_kb(shelters):
    """Инлайн-кнопки после показа убежищ."""
    rows = []
    for i, s in enumerate(shelters, 1):
        rows.append([
            InlineKeyboardButton(
                f"📝 Отзыв #{i}", callback_data=f"review:{s['id']}:{s['address'][:30]}"
            ),
            InlineKeyboardButton(
                f"🤝 Иду в #{i}", callback_data=f"checkin:{s['id']}:{i}"
            ),
        ])
    rows.append([InlineKeyboardButton("🗺️ Полная карта", url=(
        "https://gisn.tel-aviv.gov.il/iview2js4/index.aspx"
        "?zoom=14000&layers=592&back=0&year=2025"
    ))])
    return InlineKeyboardMarkup(rows)


def after_checkin_kb(shelter_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("👥 Кто ещё здесь?",  callback_data=f"buddies:{shelter_id}"),
        InlineKeyboardButton("🚪 Покинуть убежище", callback_data="checkout"),
    ]])


# ─── HANDLERS ─────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛡️ *Ближайшие убежища Тель-Авива*\n\n"
        "Нажми кнопку внизу — найду убежища рядом.\n\n"
        "Также можно:\n"
        "• Оставить отзыв и фото к убежищу\n"
        "• Отметиться в убежище и найти собеседников",
        parse_mode="Markdown",
        reply_markup=LOCATION_KB,
    )


async def handle_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lat, lon = update.message.location.latitude, update.message.location.longitude
    await update.message.reply_text("🔍 Ищу ближайшие убежища...")

    try:
        shelters = fetch_shelters(lat, lon)
    except Exception as e:
        logger.error(e)
        await update.message.reply_text("❌ Ошибка API, попробуй позже.", reply_markup=LOCATION_KB)
        return

    if not shelters:
        await update.message.reply_text(
            f"😔 Убежищ в радиусе {SEARCH_RADIUS_M} м не найдено.\n"
            "Возможно, ты за пределами Тель-Авива.",
            reply_markup=LOCATION_KB,
        )
        return

    # Сохраняем список в контекст для ConversationHandler отзывов
    ctx.user_data["shelters"] = shelters

    # Текстовый список
    lines = [f"🛡️ *Найдено {len(shelters)} убежищ:*\n"]
    for i, s in enumerate(shelters, 1):
        b = [f"*{i}. {s['type']}*"]
        if s["name"]:     b.append(f"   🏷️ {s['name']}")
        b.append(         f"   📍 {s['address']}")
        b.append(         f"   📏 {s['distance']} м")
        if s["hours"]:    b.append(f"   🕐 {s['hours']}")
        if s["phone"]:    b.append(f"   📞 {s['phone']}")
        if s["notes"]:
            note = s["notes"][:100] + "…" if len(s["notes"]) > 100 else s["notes"]
            b.append(f"   ℹ️ _{note}_")
        lines.append("\n".join(b))

    lines.append("\n_Данные: ГИС мэрии Тель-Авива_")

    await update.message.reply_text(
        "\n\n".join(lines),
        parse_mode="Markdown",
        reply_markup=LOCATION_KB,
    )

    # Геометки
    for i, s in enumerate(shelters, 1):
        await update.message.reply_venue(
            latitude=s["lat"], longitude=s["lon"],
            title=f"#{i} {s['type']}", address=s["address"],
        )

    # Инлайн-кнопки с действиями
    await update.message.reply_text(
        "Выбери действие для любого убежища:",
        reply_markup=shelter_list_kb(shelters),
    )


# ── ОТЗЫВ: начало через инлайн-кнопку ────────────────────────────────────────

async def cb_review_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Callback от кнопки '📝 Отзыв #N'"""
    query = update.callback_query
    await query.answer()
    _, shelter_id, shelter_addr = query.data.split(":", 2)
    ctx.user_data["review_shelter_id"]   = shelter_id
    ctx.user_data["review_shelter_addr"] = shelter_addr

    # Показываем существующие отзывы
    reviews = await get_reviews(shelter_id, limit=3)
    if reviews:
        rev_text = "\n\n".join(
            f"👤 *{r['username'] or 'Аноним'}*: {r['text'] or '(без текста)'}"
            for r in reviews
        )
        await query.message.reply_text(
            f"📋 *Последние отзывы:*\n\n{rev_text}",
            parse_mode="Markdown",
        )

    await query.message.reply_text(
        f"✍️ Пиши отзыв для *{shelter_addr}*\n\n"
        "Напиши текст (или /skip чтобы сразу перейти к фото):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return REVIEW_TEXT


async def review_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text and update.message.text != "/skip":
        ctx.user_data["review_text"] = update.message.text
    else:
        ctx.user_data["review_text"] = None

    await update.message.reply_text(
        "📷 Отправь фото убежища (или /skip чтобы пропустить):",
    )
    return REVIEW_PHOTO


async def review_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    photo_id = None
    if update.message.photo:
        photo_id = update.message.photo[-1].file_id
    elif update.message.text and update.message.text == "/skip":
        pass
    else:
        await update.message.reply_text("Отправь фото или /skip:")
        return REVIEW_PHOTO

    user = update.effective_user
    await save_review(
        shelter_id   = ctx.user_data["review_shelter_id"],
        shelter_addr = ctx.user_data["review_shelter_addr"],
        user_id      = user.id,
        username     = user.username or user.first_name,
        text         = ctx.user_data.get("review_text"),
        photo_id     = photo_id,
    )

    await update.message.reply_text(
        "✅ Отзыв сохранён, спасибо!\n"
        "Другие люди увидят его когда будут смотреть это убежище.",
        reply_markup=LOCATION_KB,
    )
    ctx.user_data.pop("review_shelter_id", None)
    ctx.user_data.pop("review_text", None)
    return ConversationHandler.END


async def review_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=LOCATION_KB)
    return ConversationHandler.END


# ── ЧЕКИН В УБЕЖИЩЕ ───────────────────────────────────────────────────────────

async def cb_checkin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, shelter_id, idx = query.data.split(":", 2)
    idx = int(idx) - 1

    shelters = ctx.user_data.get("shelters", [])
    shelter  = next((s for s in shelters if s["id"] == shelter_id), None)
    if not shelter and shelters:
        shelter = shelters[idx] if idx < len(shelters) else shelters[0]

    if not shelter:
        await query.message.reply_text("⚠️ Не нашёл данные об убежище, отправь геолокацию заново.")
        return

    user = update.effective_user
    await checkin(
        user_id    = user.id,
        username   = user.username,
        first_name = user.first_name,
        shelter    = shelter,
    )

    buddies = await get_buddies(shelter_id, user.id)
    buddy_text = ""
    if buddies:
        names = []
        for b in buddies:
            name = f"@{b['username']}" if b["username"] else b["first_name"] or "Аноним"
            names.append(name)
        buddy_text = f"\n\n👥 *{len(buddies)} чел. уже здесь:* {', '.join(names)}"
    else:
        buddy_text = "\n\n😶 Ты пока первый в этом убежище."

    await query.message.reply_text(
        f"✅ Ты отмечен в *{shelter['name'] or shelter['address']}*\n"
        f"Чекин действует {CHECKIN_TTL_H} часа."
        f"{buddy_text}\n\n"
        "Люди смогут увидеть тебя и написать в личку.",
        parse_mode="Markdown",
        reply_markup=after_checkin_kb(shelter_id),
    )


async def cb_buddies(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    shelter_id = query.data.split(":", 1)[1]

    buddies = await get_buddies(shelter_id, update.effective_user.id)
    if not buddies:
        await query.message.reply_text("😶 Пока никого нет.")
        return

    lines = [f"👥 *В этом убежище ({len(buddies)} чел.):*\n"]
    for b in buddies:
        name     = f"@{b['username']}" if b["username"] else b["first_name"] or "Аноним"
        dt       = datetime.fromisoformat(b["checked_in_at"])
        ago_min  = int((datetime.utcnow() - dt).total_seconds() / 60)
        ago_text = f"{ago_min} мин. назад" if ago_min < 60 else f"{ago_min//60} ч. назад"
        lines.append(f"• {name} _(отметился {ago_text})_")

    lines.append("\nНапиши им в Telegram — имя кликабельно.")
    await query.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cb_checkout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await checkout(update.effective_user.id)
    await query.message.reply_text("🚪 Ты покинул убежище.", reply_markup=LOCATION_KB)


# ── МОЙ ЧЕКИН ─────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ci = await get_my_checkin(update.effective_user.id)
    if not ci:
        await update.message.reply_text(
            "Ты не отмечен ни в каком убежище.\n"
            "Отправь геолокацию чтобы найти ближайшее.",
            reply_markup=LOCATION_KB,
        )
        return
    buddies = await get_buddies(ci["shelter_id"], update.effective_user.id)
    await update.message.reply_text(
        f"📍 Ты в *{ci['shelter_name'] or ci['shelter_addr']}*\n"
        f"👥 Рядом: {len(buddies)} чел.",
        parse_mode="Markdown",
        reply_markup=after_checkin_kb(ci["shelter_id"]),
    )


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📍 Нажми кнопку внизу чтобы найти убежища рядом:",
        reply_markup=LOCATION_KB,
    )


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    if BOT_TOKEN == "YOUR_TOKEN_HERE":
        print("❌ Установи BOT_TOKEN")
        return

    import asyncio
    asyncio.get_event_loop().run_until_complete(db_init())

    app = Application.builder().token(BOT_TOKEN).build()

    # ConversationHandler для отзыва
    review_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_review_start, pattern=r"^review:")],
        states={
            REVIEW_TEXT:  [
                MessageHandler(filters.TEXT, review_text),
            ],
            REVIEW_PHOTO: [
                MessageHandler(filters.PHOTO | filters.TEXT, review_photo),
            ],
        },
        fallbacks=[CommandHandler("cancel", review_cancel)],
        per_message=False,
    )

    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(review_conv)
    app.add_handler(CallbackQueryHandler(cb_checkin,  pattern=r"^checkin:"))
    app.add_handler(CallbackQueryHandler(cb_buddies,  pattern=r"^buddies:"))
    app.add_handler(CallbackQueryHandler(cb_checkout, pattern=r"^checkout$"))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("🚀 Бот v2 запущен.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
