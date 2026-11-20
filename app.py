"""HawkerFit - an AI hawker food and nutrition buddy for Singapore.

Run:  python -m uvicorn app:app --port 8000
Then open http://localhost:8000
"""

import base64
import json
import os
import threading
from collections import defaultdict
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

import sg_data

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")  # lets you keep ANTHROPIC_API_KEY in a .env file

MODEL = "claude-opus-5-5"
MAX_IMAGE_BYTES = 4_500_000
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

app = FastAPI(title="HawkerFit")
_client = None

# ---------------------------------------------------------------- AI usage limits
# Every AI search/scan spends your Anthropic credit, so cap it per visitor and for the whole site.
PER_VISITOR_DAILY = int(os.environ.get("PER_VISITOR_DAILY", "20"))
SITE_DAILY = int(os.environ.get("SITE_DAILY", "300"))
_usage_lock = threading.Lock()
_usage = {"day": None, "site": 0, "visitors": defaultdict(int)}


def _visitor(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")  # set by the hosting proxy
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


def take_quota(request: Request) -> str:
    """Reserve one AI call for this visitor, or refuse with a friendly 429."""
    today = sg_data.now_sg().date().isoformat()
    who = _visitor(request)
    with _usage_lock:
        if _usage["day"] != today:
            _usage.update(day=today, site=0, visitors=defaultdict(int))
        if _usage["site"] >= SITE_DAILY:
            raise HTTPException(429, "HawkerFit's AI is resting for today (daily limit reached). "
                                     "Browsing, closures and the spin wheel still work. Try again tomorrow!")
        if _usage["visitors"][who] >= PER_VISITOR_DAILY:
            raise HTTPException(429, f"You've used your {PER_VISITOR_DAILY} free AI searches for today. "
                                     "Browsing and the spin wheel still work. Come back tomorrow!")
        _usage["site"] += 1
        _usage["visitors"][who] += 1
    return who


def refund_quota(who: str) -> None:
    """Give the call back if it failed, so errors don't eat into the limit."""
    with _usage_lock:
        _usage["site"] = max(0, _usage["site"] - 1)
        _usage["visitors"][who] = max(0, _usage["visitors"][who] - 1)


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


# ---------------------------------------------------------------- centre views

def in_singapore(lat, lng) -> bool:
    return lat is not None and lng is not None and 1.15 <= lat <= 1.48 and 103.6 <= lng <= 104.1


def public_centre(c: dict, lat=None, lng=None, weather=None) -> dict:
    """What the browser gets for one centre: static info + today's status + distance."""
    out = {k: c.get(k) for k in ("id", "name", "address", "lat", "lng", "photo", "food_stalls",
                                  "description", "known_for", "nearest_mrt", "open_late", "source")}
    out.update(sg_data.closure_status(c))
    out["weather"] = sg_data.weather_at(c["lat"], c["lng"], weather)
    if in_singapore(lat, lng):
        km = sg_data.distance_km(lat, lng, c["lat"], c["lng"])
        out["distance_km"] = round(km, 1)
        out.update(sg_data.travel_estimates(km))
    else:
        out["distance_km"] = None
    return out


# ---------------------------------------------------------------- prompts

def food_system_prompt(centres: list[dict]) -> str:
    rows = []
    for c in centres:
        row = {"id": c["id"], "name": c["name"], "address": c["address"]}
        if c.get("food_stalls"):
            row["food_stalls"] = c["food_stalls"]
        if c.get("known_for"):
            row["known_for"] = c["known_for"]
        if c.get("open_late"):
            row["good_for_supper"] = True
        if c.get("description"):
            row["about"] = c["description"][:220]
        rows.append(row)
    return f"""You are "HawkerFit", a friendly Singapore hawker food guide.
You help people decide what and where to eat at Singapore hawker centres.

Rules:
- Recommend ONLY hawker centres from the list below, referring to them by their "id".
- Give 3 picks (fewer only if the request is very narrow). Each pick is one dish at one centre.
- NEVER pick a centre listed as closed today. If it is raining near the user, prefer nearby
  centres so they stay dry, and mention it.
- Respect every constraint: budget, halal, vegetarian, not spicy, area, time of day, fitness goals.
  Many hawker centres have halal and vegetarian stalls even if not listed - say so honestly
  instead of inventing stall names. If distances are given, prefer nearer centres.
- Prices are typical hawker prices in SGD (e.g. "$4-6"). They are estimates, never guarantees.
- Only name a specific stall if it appears in the centre's notes; otherwise name the dish.
- Keep a warm local tone. A light touch of Singlish is welcome but stay easy to understand.
- If the request is not about food in Singapore, gently steer back to food.

Hawker centres (official NEA list plus our notes), JSON:
{json.dumps(rows, ensure_ascii=False)}
"""


FOOD_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "description": "1-2 friendly sentences answering the user."},
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "centre_id": {"type": "string"},
                    "dish": {"type": "string"},
                    "why": {"type": "string", "description": "One or two sentences."},
                    "est_price": {"type": "string", "description": "e.g. '$4-6'"},
                    "tags": {"type": "array", "items": {"type": "string"},
                             "description": "Short tags like 'halal options', 'not spicy', 'supper'."},
                },
                "required": ["centre_id", "dish", "why", "est_price", "tags"],
                "additionalProperties": False,
            },
        },
        "tip": {"type": "string", "description": "One practical local tip."},
    },
    "required": ["reply", "picks", "tip"],
    "additionalProperties": False,
}

