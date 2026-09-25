"""VP Review and Critique, for Invoices the Approval Policy sends for a closer look.

The VP Review works step by step: each answer either calls a read-only tool (Stock for an
item, the Vendor's earlier Invoices, the Approval Policy), which our code runs and reports
back, or decides Approved or Held with a justification. Its answer type can't express Rejected,
and it only sees Invoices the policy did not decide outright, so it can't overturn a
Rejected.

A Critique then checks the justification. On an objection the VP revises, up to
MAX_REVISIONS times; the pipeline Holds an Invoice they still disagree on.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal, NamedTuple

import yaml
from pydantic import BaseModel, Field

from .ai import AIClient, Messages, ask_with_retries
from .database import load_stock, vendor_history
from .models import AICall, AIRole, Critique, Finding, Invoice, Outcome, ToolCall, VPReview
from .policy import ApprovalPolicy
from .validation import inventory_item

MAX_REVISIONS = 2
MAX_TOOL_CALLS = 6
"""Tools the VP Review may call in one round before it must decide."""

ToolName = Literal["stock_for_item", "vendor_history", "approval_policy"]

VP_INSTRUCTIONS = f"""\
You are the VP of Finance at Acme Corp, reviewing a vendor Invoice before it is paid.
The automated checks passed it, but the Approval Policy wants a person's judgement.

Decide Approved (pay it) or Held (a person must look at it first). Hold if anything
material is unexplained. Before deciding, call tools to ground your reasoning, one per
answer:
- stock_for_item (tool_input: the item name): how many units Acme has in Stock.
- vendor_history (tool_input: the Vendor name): the Vendor's earlier Invoices and Outcomes.
- approval_policy: the Approval Policy, including the review threshold.
You may call at most {MAX_TOOL_CALLS} tools before you must decide.

To call a tool, set tool (and tool_input), and leave decision and justification null.
To decide, set decision and justification, and leave tool null.

Stock is how many units Acme has on hand: the evidence of what was actually delivered.
An Invoice billing no more than the Stock is backed by it; it does not add to it.
A Vendor with no earlier Invoices is worth mentioning, but it is not a reason to hold on
its own.

The justification is read by a finance colleague next to your decision, so:
- Don't restate the decision. Start with the reason.
- Two to four plain sentences, at most 60 words.
- Give only the facts that decided it, e.g. whether Stock covers what is billed, what the
  Vendor's history shows, and why the amount needed review. Don't walk through the
  arithmetic line by line unless something in it is wrong.
- Address every Finding and note listed with the Invoice.
- Write as a person would. Never use field names, JSON, code or words like null.
"""

CRITIQUE_INSTRUCTIONS = """\
You check a VP of Finance's review of a vendor Invoice before its Outcome is final.

Object if the justification:
- leaves a Finding or note listed with the Invoice unaddressed,
- makes a claim that neither the Invoice nor the VP's tool results support, or
- is inconsistent with the Approval Policy.
Otherwise accept. Each objection must be specific enough for the VP to fix. Do not object
to the decision alone if the justification supports it, nor because it is short or leaves
out facts that raise no concern. Write your summary in plain language for a finance
colleague.
"""


class VPStep(BaseModel):
    """One answer from the VP Review: a tool to call, or its decision."""

    tool: ToolName | None = Field(default=None, description="A tool to call; null once deciding")
    tool_input: str | None = Field(
        default=None,
        description="The item name for stock_for_item, or the Vendor name for vendor_history",
    )
    decision: Literal["Approved", "Held"] | None = Field(
        default=None, description="Your decision; null while calling a tool"
    )
    justification: str | None = Field(
        default=None, description="Why, referencing specific facts; null while calling a tool"
    )


class CritiqueAnswer(BaseModel):
    verdict: Literal["accept", "object"]
    summary: str = Field(description="One or two sentences on what you checked and found")
    objections: list[str] = Field(
        default_factory=list, description="Each specific problem; empty when accepting"
    )


class ReviewTools:
    """The VP Review's read-only tools."""

    def __init__(self, db_path: Path, policy: ApprovalPolicy) -> None:
        self._db_path = db_path
        self._policy = policy
        self._tools: dict[ToolName, Callable[[str], str]] = {
            "stock_for_item": self.stock_for_item,
            "vendor_history": self.vendor_history,
            "approval_policy": lambda _: self.approval_policy(),
        }

    def call(self, tool: ToolName, tool_input: str | None) -> ToolCall:
        result = self._tools[tool](tool_input or "")
        return ToolCall(tool=tool, input=tool_input, result=result)

    def stock_for_item(self, name: str) -> str:
        stock = load_stock(self._db_path)
        item = inventory_item(name, stock)
        if item is None:
            return f"'{name}' is not in the inventory database."
        return f"{item}: {stock[item]} in Stock."

    def vendor_history(self, vendor: str) -> str:
        earlier = vendor_history(self._db_path, vendor)
        if not earlier:
            return f"No earlier Invoices from '{vendor}'."
        return "\n".join(
            f"{p.invoice_number}: {_amount(p.amount, p.currency)}, {p.outcome.value} "
            f"(processed {p.processed_at:%Y-%m-%d})"
            for p in earlier
        )

    def approval_policy(self) -> str:
        return approval_policy_text(self._policy)


