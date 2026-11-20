"""Community stall directory for HawkerFit: stalls in each hawker centre, with dishes and prices.

There is no official open data listing stalls or menus, so visitors add them (optionally by
photographing a menu board), and the site owner fixes or hides anything wrong on /admin.

Storage mirrors ratings.py:
  - Supabase table `stalls` when SUPABASE_URL and SUPABASE_KEY are set (used on Render)
  - otherwise a local JSON file (data/stalls.json) for testing
"""

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import ratings  # reuses the Supabase connection helpers

LOCAL_FILE = Path(__file__).parent / "data" / "stalls.json"
_lock = threading.Lock()

MAX_DISHES = 60


class StallsError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_dishes(raw) -> list[dict]:
    """Keep only sensible {name, price} entries. Price is optional (None = not listed)."""
    out = []
    for d in (raw or [])[:MAX_DISHES]:
        if not isinstance(d, dict):
            continue
        name = " ".join(str(d.get("name") or "").split())[:60]
        if not name:
            continue
        price = d.get("price")
        try:
            price = round(float(price), 2) if price not in (None, "") else None
        except (TypeError, ValueError):
            price = None
        if price is not None and not (0 < price <= 200):
            price = None
        out.append({"name": name, "price": price})
    return out


def _local_load() -> dict:
    return json.loads(LOCAL_FILE.read_text(encoding="utf-8")) if LOCAL_FILE.exists() else {}


def _local_save(data: dict) -> None:
    LOCAL_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _sb(method, path, body=None, prefer=None):
    try:
        return ratings._sb_request(method, path, body, prefer)
    except ratings.RatingsError as e:
        raise StallsError(str(e).replace("Ratings database", "Stalls database").replace("ratings database", "stalls database"))


def list_stalls(centre_id: str, include_hidden: bool = False) -> list[dict]:
    if ratings._supabase():
        q = f"stalls?centre_id=eq.{centre_id}&select=id,centre_id,name,unit,dishes,hidden,updated_at&order=name.asc"
        rows = _sb("GET", q) or []
    else:
        rows = [s for s in _local_load().values() if s["centre_id"] == centre_id]
        rows.sort(key=lambda s: s["name"].lower())
    return [r for r in rows if include_hidden or not r.get("hidden")]


def counts() -> dict[str, int]:
    """centre_id -> number of visible stalls (for showing "12 stalls listed")."""
    if ratings._supabase():
        rows = _sb("GET", "stalls?select=centre_id&hidden=eq.false&limit=50000") or []
    else:
        rows = [s for s in _local_load().values() if not s.get("hidden")]
    out: dict[str, int] = {}
    for r in rows:
        out[r["centre_id"]] = out.get(r["centre_id"], 0) + 1
    return out


def get_stall(stall_id: str) -> dict | None:
    if ratings._supabase():
        rows = _sb("GET", f"stalls?id=eq.{stall_id}&select=*") or []
        return rows[0] if rows else None
    return _local_load().get(stall_id)


def add_stall(centre_id: str, name: str, unit: str, dishes: list[dict], contributor: str) -> dict:
    row = {"id": str(uuid.uuid4()), "centre_id": centre_id, "name": name, "unit": unit or None,
           "dishes": dishes, "contributor": ratings._voter_hash(contributor), "hidden": False,
           "created_at": _now(), "updated_at": _now()}
    if ratings._supabase():
        _sb("POST", "stalls", [row], prefer="return=minimal")
    else:
        with _lock:
            data = _local_load(); data[row["id"]] = row; _local_save(data)
    return row


def add_dishes(stall_id: str, new: list[dict]) -> dict:
    """Visitors can add dishes to an existing stall; a dish with the same name updates its price."""
    stall = get_stall(stall_id)
    if not stall or stall.get("hidden"):
        raise StallsError("That stall doesn't exist.")
    merged = {d["name"].lower(): d for d in clean_dishes(stall.get("dishes"))}
    for d in new:
        old = merged.get(d["name"].lower())
        merged[d["name"].lower()] = {"name": (old or d)["name"],
                                     "price": d["price"] if d["price"] is not None else (old or {}).get("price")}
    dishes = list(merged.values())[:MAX_DISHES]
    return update_stall(stall_id, {"dishes": dishes})


def update_stall(stall_id: str, fields: dict) -> dict:
    fields = {**fields, "updated_at": _now()}
    if ratings._supabase():
        _sb("PATCH", f"stalls?id=eq.{stall_id}", fields, prefer="return=minimal")
        return get_stall(stall_id) or {}
    with _lock:
        data = _local_load()
        if stall_id not in data:
            raise StallsError("That stall doesn't exist.")
        data[stall_id].update(fields); _local_save(data)
        return data[stall_id]


def delete_stall(stall_id: str) -> None:
    if ratings._supabase():
        _sb("DELETE", f"stalls?id=eq.{stall_id}", prefer="return=minimal")
    else:
        with _lock:
            data = _local_load(); data.pop(stall_id, None); _local_save(data)


# Run once in the Supabase SQL editor:
SETUP_SQL = """
create table if not exists public.stalls (
  id uuid primary key default gen_random_uuid(),
  centre_id text not null,
  name text not null check (char_length(name) between 1 and 80),
  unit text check (char_length(unit) <= 20),
  dishes jsonb not null default '[]'::jsonb,
  contributor text,
  hidden boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists stalls_centre_idx on public.stalls (centre_id);
alter table public.stalls enable row level security;
grant select, insert, update, delete on public.stalls to service_role;
"""
