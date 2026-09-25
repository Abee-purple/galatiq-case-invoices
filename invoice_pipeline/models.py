"""The shared domain types: Invoice, Finding, Outcome and Case File."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, Field


class LineItem(BaseModel):
    item: str
    quantity: float
    unit_price: float
    line_total: float | None = None
    note: str | None = Field(
        default=None, description="An annotation on the line, e.g. 'rush order'; not the item"
    )


class Invoice(BaseModel):
    """The standard Invoice shape every format is read into."""

    invoice_number: str
    vendor: str | None = None
    invoice_date: str | None = None
    due_date: str | None = None
    currency: str = "USD"
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float | None = None
    tax: float | None = None
    shipping: float | None = Field(default=None, description="Shipping, freight or delivery")
    total: float | None = None
    payment_terms: str | None = None
    notes: str | None = None


class Outcome(str, Enum):
    APPROVED = "Approved"
    HELD = "Held"
    REJECTED = "Rejected"


class FindingKind(str, Enum):
    SHORTFALL = "shortfall"
    UNKNOWN_ITEM = "unknown_item"
    INVALID_QUANTITY = "invalid_quantity"
    ARITHMETIC_MISMATCH = "arithmetic_mismatch"
    MISSING_VENDOR = "missing_vendor"
    MISSING_TOTAL = "missing_total"
    MISSING_DUE_DATE = "missing_due_date"
    NON_USD_CURRENCY = "non_usd_currency"
    DUPLICATE = "duplicate"
    UNREADABLE_DOCUMENT = "unreadable_document"
    GUESSED_FIELD = "guessed_field"
    AI_UNAVAILABLE = "ai_unavailable"
    FRAUD_SIGNAL = "fraud_signal"


class AIRole(str, Enum):
    READER = "Reader"
    FRAUD_SCREEN = "Fraud Screen"
    VP_REVIEW = "VP Review"
    CRITIQUE = "Critique"


class AICall(BaseModel):
    """One question put to the AI, kept on the Case File so every answer can be traced."""

    role: AIRole
    attempt: int
    answer: str | None
    problems: list[str] = Field(
        default_factory=list, description="What was wrong with the answer; sent back on a retry"
    )
    error: str | None = None
    duration_ms: float


class Finding(BaseModel):
    kind: FindingKind
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class Reading(NamedTuple):
    """What Ingestion got from a file: the Invoice (if any), Findings, and the AI calls made.

    `document_text` is the full source document as text, for the Fraud Screen; None when
    the file has no text to read.
    """

    invoice: Invoice | None
    findings: list[Finding]
    ai_calls: list[AICall]
    document_text: str | None = None


class ItemNameCleanup(BaseModel):
    """A Line Item name matched to an inventory item only after ignoring spaces, case and dashes."""

    billed_as: str
    matched_to: str


class ToolCall(BaseModel):
    """One read-only tool the VP Review called, and what it returned."""

    tool: str
    input: str | None
    result: str


class VPReview(BaseModel):
    """The VP Review's judgement: it can only Approve or Hold, never Reject, and it only
    sees Invoices the Approval Policy didn't Reject, so it can't overturn one."""

    decision: Literal[Outcome.APPROVED, Outcome.HELD]
    justification: str


class Critique(BaseModel):
    """A second AI's check of a VP Review's reasoning."""

    verdict: Literal["accept", "object"]
    summary: str = Field(description="What the Critique checked and found")
    objections: list[str] = Field(default_factory=list)


class ReviewRound(BaseModel):
    """One round: the tools the VP Review called, its decision, and the Critique of it."""

    round: int
    tool_calls: list[ToolCall] = Field(default_factory=list)
    vp_review: VPReview | None = Field(
        default=None, description="None when the VP Review reached no decision"
    )
    critique: Critique | None = Field(
        default=None, description="None when the Critique did not run or failed"
    )
    failure: str | None = Field(
        default=None, description="Why the round ended without a decision or a Critique"
    )


class ProcessedInvoice(BaseModel):
    """One row of processing history: an Invoice that went through the pipeline."""

    invoice_number: str
    vendor: str
    amount: float | None
    currency: str
    outcome: Outcome
    processed_at: datetime
    case_file: str


class CaseFile(BaseModel):
    """The complete record of how one Invoice was processed."""

    source_file: str
    invoice: Invoice | None = Field(description="None when no Invoice could be read from the file")
    findings: list[Finding]
    item_name_cleanups: list[ItemNameCleanup]
    ai_calls: list[AICall]
    review_rounds: list[ReviewRound] = Field(default_factory=list)
    outcome: Outcome
    reasons: list[str]
    payment_result: dict[str, Any] | None
    processed_at: datetime

    @property
    def main_reason(self) -> str:
        """The one-line reason shown in summaries: the first reason at the winning severity."""
        return self.reasons[0] if self.reasons else ""
