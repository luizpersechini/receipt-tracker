# API reference

All endpoints are JSON in / JSON out. Root prefix is `/api`. The
SPA in `static/` is the only intended client but the API is plain
HTTP so curl works.

## Receipts

### `POST /api/upload/receipt/preview`

First step of the bbox-editor flow. Saves the file, runs the
segmenter, returns suggested bounding boxes. PDFs bypass the editor
entirely — they're rasterised + OCR'd directly and the response sets
`auto_processed: true`.

**Body:** multipart `file`.

**Response (image):**

```json
{
  "success": true,
  "filename": "receipt_20260526_120402_foo.jpg",
  "image_url": "/api/preview/receipt_20260526_120402_foo.jpg",
  "image_size": { "w": 1080, "h": 1440 },
  "bboxes": [{ "x": 10, "y": 10, "w": 200, "h": 400 }],
  "auto_segmented": false
}
```

**Response (PDF):**

```json
{
  "success": true,
  "filename": "receipt_20260526_120402_foo.pdf",
  "auto_processed": true,
  "count": 3,
  "pages": 3,
  "receipts": [{"id": 1234, "metadata": {...}}]
}
```

### `POST /api/upload/receipt/commit`

Run OCR on user-confirmed bboxes. Body:

```json
{
  "filename": "receipt_20260526_...jpg",
  "bboxes": [{ "x": 10, "y": 10, "w": 200, "h": 400 }],
  "description": "team lunch"
}
```

### `POST /api/upload/receipt`

One-shot upload — saves file, segments, OCRs, persists. No bbox
review. Used by the bulk path.

### `GET /api/receipts`

List all receipts. Returns `{receipts: [...]}` with full metadata.

### `DELETE /api/receipts/{id}`

Remove a receipt and any matches that reference it.

### `PATCH /api/receipts/{id}`

Edit metadata. Body keys go into the JSON blob: `vendor_name`,
`total_amount`, `currency`, `date`, `country`, `category`,
`description`, `expense_type`, etc.

### `PATCH /api/receipts/{id}/expense-type`

Toggle between `corp_card` and `reimbursement`. Body: `{expense_type}`.

### `POST /api/receipts/{id}/rotate`

Rotate the saved image 90° (clockwise). Useful when a phone photo
came in sideways.

### `POST /api/receipts/{id}/reocr`

Re-run OCR on the saved file and overwrite metadata. Doesn't touch
the `description` field.

### `POST /api/receipts/bulk-delete`

Body: `{ids: [int]}`. Removes many at once.

### `POST /api/receipts/dedupe`

Walk the receipts table and merge near-duplicates (same vendor, same
date, amount diff < 0.01).

### `POST /api/receipts/dedupe-uber`

Same idea but Uber-specific — uses ride id / driver / pickup so two
imports from CSV and Gmail collapse into one row.

### `POST /api/receipts/backfill-country`

Walk all receipts and infer `country` from `state` / `city` /
`vendor_address` when missing.

### `POST /api/receipts/strip-times`

Walk all receipts and chop clock times off `date` fields
(`"10-Mar-2026 1:44 PM"` → `"10-Mar-2026"`). One-shot recovery.

### `POST /api/receipts/normalize-dates`

Walk all receipts and normalise `date` to ISO `YYYY-MM-DD`. Locale-
aware via the receipt's stored `language` and `country`. Keeps the
original printed string when parsing fails.

## Statements

### `POST /api/upload/statement`

Body: multipart `file`. Dispatcher routes by extension.

- `.csv` → pandas reader, column heuristics for English + Portuguese
- `.pdf` → Itaú PT-BR parser

Response: `{statement_id, filename, transaction_count}`.

### `GET /api/statements`

List of `{id, filename, filepath, transactions, uploaded_at}`. Note:
`transactions` is a parsed array, not a JSON string.

### `DELETE /api/statements/{id}`

Removes the statement and any matches pointing to it.

### `POST /api/statements/{id}/reparse`

Re-run the parser over the saved file and replace
`statements.transactions`. Manual matches survive (they're keyed on
`transaction_index` which stays stable).

### `GET /api/statements/{id}/transactions`

Reconcile-board-ready view: each transaction annotated with its current
matches. A transaction can hold **multiple** matches (cumulative
pairing), so read `matches` (array). `match` is the first entry,
retained for older callers.

```json
{
  "statement": { "id": 1, "filename": "...", "transaction_count": 98 },
  "transactions": [
    {
      "index": 0,
      "date": "2026-03-21",
      "description": "UBER* TRIP",
      "amount": 17.81,
      "currency": "BRL",
      "category": "transportation",
      "city": null,
      "original_amount": null,
      "original_currency": null,
      "fx_to_brl": null,
      "matches": [],
      "match": null
    },
    {
      "index": 17,
      "date": "2026-03-06",
      "description": "TWIST T1 -CT",
      "amount": 148.31,
      "currency": "BRL",
      "category": "meals",
      "city": "MISSISSAUGA",
      "original_amount": 35.92,
      "original_currency": "CAD",
      "fx_to_brl": 5.46,
      "matches": [
        {
          "id": 287,
          "receipt_id": 1003,
          "receipt_vendor": "Twist Toronto",
          "receipt_amount": 35.92,
          "receipt_currency": "CAD",
          "score": 0.97,
          "kind": "auto"
        }
      ],
      "match": { "id": 287, "receipt_id": 1003, "...": "(first entry)" }
    }
  ]
}
```

### `GET /api/statements/{id}/guess-user`

