"""The Fraud Screen: the AI reads an Invoice's full source document, in any format, for signs
that it may not be genuine, quoting the evidence for each Fraud Signal.

Like all AI judgement here, it can only make an Outcome stricter. If the screen can't be done
(the AI is unavailable, or never gives an answer that fits), that is an `ai_unavailable`
Finding: nothing is paid without a Fraud Screen.
"""

from __future__ import annotations

from typing import NamedTuple

from pydantic import BaseModel, Field

from .ai import AIClient, ask_with_retries
from .models import AICall, AIRole, Finding, FindingKind

FRAUD_SCREEN_INSTRUCTIONS = """\
You screen vendor Invoices sent to Acme Corp for signs that they may not be genuine.

Read the whole document and list every Fraud Signal you see, such as:
- Pressure to pay: urgency, threats of penalties, "pay immediately", or a due date that has
  already passed or makes no sense (e.g. "due yesterday").
- Unusual payment requests: wire transfer, gift cards, crypto, new or changed bank details,
  or paying someone other than the Vendor.
- Implausible details: a famous landmark, fictional or impossible address for the Vendor,
  or a Vendor name that looks made up or imitates a well-known company.
- Anything else suggesting the document was made up to get paid.

For each one, say what is suspicious in one sentence, and quote the text from the document
that shows it, copied exactly.

Do not report ordinary data problems: typos, garbled text, arithmetic errors, missing
fields, the currency, or whether we stock an item. Other checks handle those.
If there are no Fraud Signals, return an empty list. Never invent one.
"""


class FraudSignal(BaseModel):
    signal: str = Field(description="What is suspicious, in one sentence")
    quote: str = Field(description="The text in the document that shows it, copied exactly")


class FraudScreenAnswer(BaseModel):
    fraud_signals: list[FraudSignal]


class Screening(NamedTuple):
    findings: list[Finding]
    ai_calls: list[AICall]


def screen_for_fraud(document_text: str, ai: AIClient) -> Screening:
    asked = ask_with_retries(
        ai,
        AIRole.FRAUD_SCREEN,
        [("system", FRAUD_SCREEN_INSTRUCTIONS), ("user", document_text)],
        FraudScreenAnswer,
        _problems,
    )
    if asked.answer is None:
        not_screened = Finding(
            kind=FindingKind.AI_UNAVAILABLE,
            message=f"Not screened for Fraud Signals: {asked.failure}",
        )
        return Screening([not_screened], asked.calls)
    # A Fraud Signal still without a quote after the retries is kept anyway:
    # it can only make the Outcome stricter.
    return Screening([_finding(found) for found in asked.answer.fraud_signals], asked.calls)


def _finding(found: FraudSignal) -> Finding:
    description = found.signal.strip()
    if not description.endswith((".", "!", "?")):
        description += "."
    quote = found.quote.strip()
    evidence = f'The document says: "{quote}"' if quote else "No evidence was quoted."
    return Finding(
        kind=FindingKind.FRAUD_SIGNAL,
        message=f"Possible fraud: {description} {evidence}",
        evidence=found.model_dump(),
    )


def _problems(answer: FraudScreenAnswer) -> list[str]:
    return [
        f"fraud_signals.{i}.quote: must quote the text in the document that shows it"
        for i, found in enumerate(answer.fraud_signals)
        if not found.quote.strip()
    ]
