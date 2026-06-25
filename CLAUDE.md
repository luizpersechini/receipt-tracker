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
- OCR backend is platform-adaptive (blank `OCR_BACKEND` resolves per-OS):
  `apple_vision` on macOS, `paddle_ocr` (PaddleOCR, cross-platform) on
  Windows/Linux. Both do on-device text extraction + an Ollama text model for
  structuring — only the OCR engine differs, so the rest of the pipeline is
  identical. `ollama_vision` is the one-shot vision-model alternative. Ollama
  calls run at temperature 0.
- Windows/Linux setup for `paddle_ocr`: `pip install paddleocr paddlepaddle`
  (CPU; first OCR run auto-downloads the small PP-OCR models). The Mac-only
  Apple Vision pyobjc deps are a no-op off Darwin.
- QA can A/B the text-extraction backends: `qa/run_qa.py --ocr real --backend
  {apple_vision|paddle_ocr} --corpus N` scores total accuracy per backend.
- Structuring backend (`STRUCTURE_BACKEND`, separate from the OCR backend):
  `ollama` (default, fully local) turns OCR'd text into receipt JSON with a
  local model; `anthropic` sends only the receipt *text* (never the image) to
  the hosted Claude API — opt-in, needs `ANTHROPIC_API_KEY`, lets a low-powered
  machine use a much stronger model for the accuracy-critical structuring step
  (`claude-haiku-4-5` is the cheap default). OCR always stays local either way.
- Statement estornos/refunds are captured as negative `credit` rows; the
  PDF parser is Itaú-targeted (column-aware, year-boundary rollback)
- Local/offline by default — no external API calls unless you opt into
  `STRUCTURE_BACKEND=anthropic` (FX rates + Gmail also optional)
- QA: `qa/run_qa.py --ocr mock` is the fast gate; `--corpus N
--corpus-seed S` scores OCR total accuracy against real receipts
  (scoreboard in `qa/corpus_scoreboard.jsonl`)
