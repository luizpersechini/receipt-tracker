# Receipt Tracker — FastAPI receipt OCR tracker using local LLM

## Stack

Python · FastAPI · Ollama (local LLM) · Pillow · pandas · openpyxl · SQLite

## Run

```
# Ollama must be running first:
ollama serve

# Then start the API (the DYLD var lets weasyprint find Homebrew's
# pango/cairo under uv's standalone Python):
DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib uv run uvicorn src.main:app --reload
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
- `pyproject.toml` + `uv.lock` — Python deps (uv; `requirements.txt` is a
  secondary pip fallback, regenerate with `uv export` if you need it in sync)
- `.env.example` — copy to `.env` and edit

## Setup (first time)

```
uv sync              # builds .venv (Python 3.13) from the hash-pinned uv.lock
ollama pull llama3   # or whichever model is configured
```

uv (>=0.11) is the package manager. `uv.lock` pins every dep to an exact
version + sha256. `.python-version` pins CPython 3.13. To re-lock with a
7-day cooldown (skip just-published versions): `uv lock --exclude-newer
$(date -v-7d +%Y-%m-%d)`.

## Don't touch

- `data/` — live receipt data and DB; propose plan before any schema changes

## Notes

- Ollama must be running locally before starting the app (the structuring
  text model runs there, even on the apple_vision backend)
- OCR backend is `apple_vision` (Apple on-device OCR + an Ollama text model,
  e.g. `llama3.1:8b`, that structures the text into JSON). This is the only
  backend offered in the UI — the `ollama_vision` one-shot vision path
  (glm-ocr/llava) scored 0% in the bake-off and was removed from the picker
  (still reachable via `OCR_BACKEND` in `.env` if you must). The structuring
  model is picked in the sidebar "OCR model" card; pick it well — accuracy is
  strongly model-bound (8b ≫ 3b on totals). Ollama calls run at temperature 0.
- Statement estornos/refunds are captured as negative `credit` rows; the
  PDF parser is Itaú-targeted (column-aware, year-boundary rollback)
- No external API calls — fully local/offline (FX rates + Gmail optional)
- QA: `qa/run_qa.py --ocr mock` is the fast gate; `--corpus N --corpus-seed S`
  scores total accuracy on Portuguese (your own receipts) + English (SROIE)
  corpora; `qa/bakeoff.py` compares OCR configs per-field (total/currency/
  date/vendor/category). Always run with `.venv/bin/python` (needs 3.10+).
