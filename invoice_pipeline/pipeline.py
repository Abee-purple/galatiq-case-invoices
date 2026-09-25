"""The pipeline entry point and the LangGraph flow behind it.

Ingestion -> Fraud Screen -> Validation -> Approval -> (Payment | VP Review | end)
VP Review <-> Critique, until the Critique accepts or the revisions run out -> (Payment | end)
"""

from __future__ import annotations

import logging
import operator
import time
from collections.abc import Callable
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .ai import AIClient, Messages
from .case_files import write_case_file
from .database import ensure_database, find_approved, load_stock, record_processed
from .fraud_screen import screen_for_fraud
from .ingestion import ingest
from .logs import log_event
from .models import (
    AICall,
    CaseFile,
    Finding,
    FindingKind,
    Invoice,
    ItemNameCleanup,
    Outcome,
    ReviewRound,
)
from .payment import PaymentFunction, mock_payment
from .policy import ApprovalPolicy, Decision, decide
from .validation import validate
from .vp_review import (
    MAX_REVISIONS,
    ReviewTools,
    ask_for_revision,
    review_brief,
    run_critique,
    run_vp_review,
    start_vp_review,
)

log = logging.getLogger(__name__)


class Stage(str, Enum):
    INGESTION = "Ingestion"
    FRAUD_SCREEN = "Fraud Screen"
    VALIDATION = "Validation"
    APPROVAL = "Approval"
    VP_REVIEW = "VP Review"
    CRITIQUE = "Critique"
    PAYMENT = "Payment"


StageListener = Callable[[Stage], None]


class PipelineState(TypedDict, total=False):
    source_file: Path
    document_text: str | None
    invoice: Invoice | None
    findings: Annotated[list[Finding], operator.add]  # each stage adds its own
    ai_calls: Annotated[list[AICall], operator.add]
    item_name_cleanups: list[ItemNameCleanup]
    decision: Decision
    vp_conversation: Messages  # continued when the VP revises
    review_rounds: list[ReviewRound]
    outcome: Outcome
    reasons: list[str]
    payment_result: dict[str, Any] | None


def process_invoice(
    invoice_path: Path,
    *,
    db_path: Path,
    policy: ApprovalPolicy,
    output_dir: Path,
    ai: AIClient,
    payment: PaymentFunction = mock_payment,
    on_stage: StageListener | None = None,
) -> CaseFile:
    """Take one Invoice file through the whole flow, write its Case File and return it.

    `on_stage` is told each Stage as it starts, so callers can show live progress.
    """
    try:
        ensure_database(db_path)
        graph = _build_graph(
            db_path=db_path,
            policy=policy,
            payment=payment,
            on_stage=on_stage or (lambda _: None),
            ai=ai,
        )
        state = graph.invoke({"source_file": Path(invoice_path)})

        case_file = CaseFile(
            source_file=str(invoice_path),
            invoice=state["invoice"],
            findings=state["findings"],
            item_name_cleanups=state.get("item_name_cleanups", []),
            ai_calls=state.get("ai_calls", []),
            review_rounds=state.get("review_rounds", []),
            outcome=state["outcome"],
            reasons=state["reasons"],
            payment_result=state.get("payment_result"),
            processed_at=datetime.now(),
        )
        saved = write_case_file(case_file, output_dir)
        record_processed(db_path, case_file, saved.name)
    except Exception:
        log_event(
            log,
            "invoice not processed",
            level=logging.ERROR,
            exc_info=True,
            invoice_file=str(invoice_path),
        )
        raise
    log_event(
        log,
        "invoice processed",
        invoice_file=str(invoice_path),
        invoice_number=case_file.invoice.invoice_number if case_file.invoice else None,
        outcome=case_file.outcome.value,
        case_file=saved.name,
    )
    return case_file


