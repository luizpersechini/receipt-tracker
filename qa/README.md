# QA harness

`run_qa.py` boots the app **in-process** (FastAPI `TestClient`) against a
**throwaway** db + data dir under a tempfile — it never touches the live
`data/`. It then drives the real flows end to end and reports every error with
timing, so you can catch regressions before they reach live data.

`bakeoff.py` is a sibling tool: a head-to-head **OCR config comparison** —
runs a fixed sample of your own receipts through multiple configs (apple_vision

- different text models, vs glm-ocr) and prints a **per-field** scorecard
  (total / currency / date / vendor / category) + a verdict. Read-only on
  `data/`. Used to retire glm-ocr (0% every field) and confirm apple_vision +
  `llama3.1:8b` as the default. Re-run when models change:

```bash
.venv/bin/python qa/bakeoff.py --n 8           # 8 receipts, all fields
.venv/bin/python qa/bakeoff.py --apple-models llama3.2:3b,llama3.1:8b --glm glm-ocr:latest
```

## Run

Use the **project venv interpreter** (Python 3.13) — the app needs 3.10+, and
your system `python`/`python3` may be older (a 3.9 fails with a `dict | None`
TypeError):

```bash
.venv/bin/python qa/run_qa.py                # real OCR (Apple Vision + your configured text model) — needs Ollama
.venv/bin/python qa/run_qa.py --ocr mock     # stub OCR — fast, deterministic (seconds)
.venv/bin/python qa/run_qa.py --loops 3      # run the battery 3×
.venv/bin/python qa/run_qa.py --until-clean  # loop until a fully green run (cap 5)
.venv/bin/python qa/run_qa.py --keep         # keep the temp sandbox for inspection
.venv/bin/python qa/run_qa.py --report qa/last.json   # also dump JSON results
.venv/bin/python qa/run_qa.py --corpus 8 --corpus-seed 7   # 8 receipts/corpus, FIXED sample (reproducible A/B)
.venv/bin/python qa/run_qa.py --text-model llama3.1:8b     # force a specific structuring model
```

The structuring model defaults to your **configured effective model** (the
persisted picker choice), so the corpus measures what your app actually runs.
`--corpus-seed N` pins the random sample so you can compare a before/after
algo change on the _same_ receipts; omit it for a fresh sample each run.

Exit code is `0` when every check passes (warnings allowed), `1` otherwise —
so it drops into CI or a pre-push hook. `--ocr mock` is the right mode for a
fast gate; `--ocr real` additionally exercises the OCR/structuring pipeline.

## What it checks

| Check                      | Covers                                                                                 |
| -------------------------- | -------------------------------------------------------------------------------------- |
| health: `GET /`            | index served                                                                           |
| health: core GET endpoints | receipts, statements, stats, ocr/config, review-queue, watch-folder, matches all 200   |
| ocr config                 | QA sandbox is on `apple_vision` (not the looping glm-ocr default)                      |
| receipt single-flow        | `preview?single` → one box → `commit` (real OCR extracts vendor/total) → edit → delete |
| receipt multi-segment      | 2-receipt image → segmenter returns ≥2 boxes → commit                                  |
| statement                  | upload CSV → list → **complete (archive)** → reopen → delete                           |
| matching                   | `POST /match` runs without error; `/stats` shape                                       |
| review queue               | PDF preview queues pages → commit drains the item                                      |
| report                     | expense report generates (warns, not fails, if FX/network is offline)                  |
| corpus EN (SROIE)          | random real English receipts (SROIE), OCR'd + scored vs ground-truth totals            |
| corpus PT (your own)       | random real BRL image receipts from your data/, re-OCR'd + scored vs the stored total  |

### Real-corpus accuracy steps (Portuguese + English)

Focused on the receipts this app actually sees. Each run scores total-amount
accuracy and appends to `qa/corpus_scoreboard.jsonl` (gitignored); the last 3
runs per corpus print as a trend — the "test and improve" loop.

- **EN — SROIE** ([mp-02/sroie](https://huggingface.co/datasets/mp-02/sroie)):
  a fresh random sample of real English receipts; ground-truth total comes
  from the `S-TOTAL` NER tag (abs() — some tags carry a stray sign). SROIE
  images are small (512px tall), so they're upscaled before OCR. Apple
  Vision often splits the label column from the value column on these,
  which the evidence-anchored total repairs in `llm_processor` largely
  recover (50% → 88% on a pinned seed); the residue is OCR digit noise.
- **`diag_sroie.py`** — the companion diagnostic: same pipeline over a
  seed-pinned sample, but dumps gt / got / vendor / full OCR text per
  receipt to a JSON you can actually analyse. Use it before touching the
  parser:
  `.venv/bin/python qa/diag_sroie.py --n 12 --seed 7 --out qa/diag.json`
  (outputs are gitignored).
- **PT — your own receipts:** a random sample of your real **BRL image**
  receipts (Uber rides and non-image files excluded), read-only from the live
  DB, re-OCR'd and scored against the **stored** total. For receipts you've
  corrected this is true ground truth; for the rest it's a regression signal
  (a drop means a change broke something). HEIC (iPhone) receipts included.

Both need real OCR (`--ocr real`); EN also needs `datasets` + network
(`uv pip install --python .venv/bin/python datasets`). `--corpus N` sets the
sample size per corpus (default 3, `--no-corpus` to skip). Warn (never
hard-fail) when a source is unavailable or a sample is just hard.

Checks raise `Warn` (non-fatal, shown as `!`) for variance-prone outcomes
(e.g. real OCR recovered no total, segmenter over-merged a 2-receipt image,
report needs FX network) and hard-fail (`✗`) for broken contracts.

The sandbox forces `ocr_backend=apple_vision` + `ocr_text_model=llama3.2:3b`
and asserts every storage path resolves under the tempfile before running.

## Observations & proposed improvements

Surfaced while building/running this harness:

1. ~~**Test isolation gap (recurring).**~~ **DONE.** `tests/conftest.py` now has
   an autouse `_isolate_ocr_config` fixture pinning all `effective_*` getters
   to `settings`, so the persisted `data/ocr_config.json` can't shadow tests.
   The per-file patches were removed.

2. ~~**OCR default backend is a footgun.**~~ **DONE.** Default `ocr_backend` is
   now `apple_vision` (config.py + .env.example). glm-ocr is flagged
   `ocr_only` in `/api/ocr/config` and the picker labels it "⚠ OCR-only" +
   warns when it's the active one-shot model.

3. ~~**No automated gate.**~~ **DONE.** `.github/workflows/ci.yml` runs
   `pytest -q` + `qa/run_qa.py --ocr mock` on every push / PR — both fully
   offline (no Ollama, no network). A pre-push hook isn't used because this
   repo's global `core.hooksPath` (the gitleaks pre-commit) would shadow a
   project-local hook.

4. **Report needs live FX network.** `/reports/expense` calls `fetch_rates`,
   so reports fail offline — at odds with the app's "fully local/offline" goal.
   Cache rates or allow a manual-rate fallback.

5. ~~**Upload size is unenforced.**~~ **DONE.** `services/uploads.py` streams
   every upload with the `max_upload_mb` cap (413 beyond it) and sanitises
   filenames to basenames.

6. **Inconsistent response shapes.** Statement upload returns one of
   `success` / `statement_id` / `transaction_count` depending on path; the QA
   check has to accept all three. Standardise.
