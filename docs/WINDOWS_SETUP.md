# Running Receipt Tracker on Windows

This guide sets up Receipt Tracker on a **Windows PC** using the cross-platform
stack validated on the `claude/project-compatibility-check-g7lau0` branch:

- **OCR:** PaddleOCR (local, on-device) — the Windows equivalent of the Mac's
  Apple Vision. No cloud, runs on CPU.
- **Structuring:** the hosted Claude API turns the OCR'd text into receipt JSON.
  This is opt-in; only the receipt **text** is sent, never the image.

**No Ollama and no local LLM are required on Windows.** OCR runs locally via
PaddleOCR; the structuring LLM is the Claude API. That's the whole point of this
configuration — a modest Windows box needs no GPU and no multi-GB local model.

> The app's defaults stay local/offline (the Mac keeps Apple Vision + local
> Ollama). Everything below is configured per-machine via a `.env` file, which
> is gitignored — nothing here changes the shared codebase defaults.

---

## What you get

On the project's 25-receipt regression corpus this config scored **~80% of
totals correct**, with misreads failing **safe** (the model returns *no* total
rather than a wrong one), so they surface in the review queue instead of slipping
through. See the repo's tuning history for the full A/B (PaddleOCR + Claude Haiku
was the best accuracy/cost/speed point).

---

## Prerequisites

- **Python 3.13** (the project requires 3.10+; 3.13 is recommended).
- **uv** (the project's package manager).
- An **Anthropic API key** with billing enabled (`sk-ant-api03-…`), created at
  <https://console.anthropic.com> → Settings → API keys. Copy it at creation —
  it's shown only once.

Install uv (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

uv can fetch Python 3.13 for you, so you don't need a separate Python install.

---

## Setup (PowerShell)

```powershell
# 1. Get the code
git clone https://github.com/luizpersechini/receipt-tracker.git
cd receipt-tracker
git checkout claude/project-compatibility-check-g7lau0

# 2. Create a 3.13 virtual environment (uv downloads Python if needed)
uv venv --python 3.13
.\.venv\Scripts\Activate.ps1

# 3. Install dependencies. requirements.txt pulls paddleocr + paddlepaddle
#    (win32 markers) and the anthropic SDK automatically.
uv pip install -r requirements.txt
```

If `paddlepaddle` fails to install, install the two OCR packages explicitly
(CPU build) and re-run:

```powershell
uv pip install paddleocr paddlepaddle anthropic
```

---

## Configure (`.env`)

Create a file named `.env` in the project root with these four lines (paste your
real key):

```dotenv
OCR_BACKEND=paddle_ocr
STRUCTURE_BACKEND=anthropic
ANTHROPIC_MODEL=claude-haiku-4-5
ANTHROPIC_API_KEY=sk-ant-api03-your-real-key-here
```

PowerShell one-liner to create it (replace the key first):

```powershell
@"
OCR_BACKEND=paddle_ocr
STRUCTURE_BACKEND=anthropic
ANTHROPIC_MODEL=claude-haiku-4-5
ANTHROPIC_API_KEY=sk-ant-api03-your-real-key-here
"@ | Set-Content -Encoding utf8 .env
```

Notes:
- `OCR_BACKEND` can be left blank — on Windows it auto-resolves to `paddle_ocr`.
  It's set explicitly here for clarity.
- `ANTHROPIC_MODEL=claude-haiku-4-5` is the cheap, accurate choice for receipt
  structuring (fractions of a cent per receipt). `claude-opus-4-8` is the
  codebase default but is overkill and pricier for this task.
- `.env` is gitignored — your key never gets committed.

---

## Verify the OCR engine (one-time model download)

PaddleOCR downloads its detection/recognition models on first use (a few hundred
MB, cached under `%USERPROFILE%\.paddlex`). Warm it up and confirm it builds:

```powershell
python -c "from src.services import paddle_ocr as p; print('available:', p.is_available()); print('warmup:', p.warmup())"
```

Expect:

```
available: True
warmup: True
```

> If you have **Git Bash** or **WSL**, `bash scripts/test_paddle.sh` does the
> install + warmup (and, if a local LLM were present, an accuracy A/B) in one
> shot. The PowerShell steps above are the native equivalent and don't need bash.

---

## Run the app

```powershell
uvicorn src.main:app --host 127.0.0.1 --port 8000
```

Open <http://localhost:8000>. In the sidebar's OCR selector you should see
**"PaddleOCR (on-device · cross-platform)"** selected. Upload a receipt and
confirm the vendor / total / date come out right.

The structuring step calls the Claude API; the first upload after a cold start
pays the PaddleOCR model-load cost (warmed above), then each receipt is fast.

---

## What "working" looks like

- `paddle_ocr.warmup()` → `True`.
- Uploading a clear receipt fills vendor, total, currency, date.
- A receipt the OCR can't read cleanly comes back with **no total** and lands in
  the review queue for a manual fix — that's the intended safe-failure, not a bug.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `available: False` from warmup | `paddleocr` isn't installed → `uv pip install paddleocr paddlepaddle`. |
| `warmup: False`, "No model hoster" | No internet to download the PP-OCR models on first run. Connect and retry; they cache afterward. |
| Structuring rows show `... structuring failed: 401` | Bad/empty `ANTHROPIC_API_KEY`, or the key's org has no billing/credits. |
| `... structuring failed: 429` | Anthropic rate limit — wait and retry, or check your plan limits. |
| Sidebar says "Ollama not reachable" | Expected and harmless on this config — Ollama isn't used. OCR (PaddleOCR) + structuring (Claude) don't need it. |
| `paddlepaddle` install error on Windows | Ensure Python is 3.10–3.12/3.13 (not 3.9) and 64-bit; reinstall the CPU wheel: `uv pip install paddlepaddle`. |

---

## Privacy & cost

- **Privacy:** with `STRUCTURE_BACKEND=anthropic`, the OCR'd **text** of each
  receipt is sent to the Claude API for structuring. The image never leaves the
  machine. If that's not acceptable, set `STRUCTURE_BACKEND=ollama` and run a
  local text model instead (requires Ollama + a model, heavier on the machine).
- **Cost:** receipt structuring is ~1–2K tokens per receipt. On
  `claude-haiku-4-5` that's a fraction of a cent each — typically pennies/month
  for personal use.
