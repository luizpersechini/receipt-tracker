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
- `src/db/` — SQLite database access (WAL, busy_timeout, FK pragmas)
- `src/services/` — OCR, FX, matching, CSV/PDF parsing, Excel/PDF reports,
  watch folder; shared helpers: `money.py` (locale-tolerant amount parser),
  `json_store.py` (atomic+locked JSON stores), `uploads.py` (size-capped
  uploads)
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
- Default OCR backend is `apple_vision` (on-device text extraction + an
  Ollama text model for structuring); `ollama_vision` is the one-shot
  vision-model alternative. Ollama calls run at temperature 0.
- Statement estornos/refunds are captured as negative `credit` rows; the
  PDF parser is Itaú-targeted (column-aware, year-boundary rollback)
- No external API calls — fully local/offline (FX rates + Gmail optional)
- QA: `qa/run_qa.py --ocr mock` is the fast gate; `--corpus N
--corpus-seed S` scores OCR total accuracy against real receipts
  (scoreboard in `qa/corpus_scoreboard.jsonl`)
