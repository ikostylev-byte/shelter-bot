#!/usr/bin/env python3
"""
generate_map_data.py — собирает данные из всех статических источников
и генерирует shelters.json для веб-карты.

Запуск: python3 generate_map_data.py
Вход:   miklat_shelters.json (рядом с скриптом)
Выход:  shelters.json (для map.html)
"""
import json, math, sys, os

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    dlat = math.radians(lat2 - lat1); dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def main():
    # 1. Load miklat.co.il data
    mkl_path = os.path.join(os.path.dirname(__file__), "miklat_shelters.json")
    if not os.path.exists(mkl_path):
        print(f"❌ {mkl_path} not found"); sys.exit(1)
    
    with open(mkl_path) as f:
        mkl = json.load(f)
    print(f"✅ miklat.co.il: {len(mkl)} shelters")
    
    # 2. Build spatial index for dedup
    grid = {}
    all_shelters = []
    
    for m in mkl:
        lon, lat = m[0], m[1]
        addr = m[2] if len(m) > 2 else ""
        city = m[3] if len(m) > 3 else ""
        all_shelters.append([lon, lat, addr, city])
        key = (round(lat * 200), round(lon * 200))  # ~50m grid
        grid.setdefault(key, []).append(len(all_shelters) - 1)
    
    def is_duplicate(lat, lon):
        cy, cx = round(lat * 200), round(lon * 200)
        for dy in range(-1, 2):
            for dx in range(-1, 2):
                for idx in grid.get((cy+dy, cx+dx), []):
                    if haversine(lat, lon, all_shelters[idx][1], all_shelters[idx][0]) < 30:
                        return True
        return False
    
    # 3. Try to fetch municipal ArcGIS data (optional, adds addresses)
    added_muni = 0
    try:
        import requests
        # Same list as in shelter_bot.py — key endpoints
        ENDPOINTS = [
            ("Holon", "https://services2.arcgis.com/cjDo9oPmimdHxumn/arcgis/rest/services/%D7%9E%D7%A7%D7%9C%D7%98%D7%99%D7%9D_%D7%9E%D7%90%D7%99_2024/FeatureServer/0"),
            ("Herzliya", "https://services3.arcgis.com/9qGhZGtb39XMVQyR/arcgis/rest/services/%D7%9E%D7%A7%D7%9C%D7%98%D7%99%D7%9D_2025/FeatureServer/0"),
            ("Netanya Public", "https://services5.arcgis.com/ySpO9UsTrqoVbhLe/arcgis/rest/services/מקלטים_ציבוריים/FeatureServer/1"),
            ("Netanya Schools", "https://services5.arcgis.com/ySpO9UsTrqoVbhLe/arcgis/rest/services/בתי_ספר_עם_מקלט/FeatureServer/2"),
            ("Netanya Migunit", "https://services5.arcgis.com/ySpO9UsTrqoVbhLe/arcgis/rest/services/מיגוניות/FeatureServer/0"),
            ("Netanya Underground", "https://services5.arcgis.com/ySpO9UsTrqoVbhLe/arcgis/rest/services/מחסות_ציבוריים/FeatureServer/1383"),
            ("Ashkelon", "https://services1.arcgis.com/yAQXemoDSgzdfV2A/arcgis/rest/services/public_shelter/FeatureServer/0"),
            ("Rehovot", "https://services6.arcgis.com/U71MeVnZSuYULYvK/arcgis/rest/services/%D7%9E%D7%A7%D7%9C%D7%98%D7%99%D7%9D_%D7%A2%D7%9D_%D7%9B%D7%9C%D7%91%D7%99%D7%90_view/FeatureServer/0"),
            ("Raanana", "https://services5.arcgis.com/PtYt6sZAX61iaSv2/arcgis/rest/services/Bublic_Shelters/FeatureServer/1"),
            ("Eilat", "https://services9.arcgis.com/BtqYDIRT3FCK6rgL/arcgis/rest/services/miklatim/FeatureServer/0"),
        ]
        
        for name, url in ENDPOINTS:
            try:
                params = {"where": "1=1", "outFields": "*", "outSR": "4326",
                          "returnGeometry": "true", "f": "json", "resultRecordCount": 1000}
                r = requests.get(f"{url}/query", params=params, timeout=10)
                data = r.json()
                new = 0
                for f in data.get("features", []):
                    g = f.get("geometry", {})
                    a = f.get("attributes", {})
                    lat = g.get("y", 0); lon = g.get("x", 0)
                    if not lat or not lon or abs(lat) < 1: continue
                    
                    if not is_duplicate(lat, lon):
                        street = (a.get("Street_Nam") or a.get("Street_nam") or a.get("STREET_NAM")
                                  or a.get("shem_recho") or a.get("STREETNAME") or "").strip()
                        house = str(a.get("House_Numb") or a.get("House_num") or a.get("HOUSE_NUM")
                                    or a.get("ms_bait") or a.get("HOUSE") or "").strip()
                        sname = (a.get("name") or a.get("Name") or a.get("SITE_NAME") or 
                                 a.get("PointName") or a.get("shem") or "").strip()
                        addr = f"{street} {house}".strip() or sname
                        
                        entry = [lon, lat, addr, name.split()[0]]
                        all_shelters.append(entry)
                        key = (round(lat * 200), round(lon * 200))
                        grid.setdefault(key, []).append(len(all_shelters) - 1)
                        new += 1
                        added_muni += 1
                
                print(f"  📡 {name}: +{new} new shelters")
            except Exception as e:
                print(f"  ⚠️ {name}: {e}")
    except ImportError:
        print("⚠️ requests not installed, skipping municipal data")
    
    print(f"\n📊 Total: {len(all_shelters)} shelters ({len(mkl)} miklat + {added_muni} municipal)")
    
    # 4. Write output
    out_path = os.path.join(os.path.dirname(__file__), "shelters.json")
    with open(out_path, "w") as f:
        json.dump(all_shelters, f, ensure_ascii=False, separators=(",", ":"))
    
    size_kb = os.path.getsize(out_path) / 1024
    print(f"✅ Written {out_path} ({size_kb:.0f} KB)")
    print(f"\n🚀 Deploy map.html + shelters.json to any static hosting")

if __name__ == "__main__":
    main()
