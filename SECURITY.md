# Security

## Data handling

Receipt Tracker is designed to run entirely on the user's machine.

- OCR (Apple Vision or PaddleOCR) and the default structuring model (Ollama)
  run locally. No image ever leaves the machine.
- Opt-in integrations send only what they need: the Claude API structuring
  backend sends OCR text (not the image); the TypeSafe backends send merchant
  names, amounts, dates and OCR text lines; Gmail ingest reads Uber receipt
  emails with a read-only scope; FX rates fetch public exchange rates. All are
  off by default.
- `data/` holds the SQLite database, uploaded images, generated reports and
  the cardholder roster (names, cost centres, bank and document numbers). It
  is gitignored and must stay out of any remote.
- API keys are read from the environment or `.env` and are never logged or
  echoed by the application or its scripts.

## Dependencies

Dependencies are hash-pinned in `uv.lock`. Python 3.10+ is required because
the patched releases of Starlette (CVE-2026-48710) and Pillow
(CVE-2026-42308 to CVE-2026-42311) are only published for that line.
Uploads are size-capped (`MAX_UPLOAD_MB`), file names are sanitised, and all
file access is confined to the configured data directory.

## Reporting a vulnerability

Please do not open a public issue for security problems. Email the
maintainer (address on the GitHub profile) with a description and, if
possible, a reproduction. You will get an acknowledgement within a few days.
Fixes are released as ordinary commits on `main`; there are no separate
security advisories for this project.
