# Receipt Tracker

Local-first expense tracker for corporate-card reconciliation. Drop a
receipt photo, scan it with on-device OCR, then drag it onto the
matching transaction on your monthly statement. Nothing leaves the
machine — vision + structuring run via Ollama, FX rates and Gmail are
optional add-ons.

## Highlights

- **Photo / PDF / HEIC receipt OCR** via Apple Vision or Ollama vision
  models (configurable backend). Multi-receipt scans on a single image
  get auto-segmented and split into one row per receipt.
- **Monthly statement parsing** — CSV (any layout pandas can read) or
  Itaú-style PT-BR PDF. The PDF parser handles two-column layouts and
  preserves native-currency amounts on international transactions.
- **Drag-and-drop reconciliation** — Reconcile tab shows transactions
  (left) and unmatched corp-card receipts (right). Drag a receipt
  onto its transaction to pair them; smart-match ribbon hints at good
  candidates. Currency-aware: a CAD 21.88 Uber receipt lines up with
  the CAD 21.88 leg of a CAD/BRL international transaction. Per-pane
  filters (search, date, amount, currency) and toggles to hide
  matched rows on either side. Reimbursement receipts are excluded
  automatically.
- **Folder-scan inbox** — drop receipts in `data/inbox/` and hit "Scan
  folder now" for batch processing. PDFs are rasterised page-by-page,
  one receipt per page.
- **Gmail ingest** — pull Uber e-receipts straight from your inbox
  (optional, requires Google OAuth setup).
- **Excel expense reports** — corp-card / reimbursement / all, in any
  target currency with cached FX rates.

## Quick start

```bash
# Prereqs: Python 3.9+, Ollama running on :11434 with the configured model

git clone https://github.com/luizpersechini/receipt-tracker.git
cd receipt-tracker
pip install -r requirements.txt --break-system-packages

cp .env.example .env  # edit as needed; defaults work for most cases
ollama pull glm-ocr   # or llama3.2:3b if using apple_vision backend

python3 -m uvicorn src.main:app --reload
# → http://localhost:8000
```

Optional: macOS double-clickable launcher app —

```bash
scripts/build_mac_app.sh
# → ~/Applications/Receipt Tracker.app
```

## Structure

```
src/
  main.py                  FastAPI entry point (lifespan + routers)
  config.py                pydantic-settings, loaded from .env
  db/database.py           SQLite schema + CRUD
  routers/                 HTTP layer, one file per resource
    receipts.py            upload / list / edit / OCR retry / backfills
    statements.py          upload / list / delete / reparse
    matches.py             auto-match + manual pair + reconcile board API
    watch.py               folder scan trigger + status
    gmail.py / uber.py     optional ingestion sources
    reports.py / fx.py     exports + currency rates
  services/
    llm_processor.py       OCR orchestration, prompt, normalisation
    apple_vision_ocr.py    macOS Vision framework backend
    receipt_segmenter.py   multi-receipt detection on a page
    statement_pdf_parser.py  Itaú PT-BR PDF parser (column-aware)
    statement_parser.py    CSV ⇄ PDF dispatcher
    matching_engine.py     currency-aware fuzzy match scorer
    folder_watcher.py      inbox scan + PDF rasterisation
    pdf_render.py          PyMuPDF wrapper for PDF → PIL image
    gmail_ingest.py        Gmail API client + Uber receipt parser
    excel_reports.py       openpyxl expense-report generator
static/                    SPA frontend (vanilla JS, no build step)
  index.html               single-page UI
  css/app.css              design tokens + components
  js/app.js                all client logic
tests/                     pytest unit tests (198 passing)
scripts/                   one-shot utilities (mac app build, migrations)
data/                      SQLite + uploads + generated reports (gitignored)
docs/                      this folder
```

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — system overview, data
  flow, key services
- [`docs/api.md`](docs/api.md) — endpoint reference
- [`docs/reconcile.md`](docs/reconcile.md) — how reconciliation works
  (auto + manual + currency awareness)
- [`docs/statements.md`](docs/statements.md) — supported statement
  formats and the Itaú PDF parser

## Configuration

All settings live in `.env`. See [`.env.example`](.env.example) for the
full annotated list. The defaults assume:

- Ollama on `localhost:11434` with `glm-ocr` for the `ollama_vision`
  backend (or `llama3.2:3b` if you switch to `apple_vision` on macOS)
- SQLite at `data/receipt_tracker.db`
- Inbox folder at `data/inbox/` (auto-created)

## Tests

```bash
python3 -m pytest
# 198 passing in ~7s
```

The PDF statement parser, OCR normalisation helpers, folder scan, and
matching engine all have unit coverage. UI changes are smoke-tested
manually via the preview MCP — see commit messages for what was
verified.

## License

Private repo — no public license declared. Ask before forking.
