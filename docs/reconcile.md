# Reconciliation

How receipts get paired with statement transactions, and what to do
when the auto-matcher gets it wrong.

## The board

The Reconcile tab is a two-pane workspace. Pick a statement from the
dropdown at the top; the left pane fills with that statement's parsed
transactions, the right pane lists corp-card receipts from your
library.

```
┌─ Statement dropdown ─┐  [⚡ Auto-match]  75 / 98 paired ·  ☐ Show matched txns  ☐ Show matched receipts
├──────────────────────┴────────────┬────────────────────────────────────┐
│  STATEMENT TRANSACTIONS  23 of 98 │  RECEIPTS (UNMATCHED)  47           │
│  [🔍] [date] [ccy] [amount]       │  [🔍] [date] [ccy] [amount]         │
│                                   │                                    │
│  Mar 06  TWIST T1 -CT    CAD 35.92│  Mar 06  Twist Toronto              │
│                          R$148.31 │     [🖼]  CAD 35.92    CAD          │
│  ↳ drop a receipt here            │                                    │
│                                   │  Mar 10  Trees Coffee               │
│  Mar 09  PARKCHAMP PRK   CAD 29.12│     [🖼]  CAD 13.97    CAD          │
│                          R$117.98 │                                    │
│  ↳ drop a receipt here            │                                    │
└───────────────────────────────────┴────────────────────────────────────┘
```

Both panes sort by date ascending so a receipt's natural position
lines up with the corresponding transaction (makes drag targeting
easier on long statements).

## Filters & toggles

Each pane has its own filter strip. Both strips share the same
controls — they apply to their own column only.

- **Search box** — substring match across description / city / vendor
- **Date range** — From / To, applied to the transaction or receipt date
- **Currency dropdown** — options are derived from data actually
  present (no point listing JPY if no row uses it). Receipt-pane uses
  the receipt's stored currency; txn-pane uses
  `original_currency` for international rows and `BRL` for domestic.
- **Amount range** — Min / Max in BRL for txns, native currency for receipts

Filter values persist to `localStorage` under `rt.recon.txnFilter` and
`rt.recon.receiptFilter`.

Two **header-level toggles** control which rows are even eligible to
appear:

- ☐ **Show matched transactions** — default off. Hides the
  transactions you've already paired so the left pane focuses on
  what still needs work (23 of 98 in the screenshot).
- ☐ **Show matched receipts** — default off. Hides paired receipts
  on the right.

The "paired" counter in the header always reflects the whole
statement, not the filtered subset.

## What's hidden by default (and why)

- **Reimbursement receipts** never appear on the right pane. The
  Reconcile tab is corp-card only — a reimbursement receipt is a
  personal-spend item that won't show up on a corp-card statement,
  so listing them would just add noise. The Receipts tab still
  shows everything; flip a row's expense type there if you want it
  considered.
- **Matched rows on both sides** are hidden by default (see toggles
  above). Tick the checkboxes to bring them back.

## Pairing modes

There are three ways a match gets created:

### 1. Auto-match

Click the **Auto-match** button. The matching engine
(`src/services/matching_engine.py`) walks every unmatched corp-card
receipt and scores it against every unmatched transaction using:

```
score = amount_score × 0.50  +  date_score × 0.30  +  vendor_score × 0.20
```

Best score above 0.5 wins. Matches are persisted with
`match_kind = "auto"`, the raw score in `match_score`, and a
**`transaction_index`** that pins the match to a specific row in the
statement — without it the reconcile board can't display the match.

**Amount scoring is currency-aware.** For an Itaú statement
transaction with `raw_data.original_currency = "CAD"` and
`original_amount = 21.88`, a CAD 21.88 receipt is compared CAD-to-CAD.
A BRL receipt against the same row would be compared against the BRL
bill total instead. If receipt currency and transaction currency
disagree entirely (e.g. CAD receipt ↔ pure BRL domestic txn), the
score drops by 70% — prevents matching by coincidence.

### 2. Drag-and-drop

Drag any receipt card from the right pane onto a transaction card on
the left. The whole transaction card is the drop target — the dashed
inner box from earlier designs is gone, replaced by a hover overlay
that says "Release to match →".

While a drag is in flight, candidate transactions get a faint dashed
border. Transactions that are a **good match** for the dragged
receipt (amount diff < 0.5, date diff ≤ 3 days, currency-compatible)
get a green "good match" ribbon in the top-right corner.

On drop the UI updates optimistically — the receipt fades out of the
right pane, the transaction card flips to its `.matched` state — then
fires `POST /api/matches`. If the server returns an error, the
optimistic update rolls back and a red flash banner appears under the
header.

Manual matches have `match_kind = "manual"` and `match_score = null`.

### Cumulative matching (N receipts → 1 charge)

A single statement charge sometimes covers several receipts — a hotel
folio that's billed once but has separate room / restaurant / parking
receipts. You can drop **multiple** receipts onto the same transaction:

