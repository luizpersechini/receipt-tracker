# Receipt Tracker — FastAPI receipt OCR tracker using local LLM

## Stack

Python · FastAPI · Ollama (local LLM) · Pillow · pandas · openpyxl · SQLite

## Run

```
# Ollama must be running first:
ollama serve

# Then start the API:
uvicorn src.main:app --reload
```

## Structure

- `src/main.py` — FastAPI app entry point (lifespan + router registration only)
- `src/config.py` — pydantic-settings config loaded from `.env`
- `src/db/` — SQLite database access
- `src/services/` — OCR, FX, matching, CSV parsing, Excel reports, watch folder
- `src/routers/` — FastAPI routers split by domain
- `src/schemas/` — Pydantic request/response models
- `static/` — frontend (`index.html`, `css/app.css`, `js/app.js`)
- `tests/` — pytest unit tests for pure-logic services
- `qa/` — disposable end-to-end QA harness (`run_qa.py`); boots the app on a
  throwaway db/data dir and drives the real flows. See `qa/README.md`.
- `data/` — SQLite DB, uploads, generated reports (auto-created, gitignored)
- `pyproject.toml` / `requirements.txt` — Python deps
- `.env.example` — copy to `.env` and edit

## Setup (first time)

```
pip install -r requirements.txt --break-system-packages
ollama pull llama3   # or whichever model is configured
```

## Don't touch

- `data/` — live receipt data and DB; propose plan before any schema changes

## Notes

- Ollama must be running locally before starting the app
- Image uploads are processed via Pillow then passed to Ollama vision model
- No external API calls — fully local/offline