MENU_SYSTEM = """You are "HawkerFit", a friendly Singapore hawker food guide and sports nutritionist.
The user sends a photo of EITHER a hawker stall menu/signboard (often in Chinese, Malay, Tamil or
local shorthand) OR a plate/bowl of food.

For a menu: read every dish you can see. For a food photo: identify the dish(es) on the plate and
estimate the portion actually shown (e.g. extra egg, half the rice eaten).

For each dish:
- Keep the original name exactly as written (for a food photo, give the common local name),
  give an English name, and describe it in one or two sentences for someone who has never had it.
- Rate spiciness 0 (none) to 3 (very spicy). Note dietary points (pork, seafood, likely halal,
  vegetarian...) and say "check with the stall" when unsure.
- Copy prices only if visible; otherwise use an empty string.
- Estimate nutrition for ONE typical Singapore hawker portion (or the portion in the photo).
  Base it on typical Singapore Health Promotion Board-style values for that dish. Give whole numbers.
  These are estimates; real portions vary by stall (roughly +/-20%).
- Give a fitness_tip: one practical ordering hack for gym-goers (e.g. "ask for less rice and an
  extra egg", "choose steamed over roasted", "skip the gravy to cut sodium").

Then give gym_picks: the best choices on this menu for the user's goal, and one macro_tip.
Goals: "cut" = fat loss (high protein, lower calories), "bulk" = muscle gain (high calories AND
high protein), "maintain" = balanced. If the image has no food or menu, say so in the summary
and return empty lists."""

NUTRITION_SCHEMA = {
    "type": "object",
    "properties": {
        "calories": {"type": "integer"},
        "protein_g": {"type": "integer"},
        "carbs_g": {"type": "integer"},
        "fat_g": {"type": "integer"},
        "fibre_g": {"type": "integer"},
        "sugar_g": {"type": "integer"},
        "sodium_mg": {"type": "integer"},
    },
    "required": ["calories", "protein_g", "carbs_g", "fat_g", "fibre_g", "sugar_g", "sodium_mg"],
    "additionalProperties": False,
}

MENU_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["menu", "food", "other"]},
        "summary": {"type": "string", "description": "What this is, in 1-2 sentences."},
        "dishes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "original_name": {"type": "string"},
                    "english_name": {"type": "string"},
                    "description": {"type": "string"},
                    "spice_level": {"type": "integer", "description": "0 to 3"},
                    "dietary_notes": {"type": "string"},
                    "price": {"type": "string"},
                    "portion": {"type": "string", "description": "e.g. '1 plate (~350 g)'"},
                    "nutrition": NUTRITION_SCHEMA,
                    "fitness_tip": {"type": "string"},
                },
                "required": ["original_name", "english_name", "description", "spice_level",
                             "dietary_notes", "price", "portion", "nutrition", "fitness_tip"],
                "additionalProperties": False,
            },
        },
        "gym_picks": {
            "type": "array",
            "description": "1-3 best dishes for the user's goal.",
            "items": {
                "type": "object",
                "properties": {
                    "dish": {"type": "string", "description": "English name, matching a dish above."},
                    "reason": {"type": "string", "description": "One short sentence with the key macros."},
                },
                "required": ["dish", "reason"],
                "additionalProperties": False,
            },
        },
        "macro_tip": {"type": "string", "description": "One practical tip for hitting the goal here."},
        "recommendation": {"type": "string", "description": "What to order first if taste is the priority."},
    },
    "required": ["kind", "summary", "dishes", "gym_picks", "macro_tip", "recommendation"],
    "additionalProperties": False,
}

GOALS = {"cut": "cut (fat loss)", "maintain": "maintain (balanced)", "bulk": "bulk (muscle gain)"}


# ---------------------------------------------------------------- Claude call

