#!/usr/bin/env python3
"""
🏠 Tel Aviv Shelter Finder Bot
Получает локацию — отдаёт ближайшие убежища на карте.

API: ArcGIS REST, layer 592 (מקלטים) от мэрии Тель-Авива
"""

import os
import math
import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

MAX_RESULTS = 5          # сколько ближайших показывать
SEARCH_RADIUS_M = 1000   # радиус поиска в метрах

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2) -> float:
    """Расстояние в метрах между двумя точками (WGS84)."""
    R = 6_371_000
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_shelter_type(feature: dict) -> str:
    """Возвращает читаемый тип убежища."""
    attrs = feature.get("attributes", {})
    sug = attrs.get("t_sug", "") or ""
    type_map = {
        "ממ\"ד": "🏠 Мамад (защищённая комната)",
        "מקלט": "🏗️ Убежище (мклат)",
        "מרחב מוגן קהילתי": "🏢 Общественное убежище",
        "ממד": "🏠 Мамад",
    }
    for heb, rus in type_map.items():
        if heb in sug:
            return rus
    return f"🛡️ {sug}" if sug else "🛡️ Убежище"


def get_address(attrs: dict) -> str:
    """Формирует адрес из атрибутов."""
    parts = []
    street = attrs.get("shem_rechov") or attrs.get("rechov") or ""
    house = attrs.get("mispar_bait") or attrs.get("bait") or ""
    if street:
        parts.append(street)
    if house:
        parts.append(str(house))
    return " ".join(parts) if parts else "адрес не указан"


def fetch_nearest_shelters(lat: float, lon: float) -> list[dict]:
    """
    Запрашивает убежища из ArcGIS REST API мэрии ТА.
    Возвращает список dict с полями: lat, lon, address, type, distance_m.
    """
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

    features = data.get("features", [])
    shelters = []

    for feat in features:
        geom = feat.get("geometry", {})
        slat = geom.get("y")
        slon = geom.get("x")
        if slat is None or slon is None:
            continue

        attrs = feat.get("attributes", {})
        dist = haversine(lat, lon, slat, slon)

        shelters.append({
            "lat": slat,
            "lon": slon,
            "address": get_address(attrs),
            "type": get_shelter_type(feat),
            "distance_m": round(dist),
            "capacity": attrs.get("kibolet") or attrs.get("mispar_mekomot") or "?",
        })

    # Сортируем по расстоянию, берём топ
    shelters.sort(key=lambda x: x["distance_m"])
    return shelters[:MAX_RESULTS]


# ─── HANDLERS ─────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛡️ *Ближайшие убежища Тель-Авива*\n\n"
        "Отправь мне свою геолокацию 📍, и я найду ближайшие убежища (мклатим и мамадим).\n\n"
        "_Нажми кнопку скрепки → Геопозиция_",
        parse_mode="Markdown"
    )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Как пользоваться:*\n\n"
        "1. Нажми 📎 (скрепка) в нижней панели\n"
        "2. Выбери «Геопозиция»\n"
        "3. Отправь свою текущую локацию\n\n"
        "Бот найдёт до 5 ближайших убежищ в радиусе 1 км.\n\n"
        "⚠️ *Данные: ГИС мэрии Тель-Авива*\n"
        "Убежища за пределами ТА могут не отображаться.",
        parse_mode="Markdown"
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
            "❌ Не удалось получить данные. Попробуй через несколько секунд.\n"
            f"_(ошибка: {e})_",
            parse_mode="Markdown"
        )
        return

    if not shelters:
        await update.message.reply_text(
            f"😔 Убежищ в радиусе {SEARCH_RADIUS_M} м не найдено.\n\n"
            "Возможно, ты находишься за пределами Тель-Авива. "
            "Попробуй ввести адрес вручную на карте:\n"
            "https://gisn.tel-aviv.gov.il/iview2js4/index.aspx?layers=592"
        )
        return

    # Отправляем текстовый список
    lines = [f"🛡️ *Найдено {len(shelters)} убежищ:*\n"]
    for i, s in enumerate(shelters, 1):
        lines.append(
            f"*{i}.* {s['type']}\n"
            f"   📍 {s['address']}\n"
            f"   📏 {s['distance_m']} м от тебя\n"
        )

    lines.append("\n_Данные: ГИС мэрии Тель-Авива_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    # Отправляем геометки одну за другой
    for i, s in enumerate(shelters, 1):
        await update.message.reply_venue(
            latitude=s["lat"],
            longitude=s["lon"],
            title=f"#{i} {s['type']}",
            address=s["address"] or "ТА",
        )

    # Кнопка на полную карту
    keyboard = [[InlineKeyboardButton(
        "🗺️ Открыть карту убежищ",
        url=f"https://gisn.tel-aviv.gov.il/iview2js4/index.aspx?zoom=5000"
            f"&layers=592&back=0"
    )]]
    await update.message.reply_text(
        "☝️ Нажми на маркер чтобы открыть в картах.\n"
        "Полная карта убежищ ТА — по кнопке ниже:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📍 Отправь мне геолокацию, и я найду ближайшие убежища!\n"
        "_(Скрепка → Геопозиция)_",
        parse_mode="Markdown"
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
