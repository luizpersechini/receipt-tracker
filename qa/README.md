# QA harness

`run_qa.py` boots the app **in-process** (FastAPI `TestClient`) against a
**throwaway** db + data dir under a tempfile — it never touches the live
`data/`. It then drives the real flows end to end and reports every error with
timing, so you can catch regressions before they reach live data.

## Run

```bash
python qa/run_qa.py                # real OCR (Apple Vision + your configured text model) — needs Ollama
python qa/run_qa.py --ocr mock     # stub OCR — fast, deterministic (seconds)
python qa/run_qa.py --loops 3      # run the battery 3×
python qa/run_qa.py --until-clean  # loop until a fully green run (cap 5)
python qa/run_qa.py --keep         # keep the temp sandbox for inspection
python qa/run_qa.py --report qa/last.json   # also dump JSON results
python qa/run_qa.py --corpus 8 --corpus-seed 7   # 8 CORD receipts, FIXED sample (reproducible A/B)
python qa/run_qa.py --text-model llama3.1:8b     # force a specific structuring model
```

The structuring model defaults to your **configured effective model** (the
persisted picker choice), so the corpus measures what your app actually runs.
`--corpus-seed N` pins the random CORD sample so you can compare a before/after
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
| real corpus (CORD)         | random real receipts from the CORD dataset, OCR'd + scored vs ground-truth totals      |

### Real-corpus accuracy step (CORD)

Each run pulls a **fresh random sample** of real receipt photos from the
[CORD dataset](https://huggingface.co/datasets/naver-clova-ix/cord-v2)
(HuggingFace, CC BY 4.0 — images + ground-truth totals), OCRs them through the
real pipeline, and **scores total-amount accuracy** against ground truth.
Results append to `qa/corpus_scoreboard.jsonl` (gitignored) and the last 5
runs' accuracy print as a trend, so you can see whether an algo change moves
the number — the "test and improve" loop.

- Needs `datasets` + network: `uv pip install --python .venv/bin/python datasets`.
- Real OCR only (mock can't read totals). Controlled by `--corpus N`
  (default 3, `--no-corpus` to skip). Warns (never hard-fails) if `datasets`
  is missing, HF is unreachable, or the sample is just hard.
- CORD is Indonesian (IDR), so it stress-tests locale/number handling beyond
  the user's BRL/CAD receipts.

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

3. **No automated gate.** Nothing enforces green before push. Wire
   `python qa/run_qa.py --ocr mock` + `pytest -q` into a pre-push hook or CI.

4. **Report needs live FX network.** `/reports/expense` calls `fetch_rates`,
   so reports fail offline — at odds with the app's "fully local/offline" goal.
   Cache rates or allow a manual-rate fallback.

5. **Upload size is unenforced.** `max_upload_mb` is advisory; a huge PDF can
   wedge OCR. Enforce a limit at the upload endpoints.

6. **Inconsistent response shapes.** Statement upload returns one of
   `success` / `statement_id` / `transaction_count` depending on path; the QA
   check has to accept all three. Standardise.
