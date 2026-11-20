"""Singapore open data for HawkerFit.

Combines, into one list of hawker centres:
  - NEA "Dates of Hawker Centres Closure" (data.gov.sg): all ~120 NEA hawker centres with
    address, GPS, photo, stall count, description and quarterly cleaning closure dates
  - NEA 2-hour weather forecast (data.gov.sg real-time API): rain near each centre right now
  - data/hawker_centres.json: our own notes (famous dishes, nearest MRT, supper spots)

Everything is cached in memory, and on disk so the app still works offline.
"""

import json
import math
import re
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).parent
DATA = BASE / "data"
SGT = timezone(timedelta(hours=8))

NEA_URL = ("https://data.gov.sg/api/action/datastore_search"
           "?resource_id=d_bda4baa634dd1cc7a6c7cad5f19e2d68&limit=1000")
WEATHER_URL = "https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast"

NEA_TTL = 6 * 3600       # hawker list/closures change rarely
WEATHER_TTL = 10 * 60    # forecast updates every ~30 min

_lock = threading.Lock()
_cache = {"centres": None, "centres_at": 0, "weather": None, "weather_at": 0}


def now_sg() -> datetime:
    return datetime.now(SGT)


def _get_json(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "HawkerFit/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _disk(name: str):
    p = DATA / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _save(name: str, obj) -> None:
    (DATA / name).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def distance_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def travel_estimates(km: float) -> dict:
    """Rough door-to-door estimates from straight-line distance (roads are ~30-40% longer)."""
    walk = round(km * 1.3 / 4.8 * 60)
    drive = round(km * 1.4 / 28 * 60) + 4  # + a few minutes to park
    return {"walk_min": max(1, walk), "drive_min": max(3, drive)}


# ------------------------------------------------------------------ hawker centres

def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]


def _parse_date(s: str):
    try:
        d, m, y = (int(x) for x in str(s).strip().split("/"))
        return date(y, m, d)
    except Exception:
        return None


def _closures(rec: dict) -> list[dict]:
    out = []
    for q in ("q1", "q2", "q3", "q4"):
        s, e = _parse_date(rec.get(f"{q}_cleaningstartdate")), _parse_date(rec.get(f"{q}_cleaningenddate"))
        if s and e:
            out.append({"start": s.isoformat(), "end": e.isoformat(), "reason": "Cleaning"})
    s, e = _parse_date(rec.get("other_works_startdate")), _parse_date(rec.get("other_works_enddate"))
    if s and e:
        remark = str(rec.get("remarks_other_works") or "").strip()
        out.append({"start": s.isoformat(), "end": e.isoformat(),
                    "reason": remark if remark and remark.lower() != "nil" else "Works"})
    return out


def _build_centres(records: list[dict]) -> list[dict]:
    curated = json.loads((DATA / "hawker_centres.json").read_text(encoding="utf-8"))
    centres, used = [], set()
    for r in records:
        try:
            lat, lng = float(r["latitude_hc"]), float(r["longitude_hc"])
        except (KeyError, TypeError, ValueError):
            continue
        if str(r.get("status", "")).lower() not in ("existing", "existing (new)", ""):
            continue
        desc = re.sub(r"\s+", " ", str(r.get("description_myenv") or "")).strip()
        official = r["name"].strip()
        paren = re.search(r"\(([^)]+)\)\s*$", official)
        if paren and re.search(r"\bBlk\b", official):
            nice = paren.group(1).strip()          # "Toa Payoh Lor 7 Blk 22 (Kim Keat ...)" -> "Kim Keat ..."
        else:
            nice = re.sub(r"\s*\([^)]*\)\s*$", "", official)  # "Maxwell Food Centre (Kim Hua Market)"
        c = {
            "id": _slug(official), "name": nice, "official_name": official,
            "address": str(r.get("address_myenv") or "").strip(),
            "lat": lat, "lng": lng,
            "photo": str(r.get("photourl") or "").replace("http://", "https://") or None,
            "food_stalls": int(r["no_of_food_stalls"]) if str(r.get("no_of_food_stalls", "")).isdigit() else None,
            "description": "" if desc.lower() == "nil" else desc,
            "closures": _closures(r),
            "known_for": [], "nearest_mrt": None, "open_late": False, "source": "NEA",
        }
        # attach our curated notes to the NEA centre at the same spot (within ~250 m)
        best = min(curated, key=lambda h: distance_km(lat, lng, h["lat"], h["lng"]))
        if best["id"] not in used and distance_km(lat, lng, best["lat"], best["lng"]) < 0.25:
            used.add(best["id"])
            c.update(known_for=best["known_for"], nearest_mrt=best["nearest_mrt"],
                     open_late=best["open_late"], area=best["area"])
        centres.append(c)
    # curated places that are not NEA-run (e.g. Lau Pa Sat) are still included
    for h in curated:
        if h["id"] not in used:
            centres.append({"id": h["id"], "name": h["name"], "address": h["area"], "lat": h["lat"],
                            "lng": h["lng"], "photo": None, "food_stalls": None, "description": "",
                            "closures": [], "known_for": h["known_for"], "nearest_mrt": h["nearest_mrt"],
                            "open_late": h["open_late"], "area": h["area"], "source": "curated"})
    # stable order so the AI prompt stays cacheable
    centres.sort(key=lambda c: c["name"].lower())
    seen, unique = set(), []
    for c in centres:
        if c["id"] in seen:
            c["id"] = f'{c["id"]}-{len(seen)}'
        seen.add(c["id"])
        unique.append(c)
    return unique


