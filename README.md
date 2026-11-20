# HawkerFit

AI hawker food guide for Singapore: find what to eat, spin for a hawker centre, and scan food or
menus for calories and macros.

## Data
- Hawker centres, photos and cleaning closures: NEA, via data.gov.sg (Singapore Open Data Licence)
- Weather: NEA 2-hour forecast, via data.gov.sg
- AI: Claude API (Anthropic)

## Run locally
```
pip install -r requirements.txt
copy .env.example .env      # then put your ANTHROPIC_API_KEY in .env
python -m uvicorn app:app --port 8000
```

## Deploy (Render)
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
- Environment variables: `ANTHROPIC_API_KEY` (required), `PYTHON_VERSION=3.12.10`,
  optional `PER_VISITOR_DAILY` (default 20) and `SITE_DAILY` (default 300) AI calls per day.