def _build_graph(
    *,
    db_path: Path,
    policy: ApprovalPolicy,
    payment: PaymentFunction,
    on_stage: StageListener,
    ai: AIClient,
):
    def reported(stage: Stage, node: Callable[[PipelineState], PipelineState]):
        """Announce the Stage to `on_stage`, and log how it ended and how long it took."""

        def reported_node(state: PipelineState) -> PipelineState:
            on_stage(stage)
            fields: dict[str, Any] = {
                "invoice_file": str(state["source_file"]),
                "stage": stage.value,
            }
            started = time.perf_counter()
            try:
                update = node(state)
            except Exception as e:
                log_event(log, "stage error", level=logging.ERROR, **fields, error=repr(e))
                raise
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            log_event(log, "stage finished", **fields, duration_ms=duration_ms)
            return update

        return reported_node

    def ingestion(state: PipelineState) -> PipelineState:
        reading = ingest(state["source_file"], ai)
        return {
            "document_text": reading.document_text,
            "invoice": reading.invoice,
            "findings": reading.findings,
            "ai_calls": reading.ai_calls,
        }

    def fraud_screen(state: PipelineState) -> PipelineState:
        document_text = state["document_text"]
        assert document_text is not None
        screening = screen_for_fraud(document_text, ai)
        return {"findings": screening.findings, "ai_calls": screening.ai_calls}

    def validation(state: PipelineState) -> PipelineState:
        invoice = state["invoice"]
        assert invoice is not None
        result = validate(invoice, load_stock(db_path), find_approved(db_path, invoice))
        return {"findings": result.findings, "item_name_cleanups": result.item_name_cleanups}

    def approval(state: PipelineState) -> PipelineState:
        decision, reasons = decide(policy, state["findings"], state["invoice"])
        update: PipelineState = {"decision": decision, "reasons": reasons}
        if decision != Decision.NEEDS_VP_REVIEW:
            update["outcome"] = Outcome(decision.value)
        return update

    tools = ReviewTools(db_path, policy)

    def brief_for(state: PipelineState) -> str:
        invoice = state["invoice"]
        assert invoice is not None
        return review_brief(invoice, state["findings"], state["reasons"])

    def vp_review(state: PipelineState) -> PipelineState:
        rounds = state.get("review_rounds", [])
        if rounds:
            critique = rounds[-1].critique
            assert critique is not None
            conversation = ask_for_revision(state["vp_conversation"], critique.objections)
        else:
            conversation = start_vp_review(brief_for(state))
        result = run_vp_review(conversation, tools, ai)
        this_round = ReviewRound(
            round=len(rounds) + 1,
            tool_calls=result.tool_calls,
            vp_review=result.review,
            failure=None if result.failure is None else f"VP Review: {result.failure}",
        )
        update: PipelineState = {
            "ai_calls": result.ai_calls,
            "vp_conversation": result.conversation,
            "review_rounds": [*rounds, this_round],
        }
        if result.review is None:
            update["outcome"] = Outcome.HELD
            conclusion = f"Held because the VP Review failed: {result.failure}"
            update["reasons"] = [conclusion, *state["reasons"]]
        return update

    def critique(state: PipelineState) -> PipelineState:
        *earlier, latest = state["review_rounds"]
        review = latest.vp_review
        assert review is not None
        result = run_critique(brief_for(state), policy, latest.tool_calls, review, ai)
        failure = None if result.failure is None else f"Critique: {result.failure}"
        latest = latest.model_copy(update={"critique": result.critique, "failure": failure})
        update: PipelineState = {"ai_calls": result.ai_calls, "review_rounds": [*earlier, latest]}
        if result.critique is None:
            outcome, conclusion = Outcome.HELD, f"Held because the Critique failed: {result.failure}"
        elif result.critique.verdict == "accept":
            outcome = review.decision
            conclusion = (
                f"VP Review: {review.decision.value}, and the Critique accepted it. "
                f"{review.justification}"
            )
        elif latest.round > MAX_REVISIONS:
            outcome = Outcome.HELD
            conclusion = (
                f"The VP Review ({review.decision.value}) and the Critique still disagreed "
                f"after {MAX_REVISIONS} revision rounds. Last objections: "
                + " ".join(result.critique.objections)
            )
        else:
            return update  # no Outcome yet: the Critique objected, so the VP revises
        update["outcome"] = outcome
        update["reasons"] = [conclusion, *state["reasons"]]
        return update

    def pay(state: PipelineState) -> PipelineState:
        invoice = state["invoice"]
        assert invoice is not None and invoice.vendor is not None and invoice.total is not None
        return {"payment_result": payment(invoice.vendor, invoice.total)}

    def route_after_ingestion(state: PipelineState) -> str:
        # Screen whatever text there is, unless the Reader already found the AI unavailable.
        ai_unavailable = any(f.kind == FindingKind.AI_UNAVAILABLE for f in state["findings"])
        if (state["document_text"] or "").strip() and not ai_unavailable:
            return "fraud_screen"
        return route_after_screening(state)

    def route_after_screening(state: PipelineState) -> str:
        # Validate only a clean reading; otherwise the policy decides on the Findings so far.
        unreadable = state["invoice"] is None or any(
            f.kind == FindingKind.UNREADABLE_DOCUMENT for f in state["findings"]
        )
        return "approval" if unreadable else "validation"

    def route_after_approval(state: PipelineState) -> str:
        return {
            Decision.APPROVED: "payment",
            Decision.NEEDS_VP_REVIEW: "vp_review",
        }.get(state["decision"], END)

    def route_after_vp_review(state: PipelineState) -> str:
        return END if "outcome" in state else "critique"

    def route_after_critique(state: PipelineState) -> str:
        if "outcome" not in state:
            return "vp_review"  # the Critique objected; the VP revises
        return "payment" if state["outcome"] == Outcome.APPROVED else END

    graph = StateGraph(PipelineState)
    graph.add_node("ingestion", reported(Stage.INGESTION, ingestion))
    graph.add_node("fraud_screen", reported(Stage.FRAUD_SCREEN, fraud_screen))
    graph.add_node("validation", reported(Stage.VALIDATION, validation))
    graph.add_node("approval", reported(Stage.APPROVAL, approval))
    graph.add_node("vp_review", reported(Stage.VP_REVIEW, vp_review))
    graph.add_node("critique", reported(Stage.CRITIQUE, critique))
    graph.add_node("payment", reported(Stage.PAYMENT, pay))
    graph.add_edge(START, "ingestion")
    graph.add_conditional_edges(
        "ingestion", route_after_ingestion, ["fraud_screen", "validation", "approval"]
    )
    graph.add_conditional_edges("fraud_screen", route_after_screening, ["validation", "approval"])
    graph.add_edge("validation", "approval")
    graph.add_conditional_edges("approval", route_after_approval, ["payment", "vp_review", END])
    graph.add_conditional_edges("vp_review", route_after_vp_review, ["critique", END])
    graph.add_conditional_edges("critique", route_after_critique, ["vp_review", "payment", END])
    graph.add_edge("payment", END)
    return graph.compile()