Best-guess cardholder for a statement, by name. Returns
`{user_id, nome}` or `{user_id: null}` when nothing matches confidently.
See [reports.md](reports.md#name-auto-detection).

## Matching

### `POST /api/match`

Run the auto-matcher. **Preserves manual matches** — it deletes only
auto-kind rows, excludes manually-paired receipts/transaction slots from
the pool, scores the rest, and persists new matches with
`match_kind="auto"` and a `transaction_index`. Reimbursement receipts
are excluded.

Response: `{success, matches, unmatched_receipts,
unmatched_transactions, total_matched, ...}`.

### `GET /api/matches`

Flat list of every match row with the joined receipt + statement
metadata.

### `POST /api/matches`

Create a manual match (drag-drop on the reconcile board).

```json
{ "receipt_id": 1003, "statement_id": 1, "transaction_index": 17 }
```

Returns `{id, kind: "manual", score: null}`. **N:1 allowed** — a
transaction can take multiple receipts (cumulative pairing). A single
receipt is still 1:1: pairing it twice returns 409 (would double-count
on the report).

### `DELETE /api/matches/{id}`

Remove a match (auto or manual). Returns 204 with an empty body. The
linked receipt goes back to `pending` if no other matches reference it.

## Folder scan

### `GET /api/watch-folder/status`

`{enabled, folder, pending, last_processed}`. `pending` is the count
of supported files sitting in the inbox (not in `processed/` or
`failed/`).

### `POST /api/watch-folder/scan`

Process every supported file in the inbox. Each file moves to
`processed/` (on success) or `failed/` (on error). Returns a
summary with per-file results.

## Reports

### `GET /api/report`

HTML expense-report preview for in-browser viewing.

### `POST /api/reports/expense`

Generate an Excel report. Body:

```json
{
  "report_type": "all" | "corp_card" | "reimbursement",
  "period_start": "2026-04-01",
  "period_end": "2026-04-30",
  "target_currency": "BRL"
}
```

Returns the file path; download via `GET /api/reports/expense?id=...`.

### `GET /api/reports/expense`

Stream a previously-generated report by id.

## Combined PDF report + cardholders

See [reports.md](reports.md) for the full picture. Endpoints:

### `GET /api/statements/{id}/report?user_id=`

Build + stream the combined "Prestação de Contas" PDF (categorised
front page + receipt-image grid) for one statement. `user_id` picks a
roster cardholder's profile; omitted → guess by name → global default.
`Content-Type: application/pdf`, attachment download.

### `GET /api/reports/reimbursement`

Build + stream the **reimbursement** PDF — the same form built from
`reimbursement`-type receipts (no statement, no matching). Query params,
all optional: `user_id` (cardholder profile), `date_from`, `date_to`
(YYYY-MM-DD), `currency` (one report per currency — the form has a
single Total). 404 when no receipts match. `application/pdf` attachment.

### `GET` / `PUT` `/api/report-profile`

Get / save the **global default** front-page fields (company code, nome,
C/C, bank details, …). Used as the template for new cardholders and the
fallback when no cardholder matches. Stored in
`data/report_profile.json`.

### `GET /api/report-users`

List cardholders: `{users: [{id, nome, cost_center, ...}]}`.

### `POST /api/report-users`

Create a cardholder. Body is any subset of the profile fields (`nome`
recommended). Unknown keys are dropped; defaults fill the rest. Returns
the created user with its assigned `id`.

### `PUT /api/report-users/{id}`

Update a cardholder. Merges the patch over the existing record. 404 if
the id doesn't exist.

### `DELETE /api/report-users/{id}`

Remove a cardholder. Returns 204. 404 if not found.

### `GET /api/report-history`

List saved reports, newest first:
`{reports: [{id, created_at, kind, title, cardholder, currency, period,
size, file, download_name}]}`. `kind` is `statement` or
`reimbursement`. Every statement / reimbursement PDF the app builds is
recorded here automatically.

### `GET /api/report-history/{id}/download`

Re-download a saved report PDF. 404 if the record or its file is gone.

### `DELETE /api/report-history/{id}`

Delete a saved report (index row + file). Returns 204. 404 if not found.

## FX rates

### `GET /api/fx/rates`

Cached rates keyed by date. Used to convert receipts into the
target reporting currency.

## Gmail (optional)

### `GET /api/gmail/status`

Connection state — `{configured, authorized, last_sync_at}`.

### `POST /api/gmail/credentials`

Upload a `credentials.json` for Google OAuth. Body: multipart `file`.

### `POST /api/gmail/auth/start`

Kick off the OAuth flow. Returns the auth URL the browser should
visit; tokens land in `data/gmail/token.json` after consent.

### `POST /api/gmail/sync`

Pull Uber receipt emails. Query params: `business_only` (bool),
`attach_only` (bool — if true, only attach PDFs to existing rows;
don't create new rows from emails).

### `POST /api/gmail/render-pdfs`

Re-render PDFs for rows with `filepath="gmail://..."`. Useful after
the Uber receipt parser changes.

### `POST /api/gmail/reset`

Remove the `rt-processed` Gmail label from every thread so the next
sync re-imports them. Power-user button — no UI control.

## Uber CSV

### `POST /api/upload/uber-csv`

Bulk import from the Uber trip-history CSV. Query params filter by
profile (`business_only`) and date range (`date_from`, `date_to`).

## Preview / static

### `GET /api/preview/{filename}`

Transparently converts HEIC/HEIF to JPEG on the fly so browsers can
render the image. Falls back to the raw file for already-viewable
formats. Returns PDFs with `Content-Type: application/pdf`.

## Stats

### `GET /api/stats`

Topbar stats. Returns `{total_receipts, matched_receipts,
unmatched_receipts, pending_receipts, corp_card_receipts,
reimbursement_receipts, total_statements, total_matches,
total_receipt_amount}`.
