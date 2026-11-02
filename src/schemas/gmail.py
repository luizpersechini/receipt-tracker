"""Request models for the ad-hoc Gmail search/import endpoints."""

from typing import Optional

from pydantic import BaseModel, Field


class GmailImportRequest(BaseModel):
    message_id: str = Field(min_length=1, max_length=64)
    # None → every importable attachment; [] → ignore attachments and render
    # the email body instead.
    attachment_ids: Optional[list[str]] = None


class FindReceiptsRequest(BaseModel):
    statement_id: int
    # None → every open line (no receipt, not a credit, not cancelled).
    indices: Optional[list[int]] = None
    max_per_txn: int = Field(3, ge=1, le=10)


class ImportMatchRequest(BaseModel):
    statement_id: int
    transaction_index: int = Field(ge=0)
    message_id: str = Field(min_length=1, max_length=64)
    attachment_ids: Optional[list[str]] = None
