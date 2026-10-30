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
│  • llm_processor   OCR pipeline orchestration + total repair  │
│  • receipt_segmenter / pdf_render / apple_vision_ocr          │
│  • statement_parser (CSV) + statement_pdf_parser (PDF)        │
│  • matching_engine fuzzy scoring with currency awareness      │
│  • money (shared parser) · uploads (size cap, safe names)     │
│  • folder_watcher, gmail_ingest, report_generator, fx_fetcher │
│  • expense_report_pdf (WeasyPrint) + report_profile/_users    │
│  • json_store — atomic + locked JSON for every sidecar store  │
└─────────────┬────────────────────────────────────┬────────────┘
              │                                    │
   ┌──────────▼─────────┐              ┌──────────▼──────────┐
   │  SQLite            │              │  Ollama :11434       │
   │  data/receipt_     │              │  vision + text       │
   │  tracker.db        │              │  models              │
   └────────────────────┘              └─────────────────────┘
```

## Repository layout

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
    llm_processor.py       OCR orchestration, prompt, normalisation,
                           evidence-anchored total repair
    text_to_receipt.py     text-model structuring (apple_vision backend)
    apple_vision_ocr.py    macOS Vision framework backend
    receipt_segmenter.py   multi-receipt detection on a page (EXIF-aware)
    typesafe_categorizer.py  opt-in Jev categorisation (batched, confidence floor)
    typesafe_segmenter.py    opt-in Jev same/different-receipt merge judgments
    ocr_settings.py        runtime OCR backend/model picker (JSON)
    statement_pdf_parser.py  Itaú PT-BR PDF parser (column-aware, credits)
    statement_parser.py    CSV ⇄ PDF dispatcher + reparse index remap
    matching_engine.py     currency-aware fuzzy match scorer (global
                           best-first assignment)
    money.py               shared locale-tolerant money parser
    folder_watcher.py      inbox scan/watchdog + PDF rasterisation
    pdf_render.py          PyMuPDF wrapper for PDF → PIL image
    gmail_ingest.py        Gmail API client + Uber receipt parser
    uber_csv.py            Uber trip-history CSV importer
    fx_fetcher.py          AwesomeAPI FX rates with daily cache
    report_generator.py    openpyxl expense-report generator
    expense_report_pdf.py  statement + reimbursement Prestação de Contas PDFs
    expense_report_xlsx.py editable front-page table as .xlsx
    receipt_pdf.py         single-receipt PDF rendering
    report_profile.py      global report front-page fields (JSON)
    report_users.py        cardholder roster + name auto-detect
    report_history.py      saved-report index (capped at 100, pruned)
    review_queue.py        pages awaiting box review before OCR
    statement_notes.py     per-transaction open-item justifications
    statement_categories.py  per-transaction report-category overrides
    statement_labels.py    per-transaction Detalhamento overrides
    statement_costcenters.py per-transaction Centro de Custo overrides
    json_store.py          atomic + locked JSON persistence shared by
                           every sidecar store above
    uploads.py             streaming upload helper (size cap, safe names)
static/                    SPA frontend (vanilla JS, no build step)
  index.html               single-page UI (Receipts/Reconcile/Statements/Users/Reports)
  css/app.css              design tokens + components
  js/app.js                all client logic
  report_assets/logo.png   company logo for the report front page
tests/                     pytest unit tests (373 passing)
qa/                        end-to-end QA harness + scored OCR corpora
scripts/                   one-shot utilities (mac app build, migrations)
data/                      SQLite + uploads + reports + report_*.json (gitignored)
docs/                      this folder
```

## Data model

Five tables. Schema lives in `src/db/database.py:init_db()`.

| Table             | What it stores                                                                                                                                                                                                                                                                                                                                                                                                         |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `receipts`        | Uploaded receipt rows. `metadata` is a JSON blob holding the extracted fields (vendor, total, date, currency, country, items, category, etc). `filepath` points at the saved image / PDF. `match_status` is one of `pending` / `matched` / `unmatched` / `reimbursed` (the last set when a reimbursement report covering the receipt is marked completed). `expense_type` is `corp_card` (default) or `reimbursement`. |
| `statements`      | Uploaded monthly statements. `transactions` is a JSON array of parsed rows — the same shape regardless of CSV or PDF source.                                                                                                                                                                                                                                                                                           |
| `matches`         | Pairs a receipt to a specific transaction inside a statement. Columns: `receipt_id`, `statement_id`, `transaction_index` (which row inside `statements.transactions`), `transaction_data` (snapshot of the row at match time), `match_score` (0–1, null for manual), `match_kind` (`auto` or `manual`).                                                                                                                |
| `expense_reports` | Excel-report history.                                                                                                                                                                                                                                                                                                                                                                                                  |
| `fx_rates`        | Cached FX rates (`date`, `from_currency`, `to_currency`, `rate`).                                                                                                                                                                                                                                                                                                                                                      |

