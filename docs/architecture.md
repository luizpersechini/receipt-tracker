# Architecture

The shape of the app, what each layer does, and why.

## One-liner

A FastAPI server + vanilla-JS SPA. SQLite for state. Local LLMs (via
Ollama) plus Apple Vision (macOS) for OCR. Everything runs on the
laptop — no external API calls except optional Gmail.

## Layers

```
┌───────────────────────────────────────────────────────────────┐
│  Browser (static/index.html + app.css + app.js)               │
│  • single-page UI, vanilla JS, no build step                  │
│  • drag-and-drop reconcile board, filter bar, modal stack     │
└──────────────────────────────┬────────────────────────────────┘
                               │  fetch (JSON over HTTP)
┌──────────────────────────────▼────────────────────────────────┐
│  FastAPI app (src/main.py)                                    │
│  • lifespan hook initialises DB, warms OCR model              │
│  • routers per resource: receipts, statements, matches,       │
│    watch, gmail, uber, reports, fx, preview                   │
└──────────────────────────────┬────────────────────────────────┘
                               │  pure Python calls
┌──────────────────────────────▼────────────────────────────────┐
│  Services (src/services/)                                     │
│  • llm_processor   OCR pipeline orchestration                 │
│  • receipt_segmenter / pdf_render / apple_vision_ocr          │
│  • statement_parser (CSV) + statement_pdf_parser (PDF)        │
│  • matching_engine fuzzy scoring with currency awareness      │
│  • folder_watcher, gmail_ingest, excel_reports, fx_rates      │
│  • expense_report_pdf (WeasyPrint) + report_profile/_users    │
└─────────────┬────────────────────────────────────┬────────────┘
              │                                    │
   ┌──────────▼─────────┐              ┌──────────▼──────────┐
   │  SQLite            │              │  Ollama :11434       │
   │  data/receipt_     │              │  vision + text       │
   │  tracker.db        │              │  models              │
   └────────────────────┘              └─────────────────────┘
```

## Data model

Five tables. Schema lives in `src/db/database.py:init_db()`.

| Table             | What it stores                                                                                                                                                                                                                                                                                                      |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `receipts`        | Uploaded receipt rows. `metadata` is a JSON blob holding the extracted fields (vendor, total, date, currency, country, items, category, etc). `filepath` points at the saved image / PDF. `match_status` is one of `pending` / `matched` / `unmatched`. `expense_type` is `corp_card` (default) or `reimbursement`. |
| `statements`      | Uploaded monthly statements. `transactions` is a JSON array of parsed rows — the same shape regardless of CSV or PDF source.                                                                                                                                                                                        |
| `matches`         | Pairs a receipt to a specific transaction inside a statement. Columns: `receipt_id`, `statement_id`, `transaction_index` (which row inside `statements.transactions`), `transaction_data` (snapshot of the row at match time), `match_score` (0–1, null for manual), `match_kind` (`auto` or `manual`).             |
| `expense_reports` | Excel-report history.                                                                                                                                                                                                                                                                                               |
| `fx_rates`        | Cached FX rates (`date`, `from_currency`, `to_currency`, `rate`).                                                                                                                                                                                                                                                   |

`matches` is N:1 — a transaction can hold several rows (cumulative
pairing), but a receipt appears in at most one. Every row **must** carry
a `transaction_index`; the reconcile board groups by it, so a null index
makes a match invisible there even though the receipt shows `matched`.

**Not in SQLite** (JSON files under `data/`, gitignored — they hold
PII): `report_profile.json` (the global report front-page defaults),
`report_users.json` (the cardholder roster), `report_history.json`
(saved-report index), `statement_notes.json` (open-item
justifications), `statement_categories.json` (per-transaction
report-category overrides), and `review_queue.json` (pages awaiting box
review before OCR — see the OCR pipeline below). `statement_notes` /
`statement_categories` are keyed by `(statement_id, transaction_index)`
so they survive a re-parse. See [reports.md](reports.md).

