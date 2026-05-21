"""Pydantic models for receipt-related requests/responses."""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class ExpenseTypeUpdate(BaseModel):
    expense_type: Literal["corp_card", "reimbursement"]


class BulkDeleteRequest(BaseModel):
    ids: List[int] = Field(..., min_length=1, max_length=1000)


class BBox(BaseModel):
    """A receipt bounding box in image-pixel coordinates."""
    x: int = Field(..., ge=0)
    y: int = Field(..., ge=0)
    w: int = Field(..., gt=0)
    h: int = Field(..., gt=0)


class ReceiptCommitRequest(BaseModel):
    """Body for POST /api/upload/receipt/commit — references a previously
    uploaded file (via filename returned by /preview) and the list of bboxes
    the user accepted/edited from the segmenter."""
    filename: str = Field(..., min_length=1)
    bboxes: List[BBox] = Field(..., min_length=1, max_length=20)
    description: Optional[str] = None


# Fields that live on the metadata JSON blob — anything in this set will be
# merged into the receipt's metadata column. Top-level fields (description,
# expense_type) go to their own columns; the router splits them.
METADATA_FIELDS = {
    "date", "vendor_name", "total_amount", "currency", "category",
    "city", "state", "country", "vendor_address", "payment_method",
    "tax_amount", "tip_amount", "service_charge",
}


class ReceiptEdit(BaseModel):
    """Partial update for a receipt. Every field is optional."""

    # Top-level columns
    description: Optional[str] = None
    expense_type: Optional[Literal["corp_card", "reimbursement"]] = None

    # Metadata fields
    date: Optional[str] = None
    vendor_name: Optional[str] = None
    vendor_address: Optional[str] = None
    total_amount: Optional[float] = Field(default=None, ge=0)
    currency: Optional[str] = Field(default=None, min_length=3, max_length=3)
    category: Optional[Literal[
        "meals", "travel", "transportation", "lodging",
        "office_supplies", "other",
    ]] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = Field(default=None, min_length=2, max_length=2)
    payment_method: Optional[str] = None
    tax_amount: Optional[float] = Field(default=None, ge=0)
    tip_amount: Optional[float] = Field(default=None, ge=0)
    service_charge: Optional[float] = Field(default=None, ge=0)
