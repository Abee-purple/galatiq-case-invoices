"""The AI Reader: text and PDF Invoices into the standard Invoice shape.

An answer that doesn't fit the Invoice shape, or whose Line Items don't add up to its
totals, is sent back with the specific problems, up to MAX_RETRIES times.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from .ai import AIClient, ask_with_retries
from .models import AIRole, Finding, FindingKind, Invoice, Reading
from .validation import check_arithmetic

READER_INSTRUCTIONS = """\
You read vendor Invoices sent to Acme Corp and extract them into JSON.

Rules:
- Copy values exactly as the document states them. Never correct or recompute a number.
- Dates must be written as YYYY-MM-DD.
- Numbers are plain JSON numbers: no currency symbols or thousands separators.
- currency is the ISO code (USD, EUR, ...). Use USD only if the document gives no indication.
- One line_items entry per item row, with the item name as written on the Invoice.
  An annotation on the row, such as "(rush order)" or "Volume discount", goes in that
  line's note, not in the item name.
- Shipping, freight or delivery charges go in shipping.
- Leave a field null if the document does not give it.
- If a value is unclear, garbled or missing and you had to infer it, still fill it in,
  and add an entry to guessed_fields saying which field, what the document actually
  shows (verbatim, or null if absent), and the value you chose.
"""


class GuessedField(BaseModel):
    field: str = Field(description="The Invoice field, e.g. 'line_items[1].unit_price'")
    read: str | None = Field(description="What the document shows, verbatim; null if absent")
    chosen: str = Field(description="The value you used instead")


class ReaderAnswer(BaseModel):
    invoice: Invoice
    guessed_fields: list[GuessedField] = Field(default_factory=list)


def read_with_ai(document_text: str, ai: AIClient) -> Reading:
    asked = ask_with_retries(
        ai,
        AIRole.READER,
        [("system", READER_INSTRUCTIONS), ("user", document_text)],
        ReaderAnswer,
        _problems,
        advice="If the document itself is wrong, keep its values as written.",
    )
    if asked.unavailable is not None:
        unavailable = Finding(
            kind=FindingKind.AI_UNAVAILABLE,
            message=f"The AI Reader could not be asked: {asked.unavailable}",
        )
        return Reading(None, [unavailable], asked.calls, document_text)
    if not asked.valid and not _as_printed(asked.answer, document_text):
        # Still wrong after the retries. The totals may be a misread the AI can't fix, so
        # the reading is kept for a person to check, but it is not validated.
        unreadable = Finding(
            kind=FindingKind.UNREADABLE_DOCUMENT,
            message="The AI Reader could not produce a valid Invoice in "
            f"{len(asked.calls)} attempts.",
            evidence={"problems": asked.calls[-1].problems},
        )
        invoice = asked.answer.invoice if asked.answer else None
        return Reading(invoice, [unreadable], asked.calls, document_text)
    assert asked.answer is not None
    guesses = [_guessed(g) for g in asked.answer.guessed_fields]
    return Reading(asked.answer.invoice, guesses, asked.calls, document_text)


def _guessed(guess: GuessedField) -> Finding:
    shown = f"'{guess.read}'" if guess.read is not None else "nothing"
    return Finding(
        kind=FindingKind.GUESSED_FIELD,
        message=f"The AI Reader had to guess {guess.field}: the document shows {shown}, "
        f"and it chose '{guess.chosen}'.",
        evidence=guess.model_dump(),
    )


def _problems(answer: ReaderAnswer) -> list[str]:
    """What is wrong with an answer that fits the Invoice shape."""
    problems = [finding.message for finding in check_arithmetic(answer.invoice)]
    if not answer.invoice.invoice_number.strip():
        problems.append("invoice.invoice_number: must not be empty")
    return problems


def _as_printed(answer: ReaderAnswer | None, document_text: str) -> bool:
    """Whether an answer whose totals still disagree after the retries is the document's own
    arithmetic, not a misread: its only problem is the subtotal or total (each line agrees
    with itself), and every number in it appears in the document. Validation then judges
    it like any other Invoice."""
    if answer is None or not answer.invoice.invoice_number.strip():
        return False
    invoice = answer.invoice
    if any("field" not in f.evidence for f in check_arithmetic(invoice)):
        return False  # a line that doesn't add up is more likely a misread number
    numbers = [invoice.subtotal, invoice.tax, invoice.shipping, invoice.total]
    for line in invoice.line_items:
        numbers += [line.quantity, line.unit_price, line.line_total]
    printed = {
        round(float(digits.replace(",", "")), 2)
        for digits in re.findall(r"\d[\d,]*(?:\.\d+)?", document_text)
    }
    return all(round(n, 2) in printed for n in numbers if n is not None)