`matches` is N:1 — a transaction can hold several rows (cumulative
pairing), but a receipt appears in at most one. Every row **must** carry
a `transaction_index`; the reconcile board groups by it, so a null index
makes a match invisible there even though the receipt shows `matched`.

**Not in SQLite** (JSON files under `data/`, gitignored — they hold
PII): `report_profile.json` (the global report front-page defaults),
`report_users.json` (the cardholder roster), `report_history.json`
(saved-report index, capped at 100), `statement_notes.json` (open-item
justifications), `statement_categories.json` / `statement_labels.json` /
`statement_costcenters.json` (per-transaction report overrides),
`ocr_config.json` (runtime backend/model picker), and
`review_queue.json` (pages awaiting box review before OCR — see the OCR
pipeline below). All of these persist through `services/json_store.py`
— per-file locks spanning each read-modify-write plus atomic
temp-file-then-replace writes, so concurrent writers (request threadpool,
folder watcher, Gmail sync) can't lose updates and a crash can't truncate
a store. The per-transaction stores are keyed by
`(statement_id, transaction_index)` and are **remapped by content** on a
re-parse. See [reports.md](reports.md).

SQLite itself opens with `journal_mode=WAL`, `busy_timeout=10s` and
`foreign_keys=ON` — readers proceed during writes and bursts queue
instead of raising "database is locked".

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
              ├─ PDF page     → review_queue.add (always — like a PDF upload)
              ├─ image, 1 rcpt → process_with_segments(skip_segmentation=True) → save
              └─ image, >1     → review_queue.add (deferred for box review)

review  →  /api/review-queue            (pending pages + suggested boxes)
           /api/review-queue/{id}/commit (OCR confirmed boxes, drop page)
           /api/review-queue/{id}        (DELETE: discard page, no OCR)