def ask_claude(system: str, content, schema: dict, effort: str = "low") -> dict:
    """One structured-output call to Claude. Returns the parsed JSON object."""
    try:
        response = get_client().beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # if this model declines, the API retries on a suitable fallback
            cache_control={"type": "ephemeral"},  # caches the (stable) system prompt
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
    except TypeError as e:  # raised when no API key / credentials are configured
        if "authentication" in str(e).lower() or "api_key" in str(e).lower():
            raise HTTPException(401, "No Anthropic API key found. Add ANTHROPIC_API_KEY to the .env file and restart.")
        raise
    except anthropic.AuthenticationError:
        raise HTTPException(401, "Your Anthropic API key was rejected. Check ANTHROPIC_API_KEY in the .env file.")
    except anthropic.RateLimitError:
        raise HTTPException(429, "Too many requests right now - wait a moment and try again.")
    except anthropic.BadRequestError as e:
        raise HTTPException(400, f"Request problem: {e.message}")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"Claude API error ({e.status_code}). Try again shortly.")
    except anthropic.APIConnectionError:
        raise HTTPException(503, "Can't reach the Claude API. Check your internet connection.")

    if response.stop_reason == "refusal":
        raise HTTPException(422, "Sorry, I can't help with that one. Try asking about food!")
    if response.stop_reason == "max_tokens":
        raise HTTPException(502, "The answer was cut off. Try a simpler request.")

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise HTTPException(502, "Got an unreadable answer from the AI. Please try again.")


# ---------------------------------------------------------------- routes

@app.get("/api/centres")
def centres(lat: float | None = None, lng: float | None = None):
    weather = sg_data.get_weather()
    out = [public_centre(c, lat, lng, weather) for c in sg_data.get_centres()]
    if in_singapore(lat, lng):
        out.sort(key=lambda c: c["distance_km"])
    return {"centres": out,
            "weather_period": (weather or {}).get("valid_text"),
            "today": sg_data.now_sg().strftime("%a %d %b %Y")}


@app.get("/api/locate")
def locate(q: str):
    hit = sg_data.locate(q[:100])
    if not hit:
        raise HTTPException(404, "Couldn't find that place. Try a 6-digit postal code or an area like 'Bedok'.")
    return hit


class FoodRequest(BaseModel):
    query: str
    lat: float | None = None
    lng: float | None = None


@app.post("/api/recommend")
def recommend(req: FoodRequest, request: Request):
    query = req.query.strip()
    if not query:
        raise HTTPException(400, "Tell me what you feel like eating first!")
    if len(query) > 500:
        raise HTTPException(400, "That's a long one - please keep it under 500 characters.")

    all_centres = sg_data.get_centres()
    by_id = {c["id"]: c for c in all_centres}
    weather = sg_data.get_weather()
    now = sg_data.now_sg()

    closed = [c["id"] for c in all_centres if sg_data.closure_status(c, now.date())["closed_today"]]
    lines = [f"Today is {now.strftime('%A %d %B %Y, %I:%M %p')} in Singapore.",
             f"Closed today (do not pick): {', '.join(closed) or 'none'}."]
    if weather and weather["forecasts"]:
        rainy = sorted(a for a, f in weather["forecasts"].items()
                       if any(x in f.lower() for x in sg_data.RAIN_WORDS))
        lines.append(f"Rain expected now ({weather.get('valid_text') or 'next 2 hours'}) in: "
                     f"{', '.join(rainy) or 'nowhere - dry island-wide'}.")
    has_loc = in_singapore(req.lat, req.lng)
    if has_loc:
        near = sorted(all_centres, key=lambda c: sg_data.distance_km(req.lat, req.lng, c["lat"], c["lng"]))[:12]
        lines.append("Nearest centres to me: " + "; ".join(
            f'{c["id"]} {sg_data.distance_km(req.lat, req.lng, c["lat"], c["lng"]):.1f} km' for c in near))
    message = "\n".join(lines) + f"\n\nMy request: {query}"

    who = take_quota(request)
    try:
        result = ask_claude(food_system_prompt(all_centres), message, FOOD_SCHEMA)
    except Exception:
        refund_quota(who)
        raise

    picks = []
    for p in result.get("picks", []):
        c = by_id.get(p.get("centre_id"))
        if not c:
            continue  # ignore anything not in our list
        picks.append({**p, "centre": public_centre(c, req.lat, req.lng, weather)})
    result["picks"] = picks
    return result


@app.post("/api/menu")
def read_menu(request: Request, image: UploadFile = File(...), question: str = Form(""), goal: str = Form("maintain")):
    if image.content_type not in IMAGE_TYPES:
        raise HTTPException(400, "Please upload a JPG, PNG, WEBP or GIF photo.")
    data = image.file.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(400, "That photo is too big (max ~4.5 MB). Try a smaller one or a screenshot.")

    text = (f"Please explain this photo and estimate the nutrition. "
            f"My fitness goal: {GOALS.get(goal, GOALS['maintain'])}.")
    if question.strip():
        text += f" Also: {question.strip()[:300]}"
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": image.content_type,
                                     "data": base64.standard_b64encode(data).decode()}},
        {"type": "text", "text": text},
    ]
    who = take_quota(request)
    try:
        return ask_claude(MENU_SYSTEM, content, MENU_SCHEMA, effort="medium")
    except Exception:
        refund_quota(who)
        raise


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/about")
def about():
    return FileResponse(BASE / "static" / "about.html")


@app.get("/creator")
def creator():
    return FileResponse(BASE / "static" / "creator.html")


@app.get("/static/page.css")
def page_css():
    return FileResponse(BASE / "static" / "page.css", media_type="text/css")
