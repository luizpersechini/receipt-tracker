# Troubleshooting

Practical fixes for the operational snags that actually come up running
Receipt Tracker locally. **None of these involve data loss** — your
receipts live in `data/receipt_tracker.db` and are never touched by any
of the symptoms below.

## First, confirm your data is safe

Whatever the UI shows, the database is the source of truth. Check it
directly:

```bash
sqlite3 data/receipt_tracker.db "SELECT COUNT(*) FROM receipts;"
```

And check what the running server actually serves:

```bash
curl -s http://localhost:8000/api/receipts \
  | python3 -c "import sys,json; print('COUNT:', len(json.load(sys.stdin)['receipts']))"
```

If the `sqlite3` count is non-zero, **your data is intact** — any "empty
app" is a display/launch problem, fixed below, not data loss.

## "The app opens but shows no receipts"

The backend can be serving all your receipts while the page renders
empty. Use the two checks above to localise it:

| `curl` count | `sqlite3` count | Where the problem is |
|---|---|---|
| matches DB | non-zero | **Frontend** — see *blank page* / *stuck filter* below |
| `0` | non-zero | **Wrong directory** — server booted somewhere with an empty `data/` (see below) |
| `0` | `0` | The server is reading a different DB file than you expect — check `DB_PATH`/`.env` and your working directory |

### Cause: server launched from the wrong directory

All storage paths (`data/receipt_tracker.db`, `data/receipts/`, …) are
**relative to the process working directory**. If a server boots from a
different folder, it creates/opens an empty `data/` there and shows no
receipts — while your real DB sits untouched in the project folder.

This is the classic "the `.app` icon opens an empty app" cause: a stale
launcher baked an old project path. Fix:

```bash
cd /path/to/receipt-tracker          # the real project folder
source .venv/bin/activate
DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib uv run uvicorn src.main:app --reload
```

Then rebuild the launcher so the icon always points here
(see *Rebuilding the macOS `.app`* below).

### Cause: a JavaScript error halts the whole frontend

The SPA is a single `static/js/app.js`. **One uncaught error stops the
entire script**, so the page renders blank even though every API call
succeeds. The usual culprit is a bad merge that duplicates a top-level
declaration (e.g. `Uncaught SyntaxError: Identifier 'x' has already been
declared`).

Diagnose: open the browser console (**Cmd+Option+J**) and read the first
red line — it names the file, line, and error.

Fix: get onto the clean, fully-merged `main`, which has the correct
file:

```bash
git fetch origin
git log --oneline origin/main..HEAD   # confirm nothing unique to lose first
git checkout main
git pull origin main
```

If `git log origin/main..HEAD` shows only merge commits (no original
work), switching to `main` loses nothing. Then hard-refresh
(**Cmd+Shift+R**) — a normal refresh keeps the cached broken script.

### Cause: a stuck list filter (survives hard-refresh)

The Receipts list persists its filter in `localStorage`
(`rt.filter.receipts`: search text, date range, amount range, status,
category, currency). A hard-refresh clears the HTTP cache but **not**
`localStorage`, so a leftover filter keeps hiding rows. Reset it from the
console:

```js
localStorage.removeItem("rt.filter.receipts");
localStorage.removeItem("rt.sort.receipts");
location.reload();
```

## "Address already in use" (port 8000)

A server is already listening on 8000 — most often because the terminal
server **and** the `.app` icon are both running. Run only one at a time.
Find and free the port:

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN     # see what's holding it
lsof -ti tcp:8000 | xargs kill -9    # free it
```

Don't reach for a different port (`:8006` etc.) to dodge this — a second
server started elsewhere is how you end up staring at an empty app while
the real one runs fine on 8000.

## Rebuilding the macOS `.app`

The launcher bakes the project path at build time. If you move the repo,
switch branches, or the icon starts opening an empty app, rebuild it from
the current project folder:

```bash
cd /path/to/receipt-tracker
scripts/build_mac_app.sh             # → ~/Applications/Receipt Tracker.app
```

Quit the old icon and open the freshly built one. The new build launches
`uvicorn` from this directory, so it always sees the right `data/`.

## OCR / structuring backend sanity check

Which backends are active is reported by the OCR config endpoint:

```bash
curl -s http://localhost:8000/api/ocr/config
```

- `backend` — the on-device OCR engine (`apple_vision` on macOS,
  `paddle_ocr` on Windows/Linux). `paddle_available: false` means
  PaddleOCR isn't installed (see [`WINDOWS_SETUP.md`](WINDOWS_SETUP.md)).
- Structuring backend is set by `STRUCTURE_BACKEND` in `.env` (`ollama`
  for local, `anthropic` for the Claude API — the latter needs
  `ANTHROPIC_API_KEY`). OCR always runs locally either way.

## TypeSafe (Jev) assist seems to do nothing

`CATEGORY_BACKEND=typesafe` / `SEGMENT_BACKEND=typesafe` are fail-safe: on
any problem the local result is used silently except for a log line.

1. Key present? `TYPESAFE_API_KEY` must be in `.env` or the environment
   (never printed, never committed). A missing key logs
   `TYPESAFE_API_KEY is not set` and falls back.
2. Is the running server actually the code/config you think? A hand-started
   `uvicorn` without `--reload` never picks up `.env` or code changes —
   check `ps -o command -p $(lsof -tnP -iTCP:8000 -sTCP:LISTEN)` and restart.
3. Confidence floor: rows below `TYPESAFE_MIN_CONFIDENCE` (0.7) keep the
   local answer and log `keeping keyword` / `keeping geometric`. Run
   `scripts/compare_categories.py` or `scripts/compare_segments.py` (both
   read-only) to see Jev's answer and confidence per row / pair.
4. Segmentation: the log line `segmenter: typesafe judged N pair(s) → M
   region(s)` confirms the call happened; `M < 2` means the text path fell
   through to the image-only methods, exactly as the local path would.
