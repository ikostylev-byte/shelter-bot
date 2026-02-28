#!/usr/bin/env python3
"""
🛡️ Tel Aviv Shelter Finder Bot v4
UX: одно сообщение, всё редактируется инлайн.
"""

import os, math, logging, asyncpg, requests
from datetime import datetime, timedelta, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters, ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest

BOT_TOKEN    = os.environ.get("BOT_TOKEN", "YOUR_TOKEN_HERE")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ARCGIS_URL   = "https://gisn.tel-aviv.gov.il/arcgis/rest/services/WM/IView2WM/MapServer/592/query"
MAX_RESULTS     = 5
SEARCH_RADIUS_M = 1000
CHECKIN_TTL_H   = 2

REVIEW_TEXT, REVIEW_PHOTO = range(2)

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

LOCATION_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("📍 Отправить геолокацию", request_location=True)]],
    resize_keyboard=True, one_time_keyboard=False,
)

_pool = None


# ─── DB ───────────────────────────────────────────────────────────────────────

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
                id SERIAL PRIMARY KEY, shelter_id TEXT NOT NULL,
                shelter_addr TEXT, user_id BIGINT NOT NULL, username TEXT,
                text TEXT, photo_id TEXT, created_at TIMESTAMPTZ DEFAULT NOW()
            )""")
        await c.execute("""
            CREATE TABLE IF NOT EXISTS checkins (
                user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
                shelter_id TEXT NOT NULL, shelter_addr TEXT, shelter_name TEXT,
                lat DOUBLE PRECISION, lon DOUBLE PRECISION,
                checked_in_at TIMESTAMPTZ DEFAULT NOW()
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


# ─── GIS ──────────────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    R = 6_371_000
    p1,p2 = math.radians(lat1), math.radians(lat2)
    dp,dl = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R*2*math.atan2(math.sqrt(a),math.sqrt(1-a))

def shelter_type_ru(t):
    if not t: return "🛡️ Убежище"
    m = {"חניון מחסה לציבור":"🅿️ Паркинг-убежище",
         "מקלט ציבורי במוסדות חינוך":"🏫 Убежище (школа)",
         "מקלט ציבורי":"🏗️ Общественное убежище",
         "מרחב מוגן קהילתי":"🏢 Общественное убежище",
         'ממ"ד':"🏠 Мамад","ממד":"🏠 Мамад"}
    for h,r in m.items():
        if h in t: return r
    return f"🛡️ {t}"

def parse_shelter(feat, ulat, ulon):
    g = feat.get("geometry",{}); a = feat.get("attributes",{})
    slat = g.get("y") or a.get("lat"); slon = g.get("x") or a.get("lon")
    addr = (a.get("Full_Address") or "").strip()
    if not addr:
        addr = f"{(a.get('shem_recho') or '').strip()} {str(a.get('ms_bait') or '').strip()}".strip() or "адрес не указан"
    return {
        "id":       a.get("UniqueId") or str(a.get("oid_mitkan","")),
        "lat":slat, "lon":slon, "address":addr,
        "name":     (a.get("shem") or "").strip(),
        "type":     shelter_type_ru(a.get("t_sug","")),
        "hours":    (a.get("opening_times") or "").strip(),
        "phone":    (a.get("telephone_henion") or a.get("celolar") or "").strip(),
        "notes":    (a.get("hearot") or "").strip(),
        "distance": round(haversine(ulat,ulon,slat,slon)),
    }

def fetch_shelters(lat, lon):
    params = {"where":"1=1","geometry":f"{lon},{lat}","geometryType":"esriGeometryPoint",
              "inSR":"4326","spatialRel":"esriSpatialRelIntersects",
              "distance":SEARCH_RADIUS_M,"units":"esriSRUnit_Meter",
              "outFields":"*","outSR":"4326","returnGeometry":"true","f":"json","resultRecordCount":50}
    r = requests.get(ARCGIS_URL, params=params, timeout=15); r.raise_for_status()
    data = r.json()
    if "error" in data: raise RuntimeError(data["error"])
    shelters = [parse_shelter(f,lat,lon) for f in data.get("features",[]) if f.get("geometry")]
    shelters.sort(key=lambda x: x["distance"])
    return shelters[:MAX_RESULTS]


# ─── MESSAGE BUILDERS ─────────────────────────────────────────────────────────

def build_list_message(shelters):
    """Список убежищ — главный экран."""
    lines = ["🛡️ *Ближайшие убежища:*\n"]
    for i, s in enumerate(shelters, 1):
        lines.append(f"*{i}.* {s['type']} — {s['address']} _{s['distance']} м_")
    lines.append("\n_Нажми на убежище чтобы узнать подробности_")
    text = "\n".join(lines)

    buttons = [[InlineKeyboardButton(f"#{i} {s['name'] or s['address'][:20]}", callback_data=f"shelter:{i-1}")]
               for i, s in enumerate(shelters, 1)]
    buttons.append([InlineKeyboardButton("🗺️ Полная карта", url="https://gisn.tel-aviv.gov.il/iview2js4/index.aspx?zoom=14000&layers=592&back=0&year=2025")])
    return text, InlineKeyboardMarkup(buttons)


async def build_detail_message(s, idx, total, user_id):
    """Детальная карточка одного убежища."""
    reviews = await get_reviews(s["id"], limit=3)
    buddies = await get_buddies(s["id"], user_id)

    lines = [f"{'◀️ ' if idx > 0 else ''}*{idx+1}/{total}* {s['type']}{'  ▶️' if idx < total-1 else ''}\n"]

    if s["name"]: lines.append(f"🏷️ *{s['name']}*")
    lines.append(f"📍 {s['address']}")
    lines.append(f"📏 {s['distance']} м от тебя")
    if s["hours"]: lines.append(f"🕐 {s['hours']}")
    if s["phone"]: lines.append(f"📞 {s['phone']}")
    if s["notes"]:
        note = s["notes"][:120] + "…" if len(s["notes"]) > 120 else s["notes"]
        lines.append(f"\nℹ️ _{note}_")

    # Кто идёт
    lines.append("")
    if buddies:
        names = [f"@{b['username']}" if b["username"] else (b["first_name"] or "Аноним") for b in buddies]
        lines.append(f"🤝 *Здесь сейчас ({len(buddies)}):* {', '.join(names)}")
    else:
        lines.append("🤝 *Здесь пока никого*")

    # Отзывы
    lines.append("")
    if reviews:
        lines.append(f"💬 *Отзывы ({len(reviews)}):*")
        for r in reviews:
            who = r["username"] or "Аноним"
            txt = r["text"] or "_(только фото)_"
            txt = txt[:80] + "…" if len(txt) > 80 else txt
            lines.append(f"• *{who}:* {txt}")
    else:
        lines.append("💬 *Отзывов пока нет — будь первым!*")

    text = "\n".join(lines)

    # Кнопки навигации + действия
    nav = []
    if idx > 0:   nav.append(InlineKeyboardButton("◀️", callback_data=f"shelter:{idx-1}"))
    nav.append(InlineKeyboardButton("📋 Список", callback_data="list"))
    if idx < total-1: nav.append(InlineKeyboardButton("▶️", callback_data=f"shelter:{idx+1}"))

    actions = [
        InlineKeyboardButton("🗺️ На карте", callback_data=f"map:{idx}"),
        InlineKeyboardButton("✍️ Отзыв",   callback_data=f"review:{s['id']}:{s['address'][:30]}"),
        InlineKeyboardButton("🤝 Иду сюда", callback_data=f"checkin:{s['id']}:{idx+1}"),
    ]
    return text, InlineKeyboardMarkup([nav, actions])


# ─── HANDLERS ─────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛡️ *Убежища Тель-Авива*\n\nНажми кнопку внизу — найду ближайшие убежища, покажу отзывы и кто туда идёт.",
        parse_mode=ParseMode.MARKDOWN, reply_markup=LOCATION_KB)