## OCR pipeline

```
upload  →  /api/upload/receipt/preview
              ├─ image  → receipt_segmenter.segment_with_boxes
              │              → bboxes returned, frontend opens bbox editor
              │              → user adjusts, POST /commit per box
              │              → llm_processor.ocr_crops → save_receipt
              │
              └─ PDF    → pdf_render.render_pdf_to_images (per page)
                            → segment_with_boxes per page → review_queue.add
                            → frontend drains the queue: bbox editor per page,
                              POST /review-queue/{id}/commit → ocr_crops → save

folder scan → folder_watcher.scan_folder (per page)
              ├─ 1 receipt   → process_with_segments(skip_segmentation=True) → save
              └─ >1 receipts → review_queue.add (deferred for box review)

review  →  /api/review-queue            (pending pages + suggested boxes)
           /api/review-queue/{id}/commit (OCR confirmed boxes, drop page)
           /api/review-queue/{id}        (DELETE: discard page, no OCR)
```

Both the PDF-upload and folder-scan paths funnel multi-receipt pages
through one **box-review queue** (`services/review_queue.py`, persisted to
`data/review_queue.json`). The user confirms/adjusts the auto-detected
receipt boxes before OCR runs, so a page with several receipts becomes
several correctly-cropped rows instead of one garbled extraction.

`llm_processor` dispatches to one of two backends configured by
`OCR_BACKEND`:

- **`ollama_vision`** — single call per segment to the Ollama vision
  model. JSON-mode prompt; we ask for a strict schema.
- **`apple_vision`** — Apple's on-device Vision framework extracts
  text first (fast, accurate on printed text); a smaller text-only
  Ollama model (`OCR_TEXT_MODEL`, default `llama3.2:3b`) structures
  the OCR text into the receipt schema.

Both feed into `_normalize_one_receipt()` which:

- coerces currency to ISO 4217
- maps country names to ISO 3166-1 alpha-2
- normalises dates to `YYYY-MM-DD` (locale-aware day-first vs
  month-first, PT/ES month abbreviations)
- parses amounts with locale-aware decimal handling
- runs category inference + vendor cleanup + payment-method scrub

## Statement parsing

Dispatched by file extension in `statement_parser.parse_statement()`.

- `.csv` → `parse_statement_csv` (pandas, column heuristics for English
  and Portuguese headers).
- `.pdf` → `parse_statement_pdf` (PyMuPDF). Itaú-targeted:
  - column-aware text extraction (lines split into left / right
    bucket at 58% of page width, then walked top-to-bottom per
    column — necessary because Itaú's two-column layout interleaves
    line groups in default reading order)
  - state machine over flattened lines: payments-section skip,
    international 8-line pattern (date / merchant / BRL / city /
    native amount / ccy / USD / "Dólar de Conversão R$ X,XX"),
    domestic 3-line fallback (date / merchant / BRL)
  - year inference from the `Postagem: DD/MM/YYYY` header on page 1

Both parsers emit the same dict shape so downstream code (matching,
reconciliation, reports) doesn't care which source was used.

## Reconciliation

Two paths land in the `matches` table:

1. **Auto-match** — `POST /api/match` runs `matching_engine.match_receipts_to_statements`,
   a weighted fuzzy score over `amount × 0.5 + date × 0.3 + vendor × 0.2`.
   Currency-aware: when receipt currency matches `raw_data.original_currency`
   on the transaction, native amounts are compared (CAD ↔ CAD) instead
   of the BRL bill total. Currency mismatch drops the score by 70%.

2. **Manual** — `POST /api/matches` from the reconcile board's drag-drop.
   `match_kind="manual"`, `match_score=null`, `transaction_index` is
   the specific row in the statement that the user dropped onto.

