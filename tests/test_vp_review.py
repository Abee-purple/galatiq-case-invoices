"""VP Review and its Critique, for Invoices the policy sends for a closer look."""

import json

import pytest

from conftest import INVOICES
from invoice_pipeline import AIRole, AIUnavailableError, FakeAI, Outcome


@pytest.fixture
def low_threshold(edited_policy):
    """The default policy with a review threshold that INV-1004 (1,890.00) is above."""
    return edited_policy("review_threshold: 10000", "review_threshold: 1000")


def decide(decision: str, justification: str) -> dict:
    return {"decision": decision, "justification": justification}


def objects(*objections: str) -> dict:
    return {"verdict": "object", "summary": "It has gaps.", "objections": list(objections)}


ACCEPTS = {"verdict": "accept", "summary": "Every claim is supported.", "objections": []}


def conversations(ai: FakeAI, role: AIRole) -> list:
    return [messages for asked, messages in ai.conversations if asked == role]


def test_a_critique_that_objects_once_then_accepts_leaves_both_rounds_on_the_case_file(
    run, low_threshold, payment
):
    first = decide("Approved", "The total is 1,890.00 and every item is in Stock.")
    revised = decide(
        "Approved",
        "The total is 1,890.00, every item is in Stock, and Precision Parts Ltd. has "
        "no earlier Invoices, so there is no history of problems.",
    )
    objection = "The justification does not mention the Vendor's history."
    ai = FakeAI(vp_review=[first, revised], critique=[objects(objection), ACCEPTS])

    case_file = run("invoice_1004.json", policy_path=low_threshold, ai=ai)

    assert case_file.outcome == Outcome.APPROVED
    rounds = [
        (r.vp_review.decision, r.vp_review.justification, r.critique.verdict, r.critique.objections)
        for r in case_file.review_rounds
        if r.vp_review is not None and r.critique is not None
    ]
    assert rounds == [
        (Outcome.APPROVED, first["justification"], "object", [objection]),
        (Outcome.APPROVED, revised["justification"], "accept", []),
    ]
    assert case_file.review_rounds[1].critique is not None
    assert case_file.review_rounds[1].critique.summary == ACCEPTS["summary"]
    _, revision = conversations(ai, AIRole.VP_REVIEW)
    assert objection in revision[-1][1], "the VP revises in answer to the objection"
    assert payment.calls == [("Precision Parts Ltd.", 1890.00)]


def test_a_critique_that_never_accepts_leaves_the_invoice_held_after_two_revisions(
    run, low_threshold, payment
):
    approve = decide("Approved", "The total is 1,890.00 and every item is in Stock.")
    ai = FakeAI(
        vp_review=[approve] * 3,
        critique=[objects("It does not check the Vendor's history.")] * 3,
    )

    case_file = run("invoice_1004.json", policy_path=low_threshold, ai=ai)

    assert case_file.outcome == Outcome.HELD
    assert [r.round for r in case_file.review_rounds] == [1, 2, 3]
    assert all(r.critique and r.critique.verdict == "object" for r in case_file.review_rounds)
    assert case_file.reasons[0] == (
        "The VP Review (Approved) and the Critique still disagreed after 2 revision rounds. "
        "Last objections: It does not check the Vendor's history."
    )
    assert payment.calls == []


def test_the_vp_review_looks_up_the_vendors_history_and_the_lookup_is_on_the_case_file(
    run, tmp_path, low_threshold
):
    # An earlier Invoice from the same Vendor, Rejected for billing 20 WidgetA (15 in Stock).
    data = json.loads((INVOICES / "invoice_1004.json").read_text(encoding="utf-8"))
    data["invoice_number"] = "INV-0999"
    data["line_items"][0]["quantity"] = 20
    earlier = tmp_path / "invoice_0999.json"
    earlier.write_text(json.dumps(data), encoding="utf-8")
    assert run(earlier).outcome == Outcome.REJECTED

    history = {"tool": "vendor_history", "tool_input": "Precision Parts Ltd."}
    ai = FakeAI(
        vp_review=[history, decide("Held", "Precision Parts Ltd. had INV-0999 Rejected.")],
        critique=[ACCEPTS],
    )

    case_file = run("invoice_1004.json", policy_path=low_threshold, ai=ai)

    assert case_file.outcome == Outcome.HELD
    [review_round] = case_file.review_rounds
    [lookup] = review_round.tool_calls
    assert (lookup.tool, lookup.input) == ("vendor_history", "Precision Parts Ltd.")
    assert "INV-0999" in lookup.result and "Rejected" in lookup.result
    _, after_lookup = conversations(ai, AIRole.VP_REVIEW)
    assert lookup.result in after_lookup[-1][1], "the VP is told the lookup's result"
    [critique_request] = conversations(ai, AIRole.CRITIQUE)
    assert lookup.result in critique_request[-1][1], "the Critique sees what the VP looked up"


