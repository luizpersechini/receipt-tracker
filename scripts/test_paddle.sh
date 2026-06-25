#!/bin/bash
#
# Test the PaddleOCR backend end-to-end on this machine (Mac or Windows/WSL).
# Runs the whole validation sequence in one shot:
#   1. activates the project venv (if present)
#   2. installs paddleocr + paddlepaddle (CPU) if missing
#   3. downloads the PP-OCR models + confirms the engine builds (warmup)
#   4. if Ollama is up, scores PaddleOCR vs Apple Vision on your real receipts
#      with the SAME corpus sample (a fair A/B)
#
# Usage:
#   scripts/test_paddle.sh [corpus_n]      # default corpus_n = 8
#
# This only READS your receipts (the corpus step is read-only) and writes the
# usual scoreboard line to qa/corpus_scoreboard.jsonl. Safe to re-run.
#
set -uo pipefail

CORPUS_N="${1:-8}"
SEED=42  # fixed so both backends score the identical sample

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
warn() { printf "\033[33m%s\033[0m\n" "$1"; }
ok()   { printf "\033[32m%s\033[0m\n" "$1"; }

# ── 1. venv ────────────────────────────────────────────────────────────────
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  ok "✓ activated .venv"
  PY=python
else
  warn "! no .venv found — using system python3 (consider: python3 -m venv .venv)"
  PY=python3
fi

# ── 2. install paddle deps if missing ──────────────────────────────────────
bold "→ checking PaddleOCR install"
if ! "$PY" -c "import paddleocr" 2>/dev/null; then
  warn "  paddleocr not installed — installing paddleocr + paddlepaddle (CPU)…"
  "$PY" -m pip install paddleocr paddlepaddle || {
    warn "  install failed. On Apple Silicon the paddlepaddle wheel can be picky —"
    warn "  paste the error and we'll pin a working version."
    exit 1
  }
else
  ok "  ✓ paddleocr already installed"
fi

# ── 3. warmup: download models + build the engine ──────────────────────────
bold "→ building PaddleOCR + downloading models (first run pulls a few hundred MB)"
"$PY" - <<'PYEOF'
import sys
from src.services import paddle_ocr as p
print("  available:", p.is_available())
okw = p.warmup()
print("  warmup:", okw)
if not okw:
    print("  ↳ warmup failed — see the message above (usually a network/model-download issue).")
    sys.exit(2)
PYEOF
WARM=$?
if [ "$WARM" -ne 0 ]; then
  warn "✗ PaddleOCR could not download/build its models on this machine."
  warn "  Everything downstream needs this — fix the download, then re-run."
  exit "$WARM"
fi
ok "✓ PaddleOCR engine is ready on this machine"

# ── 4. accuracy A/B (needs Ollama for the structuring step) ────────────────
bold "→ checking Ollama (needed to score real-receipt accuracy)"
if curl -s --max-time 3 http://localhost:11434/api/tags >/dev/null 2>&1; then
  ok "  ✓ Ollama reachable"
  bold "→ scoring PaddleOCR vs Apple Vision on $CORPUS_N receipts (seed $SEED)"
  echo "  --- PaddleOCR ---"
  "$PY" qa/run_qa.py --ocr real --backend paddle_ocr   --corpus "$CORPUS_N" --corpus-seed "$SEED"
  echo "  --- Apple Vision ---"
  "$PY" qa/run_qa.py --ocr real --backend apple_vision --corpus "$CORPUS_N" --corpus-seed "$SEED"
  echo
  ok "✓ A/B complete — compare the two total-accuracy numbers above"
  echo "  (also appended to qa/corpus_scoreboard.jsonl)"
else
  warn "! Ollama not reachable on :11434 — skipping the accuracy A/B."
  warn "  Start it and re-run to score accuracy:"
  warn "      ollama serve   &&   ollama pull llama3.2:3b"
  warn "  The engine itself is verified working (step 3 passed)."
fi

echo
bold "Done. To try it live: start the app and pick 'PaddleOCR' in the sidebar OCR selector."
echo "  uvicorn src.main:app --reload"