```

Both the PDF-upload and folder-scan paths funnel pages through one
**box-review queue** (`services/review_queue.py`, persisted to
`data/review_queue.json`) — every PDF page, plus any image the segmenter
splits into >1 receipt. The user confirms/adjusts the auto-detected
receipt boxes before OCR runs, so a page with several receipts becomes
several correctly-cropped rows instead of one garbled extraction.

`segment_with_boxes` tries three strategies in order: text-cluster
(on-device OCR boxes + spatial union-find, then a fixed merge-gap rule),
projection profile, contours. With `SEGMENT_BACKEND=typesafe` the merge
step is replaced by `services/typesafe_segmenter.judged_cluster_rects`:
one Jev request per photo asks, for every nearby cluster pair, "same
receipt or different receipts?" from the clusters' text; verdicts at or
above `TYPESAFE_MIN_CONFIDENCE` override the gap rule, a confident
"different" walls off transitive merges, and errors / low confidence /
fewer than 2 regions fall back to exactly the local behaviour.

**TypeSafe (Jev) category assist.** With `CATEGORY_BACKEND=typesafe`,
`statement_pdf_parser._apply_typesafe_categories` runs one batched call per
parsed statement (chunks of 50) and `llm_processor._category_with_typesafe`
rescues invalid / "other" receipt categories; both keep the keyword guess
below the confidence floor and on any `TypeSafeError`. The client
(`services/typesafe_categorizer.py`) is pure: it returns category,
confidence and probabilities, and the policy lives in the callers.

`llm_processor` dispatches on `OCR_BACKEND` (both the segment path and
the bbox/review-queue `ocr_crops` path honour it):

- **`apple_vision`** (default, the only one in the UI) — Apple's on-device
  Vision framework extracts text first (fast, accurate on printed text); a
  text-only Ollama model (`OCR_TEXT_MODEL`, e.g. `llama3.1:8b`) structures
  the OCR text into the receipt schema.
- **`ollama_vision`** — single call per segment to an Ollama vision model
  (glm-ocr/llava). Removed from the sidebar picker: a per-config, per-field
  bake-off (`qa/bakeoff.py`) had glm-ocr at **0% on every field** (it loops
  on the JSON-extraction prompt and times out) and 4× slower than apple. Kept
  reachable via `.env` only. Don't pair it with the shipped `OLLAMA_MODEL`
  (glm-ocr) — that's an OCR-only transcription model, not an extractor.

Model choice matters: total-amount accuracy is strongly model-bound
(`llama3.1:8b` ≫ `llama3.2:3b` on hard receipts).

Ollama calls run at **temperature 0** — extraction must be
deterministic (the 0.8 default made identical receipts parse
differently run to run).

On the apple_vision path the raw OCR text also drives a set of
**evidence-anchored total repairs** (`_post_process_with_ocr_text`):
a scanned TOTAL/VENTA line overrides an undersized model pick; a total
that appears **nowhere** in the text (phone-number/barcode digits) is
replaced by the scan, by a printed `subtotal + tax = total` token
triple, or by a printed tax rate (`@ 6%`); and on column-split receipts
the model's tax-exclusive pick or cash-tendered pick is repaired via
`model + tax` / `cash − change` identities printed in the text. Every
repair requires the corrected value to be literally printed on the
receipt, so clean receipts are untouched (the PT corpus stays 100%).

Both backends feed into `_normalize_one_receipt()` which:

- coerces currency to ISO 4217
- maps country names to ISO 3166-1 alpha-2
- normalises dates to `YYYY-MM-DD` (locale-aware day-first vs
  month-first, PT/ES month abbreviations)
- parses amounts via the shared `money.py` parser (locale-tolerant
  separator handling, tri-state decimal hint)
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
  - state machine over flattened lines: payments-section skip (every
    block — multi-cardholder statements repeat it), international
    8-line pattern (date / merchant / BRL / city / native amount /
    ccy / USD / "Dólar de Conversão R$ X,XX"), domestic 3-line
    fallback (date / merchant / BRL)
  - estornos/refunds captured as negative `credit` rows (they net the
    report and skip matching) — the bill payment itself stays excluded
  - year inference from the `Postagem: DD/MM/YYYY` header on page 1,
    with cross-year rollback (December rows on a January statement)

Both parsers emit the same dict shape so downstream code (matching,
reconciliation, reports) doesn't care which source was used.

## Reconciliation

Two paths land in the `matches` table:

1. **Auto-match** — `POST /api/match` runs `matching_engine.match_receipts_to_statements`,
   a weighted fuzzy score over `amount × 0.5 + date × 0.3 + vendor × 0.2`,
   assigned **globally best-score-first** (a weak early match can't steal
   a transaction from a better one). Eligibility needs the 0.5 threshold
   plus date support or a genuinely similar vendor — an exact round
   amount alone can't pair. Credit/estorno rows are excluded.
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
- **New-row flagging.** The Receipts table marks rows added since you
  last looked: a row is "new" when its `created_at` (SQLite UTC string,
  lexically comparable) is past the `receiptsSeenAt` high-water mark in
  `localStorage` (baselined to the newest row on first load, so nothing
  false-flags). New rows get a green **NEW** pill; a banner above the
  table counts them across **all** receipts — including the
  `matched`/`reimbursed` ones the Active filter hides, which is how
  freshly-synced (and already-matched) rows still surface. "Show new"
  reveals + filters to them; "Mark all seen" advances the mark.

## Where to look when something breaks

| Symptom                                            | Start here                                                                     |
| -------------------------------------------------- | ------------------------------------------------------------------------------ |
| OCR returns "OCR failed" or garbled fields         | `src/services/llm_processor.py` + `data/app_launcher.log`                      |
| Multi-receipt page split wrong                     | `src/services/receipt_segmenter.py`; with `SEGMENT_BACKEND=typesafe`, log line "typesafe judged N pair(s)" + `scripts/compare_segments.py` |
| Category wrong although TypeSafe is on             | log "keeping keyword" = below `TYPESAFE_MIN_CONFIDENCE`; `scripts/compare_categories.py` shows Jev's answer + confidence per row |
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