def approval_policy_text(policy: ApprovalPolicy) -> str:
    return yaml.safe_dump(
        {
            "review_threshold": policy.review_threshold,
            "findings": {kind.value: outcome.value for kind, outcome in policy.findings.items()},
        },
        sort_keys=False,
    )


def review_brief(invoice: Invoice, findings: list[Finding], reasons: list[str]) -> str:
    """What the VP Review and the Critique are told about the Invoice."""
    listed = "\n".join(f"- {f.message}" for f in findings) or "- none"
    notes = "\n".join(f"- {reason}" for reason in reasons)
    return (
        f"Invoice:\n{invoice.model_dump_json(indent=2, exclude_none=True)}\n\n"
        f"Findings from the automated checks:\n{listed}\n\n"
        f"Why it needs VP Review, and other notes:\n{notes}"
    )


class VPResult(NamedTuple):
    review: VPReview | None
    tool_calls: list[ToolCall]
    conversation: Messages
    """The VP's conversation so far, continued when it revises."""
    ai_calls: list[AICall]
    failure: str | None
    """Why no decision was reached; None when there is a review."""


def start_vp_review(brief: str) -> Messages:
    return [("system", VP_INSTRUCTIONS), ("user", brief)]


def ask_for_revision(conversation: Messages, objections: list[str]) -> Messages:
    listed = "\n".join(f"- {objection}" for objection in objections)
    request = (
        f"The Critique objected to your justification:\n{listed}\n"
        "Call any tools you need, then decide again with a justification that answers "
        "every objection."
    )
    return [*conversation, ("user", request)]


def run_vp_review(conversation: Messages, tools: ReviewTools, ai: AIClient) -> VPResult:
    conversation = list(conversation)
    ai_calls: list[AICall] = []
    tool_calls: list[ToolCall] = []

    def ended(review: VPReview | None, failure: str | None) -> VPResult:
        return VPResult(review, tool_calls, conversation, ai_calls, failure)

    for _ in range(MAX_TOOL_CALLS + 1):
        asked = ask_with_retries(ai, AIRole.VP_REVIEW, conversation, VPStep, _step_problems)
        ai_calls.extend(asked.calls)
        if asked.failure is not None or asked.answer is None:
            return ended(None, asked.failure)
        step = asked.answer
        conversation.append(("assistant", asked.calls[-1].answer or ""))
        if step.decision is not None:
            decision: Literal[Outcome.APPROVED, Outcome.HELD] = (
                Outcome.APPROVED if step.decision == "Approved" else Outcome.HELD
            )
            justification = (step.justification or "").strip()
            return ended(VPReview(decision=decision, justification=justification), None)
        assert step.tool is not None
        if len(tool_calls) == MAX_TOOL_CALLS:
            break
        tool_call = tools.call(step.tool, step.tool_input)
        tool_calls.append(tool_call)
        conversation.append(("user", f"Result of {tool_call.tool}:\n{tool_call.result}"))
    return ended(None, f"it did not decide within {MAX_TOOL_CALLS} tool calls")


def _step_problems(step: VPStep) -> list[str]:
    if (step.tool is None) == (step.decision is None):
        return ["set either tool (to call a tool) or decision (to decide), not both"]
    if step.decision is not None and not (step.justification or "").strip():
        return ["justification: must explain the decision with specific facts"]
    if step.tool == "stock_for_item" and not (step.tool_input or "").strip():
        return ["tool_input: stock_for_item needs the item name"]
    return []


class CritiqueResult(NamedTuple):
    critique: Critique | None
    ai_calls: list[AICall]
    failure: str | None


def run_critique(
    brief: str,
    policy: ApprovalPolicy,
    tool_calls: list[ToolCall],
    review: VPReview,
    ai: AIClient,
) -> CritiqueResult:
    tool_results = "\n\n".join(
        f"{call.tool}({call.input or ''}):\n{call.result}" for call in tool_calls
    )
    request = (
        f"{brief}\n\nApproval Policy:\n{approval_policy_text(policy)}\n"
        f"The tools the VP called, and their results:\n{tool_results or 'none'}\n\n"
        f"The VP's decision: {review.decision.value}\n"
        f"The VP's justification: {review.justification}"
    )
    asked = ask_with_retries(
        ai,
        AIRole.CRITIQUE,
        [("system", CRITIQUE_INSTRUCTIONS), ("user", request)],
        CritiqueAnswer,
        _critique_problems,
    )
    if asked.failure is not None or asked.answer is None:
        return CritiqueResult(None, asked.calls, asked.failure)
    answer = asked.answer
    critique = Critique(
        verdict=answer.verdict, summary=answer.summary.strip(), objections=answer.objections
    )
    return CritiqueResult(critique, asked.calls, None)


def _critique_problems(answer: CritiqueAnswer) -> list[str]:
    if answer.verdict == "object" and not any(o.strip() for o in answer.objections):
        return ["objections: say specifically what is wrong when objecting"]
    return []


def _amount(amount: float | None, currency: str) -> str:
    return "no total" if amount is None else f"{amount:,.2f} {currency}"