def test_an_invoice_the_policy_rejected_never_reaches_vp_review(run, payment):
    ai = FakeAI()

    # INV-1005 is above the review threshold, but bills more than we have in Stock.
    case_file = run("invoice_1005.json", ai=ai)

    assert case_file.outcome == Outcome.REJECTED
    assert case_file.review_rounds == []
    assert conversations(ai, AIRole.VP_REVIEW) == []
    assert payment.calls == []


def test_the_stock_and_policy_lookups_answer_from_our_own_data(run, low_threshold):
    ai = FakeAI(
        vp_review=[
            {"tool": "stock_for_item", "tool_input": "widget-a"},
            {"tool": "stock_for_item", "tool_input": "WidgetC"},
            {"tool": "approval_policy"},
            decide("Approved", "3 WidgetA billed against 15 in Stock; total 1,890.00."),
        ],
        critique=[ACCEPTS],
    )

    case_file = run("invoice_1004.json", policy_path=low_threshold, ai=ai)

    stock, unknown, policy = case_file.review_rounds[0].tool_calls
    assert stock.result == "WidgetA: 15 in Stock."
    assert unknown.result == "'WidgetC' is not in the inventory database."
    assert "review_threshold: 1000" in policy.result and "shortfall: Rejected" in policy.result
    assert case_file.outcome == Outcome.APPROVED


def test_a_vp_answer_that_both_looks_up_and_decides_is_sent_back(run, low_threshold):
    muddled = {"tool": "approval_policy", "decision": "Approved", "justification": "Fine."}
    ai = FakeAI(
        vp_review=[muddled, decide("Approved", "Total 1,890.00; all items in Stock.")],
        critique=[ACCEPTS],
    )

    case_file = run("invoice_1004.json", policy_path=low_threshold, ai=ai)

    assert case_file.outcome == Outcome.APPROVED
    first, retry = [c for c in case_file.ai_calls if c.role == AIRole.VP_REVIEW]
    assert first.problems == [
        "set either tool (to call a tool) or decision (to decide), not both"
    ]
    assert retry.problems == []


def test_a_vp_review_that_never_decides_leaves_the_invoice_held(run, low_threshold, payment):
    ai = FakeAI(vp_review=[{"tool": "approval_policy"}] * 7)

    case_file = run("invoice_1004.json", policy_path=low_threshold, ai=ai)

    assert case_file.outcome == Outcome.HELD
    assert case_file.reasons[0] == (
        "Held because the VP Review failed: it did not decide within 6 tool calls"
    )
    [review_round] = case_file.review_rounds
    assert review_round.vp_review is None
    assert len(review_round.tool_calls) == 6, "the tools it called are kept on the Case File"
    assert review_round.failure == "VP Review: it did not decide within 6 tool calls"
    assert payment.calls == []


def test_when_the_vp_review_cannot_be_asked_the_invoice_is_held_with_the_reason(
    run, low_threshold, payment
):
    down = AIUnavailableError("could not reach xAI. Check the network connection.")

    case_file = run("invoice_1004.json", policy_path=low_threshold, ai=FakeAI(vp_review=[down]))

    assert case_file.outcome == Outcome.HELD
    assert case_file.reasons[0] == (
        "Held because the VP Review failed: the AI could not be asked: "
        "could not reach xAI. Check the network connection."
    )
    assert any("above the review threshold" in reason for reason in case_file.reasons[1:])
    assert payment.calls == []


def test_when_the_critique_cannot_be_asked_the_invoice_is_held_even_if_the_vp_approved(
    run, low_threshold, payment
):
    down = AIUnavailableError("xAI rejected the API key (401).")
    ai = FakeAI(vp_review=[decide("Approved", "Total 1,890.00.")], critique=[down])

    case_file = run("invoice_1004.json", policy_path=low_threshold, ai=ai)

    assert case_file.outcome == Outcome.HELD
    [review_round] = case_file.review_rounds
    assert review_round.vp_review is not None
    assert review_round.vp_review.decision == Outcome.APPROVED
    assert review_round.critique is None
    assert case_file.reasons[0] == (
        "Held because the Critique failed: "
        "the AI could not be asked: xAI rejected the API key (401)."
    )
    assert payment.calls == []


def test_each_round_of_vp_review_and_critique_is_reported_as_a_stage(run, low_threshold):
    approve = decide("Approved", "Total 1,890.00; all items in Stock.")
    ai = FakeAI(vp_review=[approve, approve], critique=[objects("Say more."), ACCEPTS])
    reported: list[str] = []

    run("invoice_1004.json", policy_path=low_threshold, ai=ai, on_stage=reported.append)

    assert reported == [
        "Ingestion",
        "Fraud Screen",
        "Validation",
        "Approval",
        "VP Review",
        "Critique",
        "VP Review",
        "Critique",
        "Payment",
    ]
