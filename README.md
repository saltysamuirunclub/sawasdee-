# Salty Island Run Club

Club app and personal AI running coach for Koh Samui. You install it on iPhone or iPad
from Safari ("Add to Home Screen").

**Status:** Phase 1, step 1 is done (project skeleton, database, translations).
The full setup and deploy guide will be added in step 8.

## Run it locally

```bash
cd server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp ../.env.example ../.env      # then fill in values
uvicorn app.main:app --reload   # http://localhost:8000/healthz
pytest                          # run the tests
```

## Layout

```
server/app/        backend (FastAPI + SQLite)
server/app/locales translations (en, de); add a JSON file to add a language
server/tests/      tests
coach.md           coach instructions sent to Claude
.env.example       list of required settings (secrets go in .env, never in git)
```