Either kind sets the receipt's `match_status` to `matched`. Unlinking
via `DELETE /api/matches/{id}` flips it back to `pending` (only if no
other matches across statements reference it).

Auto-match **preserves manual pairings**: it deletes only auto-kind
rows, snapshots the manual ones, and feeds the matching engine a
`locked_txns` set so it can't propose a slot a manual match already
owns. A transaction can collect several receipts (cumulative); a
receipt is still 1:1.

**Critical:** every match row must carry a `transaction_index`. The
reconcile board groups matches by index to render them; rows with
`transaction_index=NULL` are invisible on the board even though
their linked receipt shows `matched`. The auto-matcher persists the
index alongside the score and snapshot; manual matches always have
it (the drop target supplies it). If you ever see a paired count
mismatch between the receipts table and the board, look here first.

## Report pipeline (combined PDF)

`POST` of nothing — it's a `GET /api/statements/{id}/report?user_id=`
that streams a self-contained PDF built by
`expense_report_pdf.build_statement_report`:

```
build_statement_report(id, user_id)
  → _assemble(id)          load statement + matches + receipts;
                           bucket each charge into a form column;
                           normalise each receipt image to a data URI;
                           extract IOF from the saved statement PDF
  → _resolve_profile(...)  user_id → guess-by-name → global default
  → HTML (front page + 3-col receipt grid) → WeasyPrint → bytes
```

Cardholder roster lives in `report_users.py`;
`guess_user_for_statement` matches the statement's filename + first-page
text against user names. Full detail in [reports.md](reports.md).

## Frontend conventions

- Single `app.js`, no framework, no build. Closures group related
  state (`Reconcile`, scan modal helpers, statement detail helpers).
- All CSS lives in `static/css/app.css`. Design tokens at the top
  (`--bg`, `--text`, `--accent`, `--green`, etc) drive every
  component. New features add scoped `.feature-*` classes; never
  override base tokens.
- Cache-busting via `?v=N` query string on the script + stylesheet
  tags. Bump in `static/index.html` when shipping changes that need
  immediate visibility.
- Persisted state (filter bars, sort order, last-sync timestamps,
  reconcile flash dismissals) lives in `localStorage` under `rt.*`
  keys.

## Where to look when something breaks

| Symptom                                            | Start here                                                                     |
| -------------------------------------------------- | ------------------------------------------------------------------------------ |
| OCR returns "OCR failed" or garbled fields         | `src/services/llm_processor.py` + `data/app_launcher.log`                      |
| Multi-receipt page split wrong                     | `src/services/receipt_segmenter.py`                                            |
| Statement PDF parses zero rows or wrong currencies | `src/services/statement_pdf_parser.py` (column split, regexes)                 |
| Auto-match misses obvious pairs                    | `src/services/matching_engine.py` `_score()` weights                           |
| Reconcile board shows fewer matches than DB has    | match rows missing `transaction_index` — see migration notes in commit history |
| Receipts table shows wrong day (off-by-one)        | `static/js/app.js` `fmt.date()` — timezone fix uses local-date components      |
| Report PDF blank / WeasyPrint error                | `expense_report_pdf.py`; check `pango`/`cairo` system libs are installed       |
| Report front page has the wrong person             | `report_users.guess_user_for_statement` — or just pick the user in the modal   |
| Report charge in the wrong category column         | `expense_report_pdf._bucket()` keyword/category map                            |
| `ModuleNotFoundError` after a fresh checkout       | wrong interpreter — use the venv (`.venv/bin/python`), needs Python 3.10+      |

## Runtime / dependencies

Python **3.10+** (developed on 3.13). The app is run from a `uv` venv
(`.venv/`); `.claude/launch.json` points the preview server at
`.venv/bin/python` so it matches. `requirements.lock` is the
reproducible install. The version floor exists because the security
pins (Starlette ≥1, Pillow ≥12) only ship on the newer line — see the
README's _Security & dependencies_ section.
