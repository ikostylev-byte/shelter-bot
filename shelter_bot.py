#!/usr/bin/env python3
"""
ялла, миклат! 🛡️
Отправляешь геолокацию — получаешь карту с 5 ближайшими убежищами.
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

BOT_TOKEN    = os.environ.get("BOT_TOKEN", "YOUR_TOKEN_HERE")
DATABASE_URL = os.environ.get("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)
ARCGIS_URL   = "https://gisn.tel-aviv.gov.il/arcgis/rest/services/WM/IView2WM/MapServer/592/query"
SEARCH_RADIUS_M = 2000
MAX_RESULTS     = 5
CHECKIN_TTL_H   = 2

REVIEW_TEXT, REVIEW_PHOTO = range(2)

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Кнопка геолокации внизу экрана
LOCATION_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("📍 Отправить геолокацию", request_location=True)]],
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
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl  = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def shelter_type_ru(t):
    if not t: return "🛡️ Убежище"
    m = {
        "חניון מחסה לציבור":          "🅿️ Паркинг-убежище",
        "מקלט ציבורי במוסדות חינוך":  "🏫 Убежище (школа)",
        "מקלט ציבורי":                "🏗️ Общественное убежище",
        "מקלט ציבורי נגיש":           "♿ Доступное убежище",
        "מקלט בשטח חניון":            "🅿️ Убежище (парковка)",
        "מקלט פנימי בשטח בית ספר":    "🏫 Убежище (школа)",
        "מרחב מוגן קהילתי":           "🏢 Общественное убежище",
        "מתקן מגון מני ילדים":        "👶 Убежище (дети)",
        "מתקן מגון רווחה":            "🏥 Убежище (соцслужба)",
        'ממ"ד': "🏠 Мамад", "ממד": "🏠 Мамад",
    }
    for h, r in m.items():
        if h in t: return r
    return f"🛡️ {t}"


def parse_shelter(feat, ulat, ulon):
    g = feat.get("geometry", {}); a = feat.get("attributes", {})
    slat = g.get("y") or a.get("lat")
    slon = g.get("x") or a.get("lon")
    addr = (a.get("Full_Address") or "").strip()
    if not addr:
        addr = f"{(a.get('shem_recho') or '').strip()} {str(a.get('ms_bait') or '').strip()}".strip() or "адрес не указан"
    return {
        "id":       a.get("UniqueId") or str(a.get("oid_mitkan", "")),
        "lat": slat, "lon": slon,
        "address":  addr,
        "name":     (a.get("shem") or "").strip(),
        "type":     shelter_type_ru(a.get("t_sug", "")),
        "hours":    (a.get("opening_times") or "").strip(),
        "phone":    (a.get("telephone_henion") or a.get("celolar") or "").strip(),
        "notes":    (a.get("hearot") or "").strip(),
        "distance": round(haversine(ulat, ulon, slat, slon)),
    }


def fetch_shelters(lat, lon):
    # Попытка 1: spatial query
    params = {
        "where": "1=1", "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint", "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "distance": SEARCH_RADIUS_M, "units": "esriSRUnit_Meter",
        "outFields": "*", "outSR": "4326", "returnGeometry": "true",
        "f": "json", "resultRecordCount": 100,
    }
    r = requests.get(ARCGIS_URL, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if "error" in data: raise RuntimeError(data["error"])
    features = data.get("features", [])

    # Fallback: если spatial вернул 0 — берём всё и фильтруем вручную
    if not features:
        logger.warning("Spatial query вернул 0, пробуем fallback")
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

    shelters = [parse_shelter(f, lat, lon) for f in features if f.get("geometry")]
    shelters.sort(key=lambda x: x["distance"])
    return shelters[:MAX_RESULTS]


# ─── КАРТА ────────────────────────────────────────────────────────────────────

def generate_map(user_lat, user_lon, shelters) -> BytesIO:
    from PIL import ImageDraw, ImageFont
    m = StaticMap(900, 700, url_template="https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png")
    # Убежища — красные
    for s in shelters:
        m.add_marker(CircleMarker((s["lon"], s["lat"]), "#C0392B", 30))
        m.add_marker(CircleMarker((s["lon"], s["lat"]), "white", 18))
    # Юзер — синий поверх
    m.add_marker(CircleMarker((user_lon, user_lat), "#2471A3", 22))
    m.add_marker(CircleMarker((user_lon, user_lat), "white", 12))
    image = m.render()
    w, h = image.size

    # Пересчёт координат в пиксели
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

    # Номера над маркерами убежищ
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
    await update.message.reply_text(
        "🛡️ *ялла, миклат!*\n\nОтправь геолокацию — покажу ближайшие убежища на карте.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=LOCATION_KB,
    )


async def handle_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lat = update.message.location.latitude
    lon = update.message.location.longitude
    logger.info("Location: %s %s", lat, lon)

    # Ищем убежища
    try:
        shelters = fetch_shelters(lat, lon)
    except Exception as e:
        logger.error("GIS error: %s", e, exc_info=True)
        await update.message.reply_text(f"❌ Ошибка при поиске: {e}")
        return

    if not shelters:
        await update.message.reply_text(
            f"😔 Убежищ в радиусе {SEARCH_RADIUS_M} м не найдено.\n"
            f"Координаты: {lat:.5f}, {lon:.5f}",
        )
        return

    ctx.user_data["shelters"] = shelters
    ctx.user_data["user_lat"] = lat
    ctx.user_data["user_lon"] = lon

    # Кнопки выбора — под картой или под текстом
    buttons = []
    for i, s in enumerate(shelters, 1):
        waze_url = f"https://waze.com/ul?ll={s['lat']},{s['lon']}&navigate=yes"
        gmaps_url = f"https://maps.google.com/maps?daddr={s['lat']},{s['lon']}"
        buttons.append([
            InlineKeyboardButton(f"#{i} {s['address'][:28]}", callback_data=f"select:{i-1}"),
        ])
        buttons.append([
            InlineKeyboardButton("🚗 Waze",         url=waze_url),
            InlineKeyboardButton("🗺️ Google Maps",  url=gmaps_url),
        ])
    kb = InlineKeyboardMarkup(buttons)

    # Карта + кнопки в одном сообщении
    try:
        map_buf = generate_map(lat, lon, shelters)
        caption_lines = ["🔵 ты   🔴 убежища\n"]
        for i, s in enumerate(shelters, 1):
            caption_lines.append(f"#{i} {s['address']} — {s['distance']} м")
        await update.message.reply_photo(
            photo=map_buf,
            caption="\n".join(caption_lines),
            reply_markup=kb,
        )
    except Exception as e:
        logger.error("Map error: %s", e, exc_info=True)
        await update.message.reply_text(f"⚠️ Карта не загрузилась: {e}")
        # Если карта не вышла — текстовый список с теми же кнопками
        lines = [f"*Найдено {len(shelters)} убежищ:*\n"]
        for i, s in enumerate(shelters, 1):
            line = f"*#{i}* {s['type']}\n📍 {s['address']} — _{s['distance']} м_"
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
        await query.message.reply_text("Отправь геолокацию заново 📍")
        return

    s = shelters[idx]
    user_id = query.from_user.id

    # Кто уже идёт
    buddies = await get_buddies(s["id"], user_id)
    reviews = await get_reviews(s["id"], limit=3)

    lines = [f"*{s['type']}*", f"📍 {s['address']}", f"📏 {s['distance']} м от тебя"]
    if s["hours"]: lines.append(f"🕐 {s['hours']}")
    if s["phone"]: lines.append(f"📞 {s['phone']}")
    if s["notes"]:
        note = s["notes"][:120] + "…" if len(s["notes"]) > 120 else s["notes"]
        lines.append(f"\nℹ️ _{note}_")

    lines.append("")
    if buddies:
        names = [f"@{b['username']}" if b["username"] else (b["first_name"] or "Аноним") for b in buddies]
        lines.append(f"🤝 *Идут сюда ({len(buddies)}):* {', '.join(names)}")
    else:
        lines.append("🤝 *Пока никто не отметился*")

    if reviews:
        lines.append("")
        lines.append(f"💬 *Отзывы:*")
        for r in reviews:
            txt = (r["text"] or "_(только фото)_")[:80]
            lines.append(f"• *{r['username'] or 'Аноним'}:* {txt}")

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🤝 Иду сюда", callback_data=f"checkin:{s['id']}:{idx}"),
            InlineKeyboardButton("✍️ Оставить отзыв", callback_data=f"review:{s['id']}:{s['address'][:30]}"),
        ],
        [InlineKeyboardButton("← Назад к списку", callback_data="back")],
    ])

    await query.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


async def cb_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    shelters = ctx.user_data.get("shelters", [])
    if not shelters:
        await query.message.reply_text("Отправь геолокацию заново 📍", reply_markup=LOCATION_KB)
        return
    lines = ["*Выбери убежище:*\n"]
    for i, s in enumerate(shelters, 1):
        line = f"*#{i}* {s['type']}\n📍 {s['address']} — _{s['distance']} м_"
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
    await query.answer("✅ Отмечаю!")
    _, shelter_id, idx = query.data.split(":", 2)
    shelters = ctx.user_data.get("shelters", [])
    shelter  = next((s for s in shelters if s["id"] == shelter_id), None)
    if not shelter: return

    user = query.from_user
    await do_checkin(user.id, user.username, user.first_name, shelter)

    buddies = await get_buddies(shelter_id, user.id)
    if buddies:
        names = [f"@{b['username']}" if b["username"] else (b["first_name"] or "Аноним") for b in buddies]
        buddy_text = f"👥 Ещё здесь: {', '.join(names)}"
    else:
        buddy_text = "😶 Ты пока первый здесь."

    await query.message.reply_text(
        f"✅ Отмечен в *{shelter['name'] or shelter['address']}*\n"
        f"Чекин активен {CHECKIN_TTL_H} часа.\n\n{buddy_text}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🚪 Покинуть убежище", callback_data="checkout")
        ]]),
    )


async def cb_checkout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await do_checkout(query.from_user.id)
    await query.message.reply_text("🚪 Ты покинул убежище.", reply_markup=LOCATION_KB)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ci = await get_my_checkin(update.effective_user.id)
    if not ci:
        await update.message.reply_text("Ты не отмечен ни в одном убежище.", reply_markup=LOCATION_KB)
        return
    buddies = await get_buddies(ci["shelter_id"], update.effective_user.id)
    names   = [f"@{b['username']}" if b["username"] else (b["first_name"] or "Аноним") for b in buddies]
    await update.message.reply_text(
        f"📍 Ты в *{ci['shelter_name'] or ci['shelter_addr']}*\n"
        f"👥 Рядом: {', '.join(names) if names else 'никого'}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🚪 Покинуть убежище", callback_data="checkout")
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
        f"✍️ Отзыв для *{shelter_addr}*\n\nНапиши текст (или /skip → сразу к фото):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )
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
    await update.message.reply_text("✅ Отзыв сохранён, спасибо!", reply_markup=LOCATION_KB)
    return ConversationHandler.END


async def review_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=LOCATION_KB)
    return ConversationHandler.END


# ── ДИАГНОСТИКА ───────────────────────────────────────────────────────────────

async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Бот живой!")
    try:
        pool = await get_pool()
        async with pool.acquire() as c:
            await c.fetchval("SELECT 1")
        await update.message.reply_text("✅ База подключена")
    except Exception as e:
        await update.message.reply_text(f"❌ База: {e}")
    try:
        r = requests.get(ARCGIS_URL,
            params={"where":"1=1","outFields":"OBJECTID","f":"json","resultRecordCount":1},
            timeout=10)
        cnt = len(r.json().get("features", []))
        await update.message.reply_text(f"✅ GIS API работает (features: {cnt})")
    except Exception as e:
        await update.message.reply_text(f"❌ GIS API: {e}")


async def global_error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error("Ошибка: %s", ctx.error, exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(f"❌ Ошибка: {ctx.error}")


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📍 Нажми кнопку внизу:", reply_markup=LOCATION_KB)


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    if BOT_TOKEN == "YOUR_TOKEN_HERE":
        print("❌ Установи BOT_TOKEN"); return
    if not DATABASE_URL:
        print("❌ Установи DATABASE_URL"); return

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
    app.add_handler(review_conv)
    app.add_handler(CallbackQueryHandler(cb_select_shelter, pattern=r"^select:"))
    app.add_handler(CallbackQueryHandler(cb_back,     pattern=r"^back$"))
    app.add_handler(CallbackQueryHandler(cb_checkin,  pattern=r"^checkin:"))
    app.add_handler(CallbackQueryHandler(cb_checkout, pattern=r"^checkout$"))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(global_error_handler)

    print("🚀 ялла, миклат! запущен.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