- A matched transaction stays a drop target (tick "Show matched
  transactions" to keep it on screen).
- It renders one green tag per receipt, each with its own × to unlink
  just that one.
- A **progress chip** appears once ≥2 receipts are attached:
  `R$ 100 of R$ 120` — the running total of the attached receipts vs
  the bill amount. It turns green when the sum lands within R$ 0.50 of
  the charge.

A receipt is still **1:1** — you can't pair the same receipt onto two
transactions (it would double-count on the report). Trying returns 409.

### 3. Pre-existing

Matches created before the drag-drop board landed still show up — a
backfill in `git history` repaired their `transaction_index` so they
render alongside new matches. They display the original `match_score`
from when auto-match created them.

## Visual states

**Transaction card states:**

- _Unmatched_ (default) — plain card with date, merchant, amount,
  category badge, and city.
- _Matched_ — accent left-border, gradient background, paired-receipt
  badge in green, score chip (e.g. `97%`), and an unlink (×) button.
- _Drop-hover_ — full accent-colour outline + scale 1.02 + "Release
  to match →" overlay.

**Receipt card states:**

- _Pending_ — clickable / draggable, grip-dot affordance on hover.
- _Matched_ — opacity 0.55, dashed border, grayscale thumbnail. Only
  visible when "Show matched receipts" is ticked.

**Receipt thumbnails:** the 40×40 box on each receipt card now shows
the actual image. Click it to open the lightbox. By file type:

- `jpg / png / webp / gif` → image from `/receipts/<filename>`
- `heic / heif` → routed through `/api/preview/<filename>` so the
  server can JPEG-convert on the fly (browsers can't render HEIC)
- `pdf` → small "PDF" chip that opens the PDF in a new tab
- Uber-CSV synth rows (no physical file) → car-glyph placeholder
- Anything else → existing striped CSS placeholder

**International transactions** show the native amount as the
headline (e.g. `CAD 35.92`) with the BRL bill total underneath
(`R$ 148.31`). Receipts are always in their native currency, so this
layout makes amount-matching obvious at a glance.

## Unlinking and re-pairing

You can unlink from **either side**:

- **Transaction side** — each green match tag on a matched transaction
  has a × button.
- **Receipt side** — a matched receipt card (visible with "Show matched
  receipts" on) shows a "Matched to <txn>" line with its own × button.
  Handy when you spot a wrong pairing while looking at the receipts
  pane and don't want to hunt for the transaction.

Both call `DELETE /api/matches/{id}` (which returns **204 No Content** —
the `api()` helper tolerates the empty body, so unlink no longer shows
a spurious "failed" flash). The receipt flips back to `pending` if it
has no other matches anywhere.

## Justifying open items (no receipt)

When a transaction has no receipt and you can't match one — you lost it,
or it's an unidentified charge — type a justification right on the
unmatched card: the `+ justification (no receipt)` field. It prints on
the report's front page in place of the blank `justificativa: ____`
line. Saved per `(statement, transaction)` in
`data/statement_notes.json`; survives re-parses. See
[reports.md](reports.md#justifying-open-items).

## Generating the report

Once a statement is reconciled, **Generate report** (top of the
reconcile header) builds the combined "Prestação de Contas" PDF — a
categorised summary front page plus receipt-image grid pages. The
cardholder is auto-detected from the statement's name. Full detail in
[reports.md](reports.md).

## When the matcher gets it wrong

Common false-positive patterns:

| Pattern                                                          | Cause                                                                                                                                     | Fix                                                                                                                              |
| ---------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| Score = 0.5 exactly, amounts way off                             | Vendor name overlapped (e.g. "Uber" matched any UBER row) but amounts didn't fit any tolerance bracket — matcher fell back to vendor-only | Unlink, look for the actual receipt later                                                                                        |
| Cross-currency pair                                              | Pre-fix matches from before currency awareness was added                                                                                  | Run Auto-match again with `POST /api/match` (clears old matches first)                                                           |
| Two receipts on same date+amount                                 | Genuine ambiguity (e.g. two coffee runs at the same shop)                                                                                 | Unlink the wrong one, drag the right one onto the txn                                                                            |
| Reconcile board shows 0 paired but receipts table says N matched | Match rows missing `transaction_index` (legacy data from before that column existed)                                                      | Fixed in c152b85; legacy rows backfilled by a one-shot SQL script. If it recurs, the engine now writes the index on every match. |

The audit-trail approach: run Auto-match, scan the board for any
matches with `score < 0.7` (visible in the score chip), and verify
manually. Real matches are usually 0.8+.

## Edge cases worth knowing

- **PDF statement re-parses preserve manual matches.** The reparse
  endpoint replaces `statements.transactions` but leaves the matches
  table alone. Since `transaction_index` is stable across re-parses
  (provided the parser produces rows in the same order), manual
  matches survive.
- **Reimbursement receipts skip auto-match AND don't show on the
  board.** Only receipts with `expense_type = "corp_card"` are
  considered. Flip a row's expense type on the Receipts tab if you
  want it included.
- **Statements you don't want to match against can be deleted** —
  there's no "soft-disable" for a statement. Deletion is destructive
  but the file stays on disk so you can re-upload.
- **Auto-match preserves manual pairings.** Running it clears only the
  `auto`-kind matches and re-scores; anything you dragged by hand stays
  put, and its receipt + transaction slot are excluded from the new
  pass so they can't be clobbered. So you can hand-fix a few, then run
  Auto-match again to mop up the rest without losing your work.
- **The currency dropdown only lists currencies present in data.**
  After uploading a new statement or adding receipts in a new
  currency, switch tabs or reload the Reconcile page to refresh the
  options.
