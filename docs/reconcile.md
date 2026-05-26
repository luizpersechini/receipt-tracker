# Reconciliation

How receipts get paired with statement transactions, and what to do
when the auto-matcher gets it wrong.

## The board

The Reconcile tab is a two-pane workspace. Pick a statement from the
dropdown at the top; the left pane fills with that statement's parsed
transactions, the right pane lists every unmatched receipt in your
library.

```
┌─ Statement dropdown ─┐  [⚡ Auto-match]  74 / 98 paired  ☐ Show matched
├──────────────────────┴────────────┬────────────────────────────────┐
│  STATEMENT TRANSACTIONS (98)      │  RECEIPTS (UNMATCHED · 59)     │
│  [🔍 search] [date] [amount]      │  [🔍 search] [date] [amount]   │
│                                   │                                │
│  Mar 06  TWIST T1 -CT    CAD 35.92│  Mar 06  Twist Toronto         │
│                          R$148.31 │           CAD 35.92    [thumb] │
│  ✓ matched ↔ Twist 35.92          │                                │
│                                   │  Mar 10  Trees Coffee          │
│  Mar 09  PARKCHAMP PRK   CAD 29.12│           CAD 13.97    [thumb] │
│                          R$117.98 │                                │
│  ↳ drop a receipt here            │                                │
└───────────────────────────────────┴────────────────────────────────┘
```

Both panes sort by date ascending so a receipt's natural position
lines up with the corresponding transaction (makes drag targeting
easier on long statements).

## Pairing modes

There are three ways a match gets created:

### 1. Auto-match

Click the **Auto-match** button. The matching engine
(`src/services/matching_engine.py`) walks every unmatched receipt and
scores it against every unmatched transaction using:

```
score = amount_score × 0.50  +  date_score × 0.30  +  vendor_score × 0.20
```

Best score above 0.5 wins. Matches are persisted with
`match_kind = "auto"` and the raw score in `match_score`.

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
fires `POST /api/matches`. If the server returns an error (most
commonly 409 because the transaction is already paired), the optimistic
update rolls back and a red flash banner appears under the header.

Manual matches have `match_kind = "manual"` and `match_score = null`.

### 3. Pre-existing

Matches created before the drag-drop board went in still show up — a
backfill in `git history` repaired their `transaction_index` so they
render alongside new matches. They display the original
`match_score` from when auto-match created them.

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

**International transactions** show the native amount as the
headline (e.g. `CAD 35.92`) with the BRL bill total underneath
(`R$ 148.31`). Receipts are always in their native currency, so this
layout makes amount-matching obvious at a glance.

## Unlinking and re-pairing

Each matched transaction has a small × button on the right. Click it
to delete the match — the receipt flips back to `pending`, the
transaction becomes an unmatched drop target again. The deletion
hits `DELETE /api/matches/{id}`.

If you unlink and the receipt has no other matches anywhere, its
`match_status` goes back to `pending`. If somehow the same receipt
was paired against multiple statements, the receipt stays `matched`
until the last reference is gone.

## When the matcher gets it wrong

Common false-positive patterns:

| Pattern                              | Cause                                                                                                                                     | Fix                                                                    |
| ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| Score = 0.5 exactly, amounts way off | Vendor name overlapped (e.g. "Uber" matched any UBER row) but amounts didn't fit any tolerance bracket — matcher fell back to vendor-only | Unlink, look for the actual receipt later                              |
| Cross-currency pair                  | Pre-fix matches from before currency awareness was added                                                                                  | Run Auto-match again with `POST /api/match` (clears old matches first) |
| Two receipts on same date+amount     | Genuine ambiguity (e.g. two coffee runs at the same shop)                                                                                 | Unlink the wrong one, drag the right one onto the txn                  |

The audit-trail approach: run Auto-match, scan the board for any
matches with `score < 0.7` (visible in the score chip), and verify
manually. Real matches are usually 0.8+.

## Edge cases worth knowing

- **PDF statement re-parses preserve manual matches.** The reparse
  endpoint replaces `statements.transactions` but leaves the matches
  table alone. Since `transaction_index` is stable across re-parses
  (provided the parser produces rows in the same order), manual
  matches survive.
- **Reimbursement receipts skip auto-match.** Only receipts with
  `expense_type = "corp_card"` are considered. Flip a row's expense
  type if you want it included.
- **Statements you don't want to match against can be deleted** —
  there's no "soft-disable" for a statement. Deletion is destructive
  but the file stays on disk so you can re-upload.
- **The Auto-match button always clears existing auto-matches first**
  on its targeted statements. Manual matches survive.
