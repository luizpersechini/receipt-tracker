"""Request models for the ad-hoc Gmail search/import endpoints."""

from typing import Optional

from pydantic import BaseModel, Field


class GmailImportRequest(BaseModel):
    message_id: str = Field(min_length=1, max_length=64)
    # None → every importable attachment; [] → ignore attachments and render
    # the email body instead.
    attachment_ids: Optional[list[str]] = None
