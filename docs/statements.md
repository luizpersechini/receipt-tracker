# Monthly statements

How statement uploads turn into a list of transactions, and how the
Itaú PDF parser actually works.

## Supported formats

- **CSV** — any layout pandas can read with English or Portuguese
  headers (`date | description | amount | category` and variants like
  `data | descrição | valor | categoria`). Handled by
  `src/services/statement_parser.py`.
- **PDF** — Itaú PT-BR corporate-card statements. Handled by
  `src/services/statement_pdf_parser.py`. Other issuers haven't been
  targeted yet; the parser is single-format on purpose.

Dispatched by file extension in `statement_parser.parse_statement()`.

## Transaction shape

Every parser, regardless of source, emits dicts of this shape so the
matching engine, DB layer, and reconcile board don't have to care
about origin:

```python
{
  "date": "2026-03-21",       # ISO
  "description": "DL *UberRides",
  "amount": 24.20,            # BRL — the bill amount the user owes
  "category": "transportation",
  "raw_data": {
    "section": "domestic" | "international",
    "original_currency": "BRL" | "CAD" | "USD" | ...,
    "original_amount": 21.88, # native amount (int'l only)
    "fx_to_brl": 5.62,        # BRL per USD (int'l only)
    "city": "TORONTO" | None,
    "credit": True,           # only on estorno/refund rows (amount < 0)
  }
}
```

`amount` is **always BRL** because that's what the user actually pays.
For international rows, the parser preserves the native currency
amount in `raw_data` so receipts (which arrive in native currency)
can be matched cleanly.

**Credits / estornos** are rows with a negative `amount` and
`raw_data.credit = true`. They net the report's category column and
TOTAL, print as `(estorno / crédito)` instead of a justification blank,
and are excluded from auto-matching and from the board's open-item
counts — a refund never expects a receipt.

## The Itaú PDF parser

### Why it's not trivial

Itaú statements use a two-column visual layout — transactions stack
top-to-bottom in column 1, then top-to-bottom in column 2 on the
same page. PyMuPDF's default `get_text()` reads in row-by-row
"newspaper" order, which interleaves a transaction's footer (city /
native amount / currency / USD / "Dólar de Conversão") with the
**next** transaction in the other column.

Concretely: a single text block contained these lines, in this
order, before the fix:

```
28/02
SUTTON PLACE HOTEL TOR    ← left col txn header
628,06
TORONTO                   ← THIS is actually the right col's footer
156,69                       (from a different earlier transaction)
CAD
115,03
Dólar de Conversão R$ 5,46
MISSISSAUGA               ← this is SUTTON PLACE's real footer
35,92
CAD
26,39
```

The strict 8-line international pattern would happily consume the
first footer and pair it with the wrong transaction.

### How the fix works

`_column_ordered_lines()` uses `page.get_text("dict")` to get every
line with its bounding box, then:

1. Compute a split point on the X-axis at **58% of page width**.
   This is calibrated to Itaú: left-column right-aligned amounts
   reach x ≈ 335, right-column content starts at x ≈ 363. A clean
   50/50 split puts left-column amounts into the right bucket and
   the reordering breaks pattern matching.
2. Assign each line to column 0 or column 1 based on `x0 < mid_x`.
3. Treat lines wider than 55% of page width as full-width (section
   headers, totals) and keep them in column 0 so section detection
   still fires.
4. Stable-sort by `(column, y0)`. The result is left column
   top-to-bottom followed by right column top-to-bottom — every
   transaction's 3 or 8 lines now cluster together.

The state machine in `_walk_lines()` then walks this reordered
sequence. Pattern matching is unchanged.

### State machine

Tracks one boolean: are we currently inside the Pagamentos efetuados
section. If so, fast-forward until the next `Lançamentos:` header.
Otherwise, at every position, try:

1. **International 8-line pattern** (strict)

   ```
   DD/MM
   MERCHANT NAME
   AMOUNT,XX            ← BRL bill total
   CITY
   AMOUNT,XX            ← native (CAD / USD / …)
   CCY                  ← ISO 4217
   AMOUNT,XX            ← USD equivalent (Itaú always shows USD)
   Dólar de Conversão R$ X,XX
   ```

   The FX-marker line at position +7 is the discriminator. Anything
   that doesn't have it falls through.

2. **Domestic 3-line pattern**
   ```
   DD/MM
   MERCHANT NAME
   AMOUNT,XX            ← BRL
   ```
   Used when the int'l pattern doesn't match. The category line that
   sometimes follows is intentionally ignored — categories come from
   `_guess_category(merchant)` instead, which keeps the buckets
   aligned with the receipt-side category inference.

Negative amounts inside the purchases sections (`- 123,45`) are
**captured as credit rows** — estornos/refunds net the report and are
flagged `raw_data.credit`. The previous bill's payment is also negative
but lives in the "Pagamentos efetuados" section, which the walker skips
wholesale — every occurrence of it, since multi-cardholder statements
repeat the block.

### Year inference

Receipts don't always print the year. The parser scans page 1 for
the `Postagem: DD/MM/YYYY` header and uses that as the year for all
parsed dates. Falls back to the current year (with a logger warning)
if the header is missing.

**Year boundary:** a January statement (Postagem 05/01/2026) lists
December purchases as `15/12` — anything dated after the posting date
rolls back one year (`2025-12-15`), so cross-year transactions match
and bucket correctly.

### Smoke-test data point

Against a real 4-page Itaú statement totalling R$ 22,700.57:

- 98 transactions parsed
  - 48 domestic
  - 50 international
- R$ 22,250.32 parsed total
- R$ 450.25 difference = `Repasse de IOF` (Brazilian tax on
  international transactions, not a merchant transaction —
  correctly excluded)

## Reparsing an already-uploaded statement

`POST /api/statements/{id}/reparse` re-runs the parser over the saved
file (we keep PDFs in `data/statements/`) and replaces the stored
`transactions` JSON. Matches and per-line overrides (notes / labels /
categories / cost centers) are keyed by `transaction_index`, so when the
new parse changes row order or count they are **remapped by content**
(date + amount + description). Rows that no longer exist drop their
matches/overrides — better gone than silently attached to a different
charge — and the affected receipts go back to `pending`.

Useful after:

- a parser fix (column-aware extraction, credit capture, and the
  year-boundary fix each landed mid-life — pre-fix statements need a
  reparse to pick them up)
- regenerating after editing the upload PDF on disk (rare, but
  legitimate)

The Statements tab doesn't have a UI button for this yet — call the
endpoint directly:

```bash
curl -X POST http://localhost:8000/api/statements/1/reparse
# → {"success": true, "transaction_count": 98, "reindexed": false,
#    "matches_kept": 0, "matches_dropped": 0, "receipts_released": 0}
```

## Adding a new statement format

If your bank doesn't use Itaú's layout:

1. Add `src/services/statement_yourbank_parser.py` exposing
   `parse_statement_yourbank(filepath: str) -> list[dict]` that
   returns the dict shape above.
2. Add a branch to `statement_parser.parse_statement()` that
   dispatches to it. The cheapest discriminator is the filename
   pattern; richer would be inspecting the first page.
3. Add a `tests/test_statement_yourbank_parser.py` that builds a
   synthetic PDF with `fitz` and asserts the expected rows. The
   existing test file is the template.

The matching engine, reconcile board, statements list, statement
detail modal, and Excel exporter all consume the dict shape — no
changes needed downstream.
