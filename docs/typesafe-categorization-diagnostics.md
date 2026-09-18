# TypeSafe categorization — diagnostics and reusable recipe

What TypeSafe (Jev / System One) improved in receipt-tracker, measured on the
real data, plus the pattern to lift into other projects. Written 2026-09-18
after the first live comparison run.

## What it is, in one paragraph

Jev is not a chat model. You send a `state` JSON and typed `questions`; each
`choice` question returns one option from a list you define, with calibrated
probabilities over all options and a `confidence` score. Every question in a
request runs in parallel server-side, so a whole batch is one HTTP call.
That makes it a good fit for closed-set classification where you already
have a cheap fallback and want a calibrated "how sure" signal you can
threshold on.

## The problem it solved here

Expense categorization had two sources, both weak in a specific way:

- Statement lines used a keyword heuristic only. Bank descriptors are
  truncated uppercase strings ("CHURRASCARIA-CTTASUL", "IFD*VARANDA1389DELIV"),
  so keyword lists miss constantly: 35% of all lines landed in "other".
- Receipts used the local structuring LLM's category, which is usually right
  but sometimes wrong in ways nobody reviews ("Jantar Rio" → other).

"Other" is expensive downstream: every such line is a manual fix on the
Reconcile tab and a wrong bucket on the reimbursement report.

## Measured results

502 items (247 receipts + 255 statement lines), read-only run, one batched
call per 50 items.

| What's measured                                             | keyword   | TypeSafe                   | Delta              |
| ----------------------------------------------------------- | --------- | -------------------------- | ------------------ |
| Agreement with current receipt category (n=247, LLM output) | 60.7%     | 72.9%                      | +12.2 pts          |
| Agreement with hard human truth (n=4 overrides)             | 1/4       | 1/4                        | none (n too small) |
| Lines left as "other" (n=502)                               | 177 (35%) | resolves most at 0.99–1.00 | the real win       |
| Rows above the 0.7 confidence floor                         | —         | 305/502 (61%)              | —                  |
| Latency                                                     | —         | ~1.3 s per 50-item call    | —                  |
| Tokens                                                      | —         | ~444 in / ~74 out per item | —                  |

What the disagreement list showed: TypeSafe rescues obvious keyword misses at
0.99–1.00 confidence, and on inspection they are all correct — hotels the
keyword map called "meals", taxis and restaurants it called "other", SaaS
subscriptions (Anthropic, Claude.ai, HuggingFace) → office_supplies. In at
least one case it was right where the "truth" was wrong ("Jantar Rio",
jantar = dinner), so 72.9% is a floor on its real accuracy, not a ceiling.

Its weak spot is ambiguous payment-processor descriptors: `IFD*iFood` →
lodging at 0.37, `MP*CLIENTES` → 0.27. Crucially it is uncertain there, not
confidently wrong, so the confidence floor catches both and keeps the
keyword guess. The system fails safe: it can leave a line as-is or fix it,
never make it worse.

Honest caveat: only 4 human-verified labels exist in this DB, so the true
accuracy is unknown. The confident-disagreement list is the evidence, not a
number. To get a defensible figure, review ~40 lines (the category dropdown
saves overrides as ground truth) and re-run `scripts/compare_categories.py`.

## Design decisions worth copying

1. Opt-in behind a flag, default off. `category_backend="keyword"` stays the
   default; the cloud path is a deliberate flip. Same shape as the existing
   `structure_backend="anthropic"` opt-in. Local-first projects should never
   gain a network dependency silently.
2. One batched call, not one per item. Build `state = {"items": [...]}` and
   one `choice` question per item whose instructions reference that item by
   path (`items[3].merchant_or_vendor`). All questions share one state and
   run in parallel. Chunk at 50. A 75-line statement is one ~1.3 s call.
3. Criteria are contrastive one-liners, and the option set is exactly the
   app's own category enum. Each criterion says what belongs AND what it is
   not ("transportation: … NOT fuel purchases, NOT airfare"), so adjacent
   categories don't bleed. Assert at import that criteria keys ==
   VALID_CATEGORIES so the two can never drift.
4. Confidence floor + fallback, applied by the caller. The client module is
   pure (returns category/confidence/probabilities); the policy — use it at
   confidence ≥ 0.7, else keep the heuristic — lives in the callers and logs
   every fallback. This is what makes low-confidence answers harmless.
5. Send only what helps. Merchant, amount, currency, date, and receipt line
   items when present. No ids, no internal fields. Question ids are for code
   only; the model never sees them, so the meaning must be in the question.
6. Every failure keeps the local result. Missing key, 401/422/429/529,
   timeout — all raise one error type that callers catch and log; parsing
   and OCR never depend on the network.
7. A single HTTP seam (`_post`) so tests stub it. Nine tests cover payload
   shape, batching, mapping, the floor, error paths, and — the one that
   matters most — that the default backend never issues a call.
8. A read-only comparison script before flipping the flag. Open the DB with
   `mode=ro`, compute heuristic / TypeSafe / truth per row, print agreement
   rates, the top confident disagreements, and per-call latency + tokens.
   Decide with data, not vibes.

## Cost and latency

~444 input tokens per item, ~1.3 s per 50-item request. For this project's
monthly volume (one ~75-line fatura plus a few dozen receipts) that is a few
calls a month. Measure your own volume with the comparison script before
enabling; the script prints exact tokens per call.

## Where it would NOT be worth it

- Open-ended output (writing text, extracting free-form fields). Jev picks
  from your list; it does not generate.
- Classification where you have no fallback and the floor would leave gaps.
- High-stakes decisions where "confidence summarizes distribution
  concentration" is mistaken for "this is true". Calibration ≠ correctness;
  validate on your own labels.

## Recipe for another project

Reach for this when you have a closed set of labels, a cheap local heuristic
as fallback, and enough volume that "other"/unknown is costing manual work.

1. Add `<thing>_backend: str = "local"` and `typesafe_api_key: Optional[str]`
   to config; key from env only; document the flag in `.env.example`.
2. Write `<thing>_typesafe.py`: build state + one `choice` per item with
   path-referenced instructions and contrastive criteria; POST to
   `https://api.typesafe.ai/v1/systemone` with `model: "jev-latest"` and
   `Authorization: Bearer <key>`; map `answers[qid].choice/confidence/
probabilities` back by position; chunk at 50; single `_post` seam.
3. Add `pick_or_fallback(result, fallback)` with a configurable floor; log
   fallbacks.
4. Wire it at the points where the local heuristic runs, keeping the
   heuristic's answer as the fallback argument. Batch where a whole
   document is parsed at once; single-item where it's a per-upload path.
5. Mocked tests, including "default backend makes no call".
6. Read-only comparison script over real data; run it; read the confident
   disagreements by eye; then decide whether to flip the flag — and tune the
   floor on what you saw.

Files to copy from this repo as templates:
`src/services/typesafe_categorizer.py`, `tests/test_typesafe_categorizer.py`,
`scripts/compare_categories.py`, and the `category_backend` block in
`src/config.py`. The skill for the API itself: `claude plugin install
typesafe@typesafe-ai`; live docs at https://docs.typesafe.ai/primitives.