def get_centres() -> list[dict]:
    with _lock:
        if _cache["centres"] and time.time() - _cache["centres_at"] < NEA_TTL:
            return _cache["centres"]
    try:
        records = _get_json(NEA_URL)["result"]["records"]
        _save("cache_nea.json", records)
    except Exception:
        records = _disk("cache_nea.json") or []
    centres = _build_centres(records)
    with _lock:
        _cache.update(centres=centres, centres_at=time.time())
    return centres


def closure_status(c: dict, today: date | None = None) -> dict:
    """closed_today + a note about the next closure within 14 days."""
    today = today or now_sg().date()
    upcoming = None
    for cl in c["closures"]:
        s, e = date.fromisoformat(cl["start"]), date.fromisoformat(cl["end"])
        if s <= today <= e:
            return {"closed_today": True,
                    "closure_note": f'Closed today ({cl["reason"].lower()}), reopens {(e + timedelta(days=1)).strftime("%a %d %b")}'}
        if today < s <= today + timedelta(days=14) and (upcoming is None or s < upcoming[0]):
            upcoming = (s, e, cl["reason"])
    if upcoming:
        s, e, why = upcoming
        span = s.strftime("%a %d %b") if s == e else f'{s.strftime("%d %b")} - {e.strftime("%d %b")}'
        return {"closed_today": False, "closure_note": f"Closing {span} ({why.lower()})"}
    return {"closed_today": False, "closure_note": None}


# ------------------------------------------------------------------ weather

RAIN_WORDS = ("rain", "shower", "thunder")


def get_weather() -> dict | None:
    with _lock:
        if _cache["weather"] and time.time() - _cache["weather_at"] < WEATHER_TTL:
            return _cache["weather"]
    try:
        d = _get_json(WEATHER_URL, timeout=10)["data"]
        areas = [{"name": a["name"], "lat": a["label_location"]["latitude"],
                  "lng": a["label_location"]["longitude"]} for a in d["area_metadata"]]
        item = d["items"][0]
        forecasts = {f["area"]: f["forecast"] if isinstance(f["forecast"], str) else f["forecast"].get("text", "")
                     for f in item["forecasts"]}
        period = item.get("valid_period", {})
        w = {"areas": areas, "forecasts": forecasts,
             "valid_text": period.get("text") if isinstance(period, dict) else None}
        _save("cache_weather_areas.json", areas)
    except Exception:
        areas = _disk("cache_weather_areas.json")
        w = {"areas": areas, "forecasts": {}, "valid_text": None} if areas else None
    with _lock:
        _cache.update(weather=w, weather_at=time.time())
    return w


def weather_at(lat: float, lng: float, w: dict | None) -> dict | None:
    if not w or not w["forecasts"]:
        return None
    area = min(w["areas"], key=lambda a: distance_km(lat, lng, a["lat"], a["lng"]))
    text = w["forecasts"].get(area["name"])
    if not text:
        return None
    return {"area": area["name"], "forecast": text, "rainy": any(x in text.lower() for x in RAIN_WORDS)}


# ------------------------------------------------------------------ location lookup (no 3rd-party geocoder)

# Postal sector (first 2 digits) -> NEA forecast area, used for an approximate location.
_SECTORS = {
    "City": "01 02 03 04 05 06 07 08 17 18 19",
    "Bukit Merah": "09 10",
    "Clementi": "11 12 13",
    "Queenstown": "14 15 16",
    "Kallang": "20 21",
    "Tanglin": "22 23 24 25 26 27",
    "Novena": "28 29 30",
    "Toa Payoh": "31 32 33",
    "Geylang": "34 35 36 37 38 39 40 41",
    "Marine Parade": "42 43 44 45",
    "Bedok": "46 47 48",
    "Changi": "49 50 81",
    "Tampines": "52",
    "Pasir Ris": "51",
    "Hougang": "53 54 55",
    "Punggol": "82",
    "Ang Mo Kio": "56 57",
    "Bukit Timah": "58 59",
    "Jurong West": "61 62 63 64",
    "Jurong East": "60",
    "Bukit Batok": "65",
    "Bukit Panjang": "66",
    "Choa Chu Kang": "67 68",
    "Lim Chu Kang": "69 70 71",
    "Woodlands": "72 73",
    "Yishun": "75 76",
    "Mandai": "77 78",
    "Seletar": "79 80",
}
SECTOR_TO_AREA = {s: area for area, ss in _SECTORS.items() for s in ss.split()}


def locate(q: str) -> dict | None:
    """Postal code, area name (e.g. 'Bedok') or hawker centre name -> approximate lat/lng."""
    q = q.strip()
    if not q:
        return None
    areas = (get_weather() or {}).get("areas") or []
    by_area = {a["name"].lower(): a for a in areas}

    digits = re.sub(r"\D", "", q)
    if len(digits) == 6:
        area = SECTOR_TO_AREA.get(digits[:2])
        if area and area.lower() in by_area:
            a = by_area[area.lower()]
            return {"label": f"{area} (postal {digits})", "lat": a["lat"], "lng": a["lng"], "approx": True}
        return None

    ql = q.lower()
    for a in areas:
        if ql == a["name"].lower() or ql in a["name"].lower():
            return {"label": a["name"], "lat": a["lat"], "lng": a["lng"], "approx": True}
    for c in get_centres():
        hay = f'{c["name"]} {c.get("area") or ""} {c["address"]}'.lower()
        if ql in hay:
            return {"label": c["name"], "lat": c["lat"], "lng": c["lng"], "approx": False}
    return None