async def handle_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lat, lon = update.message.location.latitude, update.message.location.longitude
    msg = await update.message.reply_text("🔍 Ищу...", reply_markup=LOCATION_KB)

    try:
        shelters = fetch_shelters(lat, lon)
    except Exception as e:
        logger.error(e)
        await msg.edit_text("❌ Ошибка API, попробуй позже.")
        return

    if not shelters:
        await msg.edit_text(f"😔 Убежищ в радиусе {SEARCH_RADIUS_M} м не найдено.")
        return

    ctx.user_data["shelters"] = shelters
    text, kb = build_list_message(shelters)
    await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def cb_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    shelters = ctx.user_data.get("shelters", [])
    if not shelters:
        await query.message.edit_text("Отправь геолокацию заново 📍")
        return
    text, kb = build_list_message(shelters)
    await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def cb_shelter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(":")[1])
    shelters = ctx.user_data.get("shelters", [])
    if not shelters or idx >= len(shelters):
        await query.message.edit_text("Отправь геолокацию заново 📍")
        return
    text, kb = await build_detail_message(shelters[idx], idx, len(shelters), query.from_user.id)
    try:
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except BadRequest:
        pass


async def cb_map(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Отправляет геометку как отдельное сообщение."""
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(":")[1])
    shelters = ctx.user_data.get("shelters", [])
    if not shelters: return
    s = shelters[idx]
    await query.message.reply_venue(
        latitude=s["lat"], longitude=s["lon"],
        title=s["name"] or s["type"], address=s["address"])


# ── ОТЗЫВ ─────────────────────────────────────────────────────────────────────

async def cb_review_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, shelter_id, shelter_addr = query.data.split(":", 2)
    ctx.user_data["rv_id"]   = shelter_id
    ctx.user_data["rv_addr"] = shelter_addr
    await query.message.reply_text(
        f"✍️ Пиши отзыв для *{shelter_addr}*\n\nТекст (или /skip → сразу фото):",
        parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())
    return REVIEW_TEXT

async def review_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["rv_text"] = update.message.text if update.message.text != "/skip" else None
    await update.message.reply_text("📷 Фото убежища (или /skip):")
    return REVIEW_PHOTO

async def review_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    photo_id = None
    if update.message.photo:
        photo_id = update.message.photo[-1].file_id
    elif not (update.message.text and "/skip" in update.message.text):
        await update.message.reply_text("Фото или /skip:")
        return REVIEW_PHOTO
    user = update.effective_user
    await save_review(ctx.user_data["rv_id"], ctx.user_data["rv_addr"],
                      user.id, user.username or user.first_name,
                      ctx.user_data.get("rv_text"), photo_id)
    await update.message.reply_text("✅ Отзыв сохранён! Спасибо.", reply_markup=LOCATION_KB)
    return ConversationHandler.END

async def review_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=LOCATION_KB)
    return ConversationHandler.END


# ── ЧЕКИН ─────────────────────────────────────────────────────────────────────

async def cb_checkin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("✅ Отмечаю тебя!")
    _, shelter_id, idx = query.data.split(":", 2)
    shelters = ctx.user_data.get("shelters", [])
    shelter  = next((s for s in shelters if s["id"] == shelter_id), None)
    if not shelter: return

    user = query.from_user
    await do_checkin(user.id, user.username, user.first_name, shelter)

    # Обновляем карточку чтобы отразить чекин
    idx_int = int(idx) - 1
    text, kb = await build_detail_message(shelter, idx_int, len(shelters), user.id)
    try:
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except BadRequest:
        pass

    # Отдельное подтверждение
    buddies = await get_buddies(shelter_id, user.id)
    if buddies:
        names = [f"@{b['username']}" if b["username"] else (b["first_name"] or "Аноним") for b in buddies]
        await query.message.reply_text(
            f"✅ Ты в *{shelter['name'] or shelter['address']}*\n"
            f"👥 Уже здесь: {', '.join(names)}",
            parse_mode=ParseMode.MARKDOWN)
    else:
        await query.message.reply_text(
            f"✅ Ты в *{shelter['name'] or shelter['address']}*\n😶 Пока ты первый здесь.",
            parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ci = await get_my_checkin(update.effective_user.id)
    if not ci:
        await update.message.reply_text("Ты не отмечен ни в одном убежище.\nОтправь геолокацию 📍", reply_markup=LOCATION_KB)
        return
    buddies = await get_buddies(ci["shelter_id"], update.effective_user.id)
    names = [f"@{b['username']}" if b["username"] else (b["first_name"] or "Аноним") for b in buddies]
    await update.message.reply_text(
        f"📍 Ты в *{ci['shelter_name'] or ci['shelter_addr']}*\n"
        f"👥 Рядом: {', '.join(names) if names else 'никого'}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🚪 Покинуть убежище", callback_data="checkout")]]))


async def cb_checkout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await do_checkout(query.from_user.id)
    await query.message.edit_text("🚪 Ты покинул убежище.")


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📍 Нажми кнопку внизу:", reply_markup=LOCATION_KB)


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    if BOT_TOKEN == "YOUR_TOKEN_HERE":
        print("❌ Установи BOT_TOKEN"); return
    if not DATABASE_URL:
        print("❌ Установи DATABASE_URL"); return

    import asyncio
    asyncio.get_event_loop().run_until_complete(db_init())

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
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(review_conv)
    app.add_handler(CallbackQueryHandler(cb_list,    pattern=r"^list$"))
    app.add_handler(CallbackQueryHandler(cb_shelter, pattern=r"^shelter:"))
    app.add_handler(CallbackQueryHandler(cb_map,     pattern=r"^map:"))
    app.add_handler(CallbackQueryHandler(cb_checkin, pattern=r"^checkin:"))
    app.add_handler(CallbackQueryHandler(cb_checkout,pattern=r"^checkout$"))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("🚀 Бот v4 запущен.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
