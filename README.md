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
  automatically. **Cumulative matching**: several receipts can pay one
  charge (hotel folio = room + restaurant + parking) with a progress
  chip showing the running total vs the bill.
- **Combined expense-report PDF** — one click on the reconcile header
  builds the corporate "Prestação de Contas" form: a categorised
  front-page summary with a signature footer, then receipt-image grid
  pages packing ~9 readable receipts per page. Open items (charges with
  no receipt) print a "sem comprovante — justificativa" line. A second
  **reimbursement-only** variant (Receipts tab) builds the same form
  from reimbursement-type receipts with no statement/matching. See
  [`docs/reports.md`](docs/reports.md).
- **Cardholder roster (Users tab)** — save report profiles per person
  (name, cost centre, bank, CPF/RG). When you generate a report the
  cardholder is auto-detected from the statement's name.
- **Folder-scan inbox** — drop receipts in `data/inbox/` and hit "Scan
  folder now" for batch processing. PDFs are rasterised page-by-page,
  one receipt per page.
- **Gmail ingest** — pull Uber e-receipts straight from your inbox
  (optional, requires Google OAuth setup).
- **Excel expense reports** — corp-card / reimbursement / all, in any
  target currency with cached FX rates.

## Quick start

The dependency pins require **Python 3.10+** (they close CVEs in
Starlette and Pillow that only have fixes on the newer line — see
[Security & dependencies](#security--dependencies)). The repo is
developed and tested on 3.13 via a `uv` venv.

```bash
# Prereqs: Python 3.10+ (3.13 recommended), uv, Ollama on :11434

git clone https://github.com/luizpersechini/receipt-tracker.git
cd receipt-tracker

uv venv --python 3.13
uv pip sync requirements.lock        # exact, reproducible install
# (or: uv pip install -r requirements.txt for the loose pins)

cp .env.example .env  # edit as needed; defaults work for most cases
ollama pull glm-ocr   # or llama3.2:3b if using apple_vision backend

.venv/bin/python -m uvicorn src.main:app --reload
# → http://localhost:8000
```

`weasyprint` (PDF report) needs the system libs `pango`, `cairo`,
`gdk-pixbuf` — `brew install pango` on macOS covers them.

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
    expense_report_pdf.py  statement + reimbursement Prestação de Contas PDFs
    report_profile.py      global report front-page fields (JSON)
    report_users.py        cardholder roster + name auto-detect
    report_history.py      saved-report index (data/reports/history.json)
    statement_notes.py     per-transaction open-item justifications
    statement_categories.py  per-transaction report-category overrides
static/                    SPA frontend (vanilla JS, no build step)
  index.html               single-page UI (Receipts/Reconcile/Statements/Users/Reports)
  css/app.css              design tokens + components
  js/app.js                all client logic
  report_assets/logo.png   company logo for the report front page
tests/                     pytest unit tests (226 passing)
scripts/                   one-shot utilities (mac app build, migrations)
data/                      SQLite + uploads + reports + report_*.json (gitignored)
docs/                      this folder
```

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — system overview, data
  flow, key services
- [`docs/api.md`](docs/api.md) — endpoint reference
- [`docs/reconcile.md`](docs/reconcile.md) — how reconciliation works
  (auto + manual + cumulative + currency awareness)
- [`docs/statements.md`](docs/statements.md) — supported statement
  formats and the Itaú PDF parser
- [`docs/reports.md`](docs/reports.md) — the combined expense-report PDF
  and the cardholder roster / name auto-detect

## Configuration

All settings live in `.env`. See [`.env.example`](.env.example) for the
full annotated list. The defaults assume:

- Ollama on `localhost:11434` with `glm-ocr` for the `ollama_vision`
  backend (or `llama3.2:3b` if you switch to `apple_vision` on macOS)
- SQLite at `data/receipt_tracker.db`
- Inbox folder at `data/inbox/` (auto-created)

## Tests

```bash
.venv/bin/python -m pytest
# 226 passing in ~5s
```

The PDF statement parser, OCR normalisation helpers, folder scan,
matching engine, expense-report builder, and cardholder roster all have
unit coverage. UI changes are smoke-tested manually via the preview MCP
— see commit messages for what was verified.

## Security & dependencies

- **Python 3.10+ required.** The pins close CVE-2026-48710 (Starlette
  host-header auth bypass) and CVE-2026-42308–11 (Pillow buffer overflow
  on untrusted image input); the fixed versions only ship on the newer
  line, which needs ≥3.10. Developed on 3.13.
- `requirements.lock` is the reproducible install (`uv pip sync`);
  `requirements.txt` holds the loose minimum-version pins.
- All third-party libraries that have py3.13 wheels are pinned at
  versions with those wheels, so a fresh `uv pip sync` never falls back
  to building from source.
- `data/` (DB, uploads, generated reports, `report_profile.json`,
  `report_users.json`) is gitignored — it holds receipts, bank details
  and CPF/RG.

## License

Private repo — no public license declared. Ask before forking.
