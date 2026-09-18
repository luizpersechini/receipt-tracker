"""Centralised configuration loaded from environment / .env."""

import sys
from pathlib import Path
from typing import Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_ocr_backend() -> str:
    """Pick the on-device text-extraction backend that actually works on this
    platform. Apple Vision is macOS-only; everywhere else (Windows/Linux) the
    cross-platform PaddleOCR engine is the right default. Both fall through to
    the same `text_to_receipt` structuring step. Overridable via OCR_BACKEND
    in .env or the runtime sidebar picker."""
    return "apple_vision" if sys.platform == "darwin" else "paddle_ocr"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Watch folder for auto-ingest (blank = disabled)
    watch_folder: str = ""

    # Ollama
    ollama_model: str = "glm-ocr:latest"
    ollama_host: Optional[str] = None

    # OCR safety rails — prevent the Mac from getting stuck on a huge model
    # or a huge image. See docs/ollama section in .env.example.
    ocr_max_image_dim: int = 1536  # longest-edge resize before OCR (px)
    ocr_max_model_gb: float = 6.0  # refuse to load any model bigger than this
    ocr_timeout_seconds: float = 120.0  # hard timeout on a single ollama.chat call
    ocr_num_predict: int = 2048  # cap output tokens per OCR call. A receipt's
    # JSON is small; without a cap a model that ignores
    # the JSON instruction (e.g. a pure-OCR transcription
    # model fed the extraction prompt) loops to its max
    # length and burns the entire timeout per crop.
    ocr_num_ctx: int = 8192  # cap Ollama context window. Without this,
    # Ollama uses the model's default (128K for
    # llama3.2/3.1), whose KV cache alone can eat
    # 20GB+ on a 24GB Mac. Receipt prompts are
    # short; 8K is ample. Complements ocr_max_model_gb,
    # which only guards weight size, not the KV cache.
    ocr_concurrency: int = 1  # max simultaneous OCR jobs
    ocr_warmup: bool = True  # preload the vision model at server start
    ocr_preprocess: bool = True  # auto-rotate + CLAHE + unsharp before OCR

    # OCR backend selector. Three on-/off-device backends:
    #   "apple_vision" — Apple's on-device Vision framework for text + a small
    #       text LLM (settings.ocr_text_model) to structure it. Fast on Apple
    #       Silicon, low RAM, reliable — but macOS-only.
    #   "paddle_ocr"   — PaddleOCR (PP-OCR), the cross-platform equivalent:
    #       local CPU text extraction + the same text-LLM structuring step.
    #       The right default on Windows/Linux where Apple Vision is absent.
    #   "ollama_vision"— one vision model reads AND structures in a single shot.
    #       Heavier (RAM) and depends on the vision model following a JSON
    #       prompt; the shipped glm-ocr is OCR-only and loops/times out on it.
    # The default is platform-adaptive (see `_default_ocr_backend`); both
    # text-extraction backends feed the identical structuring step, so the
    # rest of the pipeline is unaffected by which one runs.
    ocr_backend: str = ""  # blank → resolved per-platform in _apply_backend_default
    ocr_text_model: str = "llama3.2:3b"  # used by apple_vision / paddle_ocr

    # PaddleOCR (paddle_ocr backend) tuning.
    paddle_lang: str = "pt"  # recognition language; "pt" covers pt-BR + Latin
    # receipts. Use "en", "ch", "latin", "ml" etc. per
    # PaddleOCR's supported language list.
    paddle_use_gpu: bool = False  # CPU by default; GPU needs paddlepaddle-gpu+CUDA
    # Override PaddleOCR's per-language default models (3.x). Blank = the
    # default for PADDLE_LANG. Detection is language-agnostic, so a stronger
    # detector is the safe accuracy lever when totals aren't being read:
    #   PADDLE_TEXT_DET_MODEL=PP-OCRv5_server_det   (bigger detector)
    # Recognition is language-specific — for pt-BR the default
    # PP-OCRv6_medium_rec is multilingual; latin_PP-OCRv5_mobile_rec is a
    # latin-specialized alternative. (First use downloads the chosen model.)
    paddle_text_det_model: str = ""
    paddle_text_rec_model: str = ""

    # Structuring backend — which LLM turns the OCR'd text into receipt JSON.
    # This is SEPARATE from the OCR backend: OCR always runs locally
    # (apple_vision / paddle_ocr); only the structuring step's LLM changes.
    #   "ollama"    — local Ollama text model (settings.ocr_text_model). Default;
    #                 keeps the app fully local/offline.
    #   "anthropic" — hosted Claude API. Lets a low-powered machine use a much
    #                 stronger model for structuring (the real accuracy lever),
    #                 at the cost of sending the receipt TEXT (not the image) to
    #                 the API. Needs ANTHROPIC_API_KEY. Opt-in only.
    structure_backend: str = "ollama"
    anthropic_api_key: Optional[str] = None  # falls back to the ANTHROPIC_API_KEY env var
    anthropic_model: str = "claude-opus-4-8"  # structuring is simple+high-volume —
    # claude-haiku-4-5 is far cheaper and ample here; override via ANTHROPIC_MODEL.

    # Category backend — how a receipt / statement line gets its expense
    # category (meals, transportation, …):
    #   "keyword"  — local keyword heuristics (default; fully offline).
    #   "typesafe" — TypeSafe's Jev (System One) API: one batched `choice`
    #                question per item, returns a category with calibrated
    #                probabilities + confidence. Sends merchant/amount/date
    #                (and receipt line items when present) to the API. Needs
    #                TYPESAFE_API_KEY. Opt-in only, like `structure_backend`.
    # Below `typesafe_min_confidence` the keyword heuristic is used instead.
    category_backend: str = "keyword"
    typesafe_api_key: Optional[str] = None  # falls back to the TYPESAFE_API_KEY env var
    typesafe_model: str = "jev-latest"
    typesafe_min_confidence: float = 0.7

    # Gmail ingestion (Phase B of the Uber integration)
    # Set up: see scripts/gmail_auth.py for the one-time OAuth dance.
    gmail_credentials_path: Path = Path("data/gmail/credentials.json")
    gmail_token_path: Path = Path("data/gmail/token.json")
    gmail_query: str = "from:(noreply@uber.com OR receipts@uber.com)"
    gmail_processed_label: str = "rt-processed"
    gmail_max_messages_per_sync: int = 100

    # Storage paths
    data_dir: Path = Path("data")
    db_path: Path = Path("data/receipt_tracker.db")
    receipts_dir: Path = Path("data/receipts")
    statements_dir: Path = Path("data/statements")
    reports_dir: Path = Path("data/reports")
    watch_log_path: Path = Path("data/watch_log.jsonl")

    # Upload limit (currently advisory; FastAPI doesn't enforce by default)
    max_upload_mb: int = 25

    # Password for bank-issued protected statement PDFs (Itaú locks them to
    # a CNPJ prefix). Tried as-is and digits-only; empty = no unlock attempt.
    statement_pdf_password: str = ""

    @model_validator(mode="after")
    def _apply_backend_default(self) -> "Settings":
        # Resolve a blank OCR_BACKEND to the platform-appropriate on-device
        # engine. An explicit value (env or .env) is always respected.
        if not (self.ocr_backend or "").strip():
            self.ocr_backend = _default_ocr_backend()
        return self


settings = Settings()


def ensure_dirs() -> None:
    for d in (
        settings.data_dir,
        settings.receipts_dir,
        settings.statements_dir,
        settings.reports_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)
