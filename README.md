# Receipt Tracker

Local-first expense tracking and corporate-card reconciliation. Photograph a
receipt, let on-device OCR read it, drag it onto the matching line of your
monthly card statement, and export the expense report. Everything runs on
your machine by default.

[![CI](https://github.com/luizpersechini/receipt-tracker/actions/workflows/ci.yml/badge.svg)](https://github.com/luizpersechini/receipt-tracker/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

## Features

- **On-device receipt OCR.** Apple Vision on macOS, PaddleOCR elsewhere, for
  photos, PDFs and HEIC. A local Ollama text model turns the OCR text into
  structured fields (vendor, date, total, currency, category, line items).
  Multi-receipt photos are segmented into one row per receipt, with a box
  editor to confirm before OCR runs.
- **Statement import.** CSV in any layout pandas can read, or Itaú-style
  Brazilian PDF statements: two-column layout, native-currency amounts on
  international lines, year rollover, refunds as negative credit rows,
  password-protected files, and multi-cardholder statements split per holder.
- **Drag-and-drop reconciliation.** Statement lines on the left, unmatched
  receipts on the right, currency-aware match hints, cumulative matching for
  split charges (hotel folio = room + restaurant + parking), per-pane filters.
- **Expense reports.** One-click "Prestação de Contas" PDF with a categorised
  summary, signature footer and receipt-image grid; reimbursement-only
  variant; editable Excel front page; cardholder roster with auto-detection.
- **Inboxes.** Folder scan for batch processing and optional Gmail ingest for
  Uber e-receipts.
- **Optional AI assists, all opt-in and fail-safe.** Claude API for
  structuring when the machine is too small for a local model (text only,
  never the image). TypeSafe (Jev) for calibrated expense categorisation and
  for deciding which text blocks on a photo belong to the same receipt. Each
  falls back to the local heuristic on any error or low confidence.

## Quick start

Requires Python 3.10+ (developed on 3.13), [uv](https://github.com/astral-sh/uv),
and [Ollama](https://ollama.com) listening on `:11434`.

```bash
git clone https://github.com/luizpersechini/receipt-tracker.git
cd receipt-tracker

uv venv --python 3.13
uv pip sync requirements.lock        # exact, reproducible install

cp .env.example .env                 # defaults work for most setups
ollama pull llama3.1:8b              # structuring model (8b ≫ 3b on totals)

.venv/bin/python -m uvicorn src.main:app --reload
# → http://localhost:8000
```

The PDF report uses WeasyPrint, which needs `pango`, `cairo` and `gdk-pixbuf`
(`brew install pango` on macOS). On Windows, see
[docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md): PaddleOCR locally plus the
Claude API for structuring, no Ollama needed.

Optional macOS launcher app: `scripts/build_mac_app.sh` builds
`~/Applications/Receipt Tracker.app`.

## Configuration

All settings live in `.env`; [`.env.example`](.env.example) documents every
key. The ones that change behaviour:

| Variable                 | Default                          | Effect                                                                       |
| ------------------------ | -------------------------------- | ---------------------------------------------------------------------------- |
| `OCR_BACKEND`            | per OS (`apple_vision` on macOS) | Text extraction engine: `apple_vision`, `paddle_ocr`, `ollama_vision`        |
| `OCR_TEXT_MODEL`         | `llama3.2:3b`                    | Ollama model that structures OCR text (also selectable in the sidebar)       |
| `STRUCTURE_BACKEND`      | `ollama`                         | `anthropic` sends receipt text to the Claude API (needs `ANTHROPIC_API_KEY`) |
| `CATEGORY_BACKEND`       | `keyword`                        | `typesafe` categorises with Jev above `TYPESAFE_MIN_CONFIDENCE` (0.7)        |
| `SEGMENT_BACKEND`        | `local`                          | `typesafe` lets Jev decide text-cluster merges on multi-receipt photos       |
| `TYPESAFE_API_KEY`       | unset                            | Required for either TypeSafe backend; environment variable only              |
| `STATEMENT_PDF_PASSWORD` | unset                            | Opens password-protected statement PDFs                                      |
| `MAX_UPLOAD_MB`          | `25`                             | Upload size cap                                                              |

No external API is called unless one of the opt-in backends is enabled.
Before enabling a TypeSafe backend, run the read-only comparison scripts
(`scripts/compare_categories.py`, `scripts/compare_segments.py`,
`scripts/blind_segments.py`) against your own data; the measured results
for this project are in
[docs/typesafe-categorization-diagnostics.md](docs/typesafe-categorization-diagnostics.md).

## Documentation

| Document                                                                                   | Covers                                                                 |
| ------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------- |
| [docs/architecture.md](docs/architecture.md)                                               | Layers, data model, OCR pipeline, repository layout, where to look     |
| [docs/api.md](docs/api.md)                                                                 | Endpoint reference                                                     |
| [docs/reconcile.md](docs/reconcile.md)                                                     | Matching: auto, manual, cumulative, currency awareness                 |
| [docs/statements.md](docs/statements.md)                                                   | Statement formats and the Itaú PDF parser                              |
| [docs/reports.md](docs/reports.md)                                                         | Expense-report PDF, cardholder roster, name auto-detection             |
| [docs/typesafe-categorization-diagnostics.md](docs/typesafe-categorization-diagnostics.md) | What the TypeSafe assists changed, measured, plus a reusable recipe    |
| [docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md)                                             | Windows install                                                        |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)                                         | Empty app, port in use, stale server, launcher rebuild, AI assists off |

## Development

```bash
.venv/bin/python -m pytest                          # unit suite, ~20 s, fully offline
.venv/bin/python qa/run_qa.py --ocr mock            # end-to-end smoke on a throwaway sandbox
.venv/bin/python qa/run_qa.py --corpus 8 --corpus-seed 7   # scored OCR accuracy run
```

Unit tests cover the statement parser, OCR normalisation, money parsing,
matching engine, report builders, the box-review flow and both TypeSafe
clients (mocked at a single HTTP seam; the default backends are asserted to
never call out). The [QA harness](qa/README.md) boots the real app against a
sandbox and tracks total-extraction accuracy on a Portuguese receipt corpus
and the public SROIE English set. CI runs both on every push.

Dependencies are pinned in `uv.lock` (hash-pinned) with `requirements.lock`
exported for `uv pip sync`. After any `uv lock` change, regenerate it:

```bash
uv export --format requirements-txt --no-hashes --no-emit-project -o requirements.lock
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and
[SECURITY.md](SECURITY.md) for the data-handling model and how to report a
vulnerability.

## Security and privacy

- Receipts, statements, the SQLite database, generated reports and the
  cardholder roster live under `data/`, which is gitignored. They contain
  bank details and personal identifiers; keep them out of any remote.
- The dependency pins require Python 3.10+ because the fixed versions of
  Starlette (CVE-2026-48710) and Pillow (CVE-2026-42308 to 42311) only ship
  on that line.
- API keys are read from the environment or `.env` only and are never
  logged. A gitleaks pre-commit hook is recommended (see CONTRIBUTING.md).

## License

[MIT](LICENSE) © 2026 Luiz Persechini.
