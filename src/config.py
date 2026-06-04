"""Centralised configuration loaded from environment / .env."""

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # OCR backend selector. "ollama_vision" runs the prior glm-ocr/llava
    # path. "apple_vision" runs Apple's on-device Vision framework + a
    # small text LLM (settings.ocr_text_model) to structure the result —
    # much faster on Apple Silicon, and no large vision encoder resident
    # in RAM.
    ocr_backend: str = "ollama_vision"
    ocr_text_model: str = "llama3.2:3b"  # used when ocr_backend=apple_vision

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


settings = Settings()


def ensure_dirs() -> None:
    for d in (
        settings.data_dir,
        settings.receipts_dir,
        settings.statements_dir,
        settings.reports_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)
