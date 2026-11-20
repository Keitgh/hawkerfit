"""Community ratings for HawkerFit: 1-5 stars and a typical price level per hawker centre.

Storage:
  - Supabase (online Postgres) when SUPABASE_URL and SUPABASE_KEY are set - used on Render,
    so ratings survive restarts and redeploys.
  - Otherwise a local JSON file (data/ratings.json) - handy for local testing.

Each visitor gets one vote per centre (they can change it). Visitors are identified only by a
random ID their browser makes; we store a hash of it, never names, emails or IPs.
"""

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

LOCAL_FILE = Path(__file__).parent / "data" / "ratings.json"
CACHE_TTL = 60  # seconds before re-reading all ratings

PRICE_LABELS = {1: "Under $5", 2: "$5 - $8", 3: "Over $8"}

_lock = threading.Lock()
_cache = {"rows": None, "at": 0.0}


class RatingsError(Exception):
    pass


def _supabase() -> tuple[str, str] | None:
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "").strip()
    return (url, key) if url and key else None


def backend_name() -> str:
    return "supabase" if _supabase() else "local file"


def _sb_request(method: str, path: str, body=None, prefer: str | None = None):
    url, key = _supabase()
    headers = {"apikey": key, "Content-Type": "application/json", "User-Agent": "HawkerFit/1.0"}
    if not key.startswith("sb_"):  # legacy JWT keys also go in Authorization
        headers["Authorization"] = f"Bearer {key}"
    if prefer:
        headers["Prefer"] = prefer
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{url}/rest/v1/{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        raise RatingsError(f"Ratings database error {e.code}: {detail}")
    except (urllib.error.URLError, TimeoutError):
        raise RatingsError("Can't reach the ratings database right now.")


def _voter_hash(voter: str) -> str:
    return hashlib.sha256(f"hawkerfit:{voter}".encode()).hexdigest()[:32]


def _load_rows() -> list[dict]:
    if _supabase():
        return _sb_request("GET", "ratings?select=centre_id,stars,price&limit=50000") or []
    if LOCAL_FILE.exists():
        return list(json.loads(LOCAL_FILE.read_text(encoding="utf-8")).values())
    return []


def _rows() -> list[dict]:
    with _lock:
        if _cache["rows"] is not None and time.time() - _cache["at"] < CACHE_TTL:
            return _cache["rows"]
    rows = _load_rows()
    with _lock:
        _cache.update(rows=rows, at=time.time())
    return rows


def summary() -> dict[str, dict]:
    """centre_id -> {rating, count, price, price_label}. Never raises: no ratings on error."""
    try:
        rows = _rows()
    except RatingsError:
        return {}
    agg: dict[str, dict] = {}
    for r in rows:
        a = agg.setdefault(r["centre_id"], {"stars": [], "prices": []})
        if r.get("stars"):
            a["stars"].append(int(r["stars"]))
        if r.get("price"):
            a["prices"].append(int(r["price"]))
    out = {}
    for cid, a in agg.items():
        price = None
        if a["prices"]:
            p = sorted(a["prices"])
            price = p[len(p) // 2]  # median price level
        out[cid] = {
            "rating": round(sum(a["stars"]) / len(a["stars"]), 1) if a["stars"] else None,
            "count": len(a["stars"]),
            "price": price,
            "price_label": PRICE_LABELS.get(price),
        }
    return out


def submit(centre_id: str, voter: str, stars: int | None, price: int | None) -> None:
    """Save (or update) one visitor's vote for one centre."""
    row = {"centre_id": centre_id, "voter": _voter_hash(voter), "stars": stars, "price": price}
    if _supabase():
        _sb_request("POST", "ratings?on_conflict=centre_id,voter", [row],
                    prefer="resolution=merge-duplicates,return=minimal")
    else:
        with _lock:
            data = json.loads(LOCAL_FILE.read_text(encoding="utf-8")) if LOCAL_FILE.exists() else {}
            key = f'{centre_id}|{row["voter"]}'
            old = data.get(key, {})
            row["stars"] = stars if stars is not None else old.get("stars")
            row["price"] = price if price is not None else old.get("price")
            data[key] = row
            LOCAL_FILE.write_text(json.dumps(data), encoding="utf-8")
    with _lock:
        _cache.update(rows=None, at=0.0)  # show the new vote straight away


# SQL to create the table in Supabase (run once in the Supabase SQL editor).
SETUP_SQL = """
create table if not exists public.ratings (
  centre_id  text not null,
  voter      text not null,
  stars      smallint check (stars between 1 and 5),
  price      smallint check (price between 1 and 3),
  updated_at timestamptz not null default now(),
  primary key (centre_id, voter)
);
alter table public.ratings enable row level security;
"""
