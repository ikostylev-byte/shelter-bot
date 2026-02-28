#!/usr/bin/env python3
"""
🛡️ Tel Aviv Shelter Finder Bot
Получает локацию — отдаёт ближайшие убежища на карте.
API: ArcGIS REST, layer 592 (מקלטים), мэрия Тель-Авива
"""

import os
import math
import logging
import requests
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_TOKEN_HERE")

ARCGIS_URL = (
    "https://gisn.tel-aviv.gov.il/arcgis/rest/services/"
    "WM/IView2WM/MapServer/592/query"
)

MAX_RESULTS = 5
SEARCH_RADIUS_M = 1000

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Кнопка геолокации — показываем всегда внизу
LOCATION_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("📍 Отправить мою геолокацию", request_location=True)]],
    resize_keyboard=True,
    one_time_keyboard=False,
)


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6_371_000
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_shelter_type(t_sug: str) -> str:
    if not t_sug:
        return "🛡️ Убежище"
    type_map = {
        "חניון מחסה לציבור": "🅿️ Паркинг-убежище",
        "מקלט ציבורי במוסדות חינוך": "🏫 Убежище (школа)",
        "מקלט ציבורי": "🏗️ Общественное убежище",
        "מרחב מוגן קהילתי": "🏢 Общественное убежище",
        'ממ"ד': "🏠 Мамад",
        "ממד": "🏠 Мамад",
    }
    for heb, rus in type_map.items():
        if heb in t_sug:
            return rus
    return f"🛡️ {t_sug}"


def parse_shelter(feat: dict, user_lat: float, user_lon: float) -> dict:
    geom = feat.get("geometry", {})
    a = feat.get("attributes", {})
    slat = geom.get("y") or a.get("lat")
    slon = geom.get("x") or a.get("lon")

    # Адрес: берём Full_Address, если пусто — собираем из частей
    address = (a.get("Full_Address") or "").strip()
    if not address:
        street = (a.get("shem_recho") or a.get("shem_rechov") or "").strip()
        house = str(a.get("ms_bait") or a.get("mispar_bait") or "").strip()
        address = f"{street} {house}".strip() or "адрес не указан"

    name = (a.get("shem") or "").strip()
    hours = (a.get("opening_times") or "").strip()
    phone = (a.get("telephone_henion") or a.get("celolar") or "").strip()
    notes = (a.get("hearot") or "").strip()

    dist = haversine(user_lat, user_lon, slat, slon)

    return {
        "lat": slat,
        "lon": slon,
        "address": address,
        "name": name,
        "type": get_shelter_type(a.get("t_sug", "")),
        "hours": hours,
        "phone": phone,
        "notes": notes,
        "distance_m": round(dist),
    }


def fetch_nearest_shelters(lat: float, lon: float) -> list[dict]:
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
    resp = requests.get(ARCGIS_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise RuntimeError(f"API error: {data['error']}")

    shelters = [parse_shelter(f, lat, lon) for f in data.get("features", [])
                if f.get("geometry")]
    shelters.sort(key=lambda x: x["distance_m"])
    return shelters[:MAX_RESULTS]


# ─── HANDLERS ─────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛡️ *Ближайшие убежища Тель-Авива*\n\n"
        "Нажми кнопку внизу — и я сразу найду убежища рядом с тобой.",
        parse_mode="Markdown",
        reply_markup=LOCATION_KEYBOARD,
    )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Как пользоваться:*\n\n"
        "Нажми кнопку «📍 Отправить мою геолокацию» внизу экрана.\n\n"
        "Бот найдёт до 5 ближайших убежищ в радиусе 1 км с адресами, "
        "часами работы и телефонами.\n\n"
        "⚠️ Данные: ГИС мэрии Тель-Авива. "
        "Убежища за пределами ТА могут не отображаться.",
        parse_mode="Markdown",
        reply_markup=LOCATION_KEYBOARD,
    )


async def handle_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    lat, lon = loc.latitude, loc.longitude

    await update.message.reply_text("🔍 Ищу ближайшие убежища...")

    try:
        shelters = fetch_nearest_shelters(lat, lon)
    except Exception as e:
        logger.error("API error: %s", e)
        await update.message.reply_text(
            "❌ Не удалось получить данные. Попробуй через несколько секунд.",
            reply_markup=LOCATION_KEYBOARD,
        )
        return

    if not shelters:
        await update.message.reply_text(
            f"😔 Убежищ в радиусе {SEARCH_RADIUS_M} м не найдено.\n\n"
            "Возможно, ты за пределами Тель-Авива.\n"
            "Полная карта: https://gisn.tel-aviv.gov.il/iview2js4/index.aspx?layers=592",
            reply_markup=LOCATION_KEYBOARD,
        )
        return

    # Текстовый список с деталями
    lines = [f"🛡️ *Найдено {len(shelters)} убежищ поблизости:*\n"]
    for i, s in enumerate(shelters, 1):
        block = [f"*{i}. {s['type']}*"]
        if s["name"]:
            block.append(f"   🏷️ {s['name']}")
        block.append(f"   📍 {s['address']}")
        block.append(f"   📏 {s['distance_m']} м от тебя")
        if s["hours"]:
            block.append(f"   🕐 {s['hours']}")
        if s["phone"]:
            block.append(f"   📞 {s['phone']}")
        if s["notes"]:
            note = s["notes"][:120] + "..." if len(s["notes"]) > 120 else s["notes"]
            block.append(f"   ℹ️ _{note}_")
        lines.append("\n".join(block))

    lines.append("\n_Данные: ГИС мэрии Тель-Авива_")
    await update.message.reply_text(
        "\n\n".join(lines),
        parse_mode="Markdown",
        reply_markup=LOCATION_KEYBOARD,
    )

    # Геометки
    for i, s in enumerate(shelters, 1):
        await update.message.reply_venue(
            latitude=s["lat"],
            longitude=s["lon"],
            title=f"#{i} {s['type']}",
            address=s["address"],
        )

    # Кнопка на полную карту
    keyboard = [[InlineKeyboardButton(
        "🗺️ Полная карта убежищ ТА",
        url="https://gisn.tel-aviv.gov.il/iview2js4/index.aspx?zoom=14000"
            "&layers=592&back=0&year=2025"
    )]]
    await update.message.reply_text(
        "☝️ Нажми на маркер чтобы открыть в картах.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📍 Нажми кнопку внизу, чтобы отправить геолокацию:",
        reply_markup=LOCATION_KEYBOARD,
    )


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    if BOT_TOKEN == "YOUR_TOKEN_HERE":
        print("❌ Установи переменную окружения BOT_TOKEN")
        print("   export BOT_TOKEN=ваш_токен_от_@BotFather")
        return

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("🚀 Бот запущен. Ctrl+C для остановки.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
