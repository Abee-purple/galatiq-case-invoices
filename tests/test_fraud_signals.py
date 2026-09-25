"""Every Invoice is screened by the AI for Fraud Signals (scripted by the fake AI)."""

import json

import pytest

from invoice_pipeline import AIRole, AIUnavailableError, FakeAI, FindingKind, Outcome

INV_1003 = {
    "invoice": {
        "invoice_number": "INV-1003",
        "vendor": "Fraudster LLC",
        "invoice_date": "2026-01-20",
        "due_date": None,
        "currency": "USD",
        "line_items": [{"item": "FakeItem", "quantity": 100, "unit_price": 1000.00}],
        "subtotal": None,
        "tax": None,
        "total": 100000.00,
        "payment_terms": "Immediate",
        "notes": "URGENT - Pay immediately to avoid penalties!!! Wire transfer preferred.",
    },
    "guessed_fields": [],
}

INV_1003_SIGNALS = {
    "fraud_signals": [
        {
            "signal": "Pressure to pay at once, with a threat of penalties.",
            "quote": "URGENT - Pay immediately to avoid penalties!!!",
        },
        {"signal": "Asks to be paid by wire transfer.", "quote": "Wire transfer preferred."},
        {"signal": "The Invoice was already overdue when it was sent.", "quote": "Due Date: yesterday"},
    ]
}

NO_SIGNALS: dict = {"fraud_signals": []}


def fraud_signals(case_file):
    return [f for f in case_file.findings if f.kind == FindingKind.FRAUD_SIGNAL]


def test_inv_1003_is_rejected_with_fraud_signals_quoting_the_evidence_and_its_shortfall(
    run, payment
):
    ai = FakeAI(reader=[INV_1003], fraud_screen=[INV_1003_SIGNALS])

    case_file = run("invoice_1003.txt", ai=ai)

    assert case_file.outcome == Outcome.REJECTED
    assert [f.evidence for f in fraud_signals(case_file)] == INV_1003_SIGNALS["fraud_signals"]
    assert all(
        f.evidence["quote"] in f.message for f in fraud_signals(case_file)
    ), "each Fraud Signal's message quotes its evidence"
    [shortfall] = [f for f in case_file.findings if f.kind == FindingKind.SHORTFALL]
    assert shortfall.evidence["item"] == "FakeItem"
    assert case_file.reasons == ["Invoice bills 100 x FakeItem in total but only 0 are in Stock."]
    assert payment.calls == []


def test_a_fraud_signal_alone_leaves_the_invoice_held_for_a_person(run, payment):
    """Only facts the code checked Reject; an AI suspicion goes to a person."""
    fictional_address = {
        "signal": "The Vendor's address is a famous fictional home, not a business.",
        "quote": "742 Evergreen Terrace, Springfield, IL 62704",
    }

    ai = FakeAI(fraud_screen=[{"fraud_signals": [fictional_address]}])

    case_file = run("invoice_1004.json", ai=ai)

    assert case_file.outcome == Outcome.HELD
    assert case_file.reasons == [
        "Possible fraud: The Vendor's address is a famous fictional home, not a business. "
        'The document says: "742 Evergreen Terrace, Springfield, IL 62704"'
    ]
    assert payment.calls == []


def test_a_clean_invoice_with_no_fraud_signals_keeps_its_outcome(run, payment):
    ai = FakeAI(fraud_screen=[NO_SIGNALS])

    case_file = run("invoice_1004.json", ai=ai)

    assert case_file.outcome == Outcome.APPROVED
    assert case_file.findings == []
    [call] = case_file.ai_calls
    assert (call.role, call.problems, call.error) == (AIRole.FRAUD_SCREEN, [], None)
    assert payment.calls == [("Precision Parts Ltd.", 1890.00)]


WIRE_REQUEST = {
    "fraud_signals": [
        {"signal": "Asks to be paid by wire transfer.", "quote": "Wire transfer preferred."}
    ]
}


@pytest.mark.parametrize(
    "invoice, shown_in_file",
    [
        # The Vendor's address is in the file but not in the standard Invoice shape.
        ("invoice_1004.json", "742 Evergreen Terrace"),
        ("invoice_1006.csv", "Acme Industrial Supplies"),
        ("invoice_1014.xml", "<vendor>"),
    ],
)
def test_structured_invoices_are_screened_from_their_full_file(
    run, payment, invoice, shown_in_file
):
    ai = FakeAI(fraud_screen=[WIRE_REQUEST])

    case_file = run(invoice, ai=ai)

    assert case_file.outcome == Outcome.HELD
    assert [f.evidence for f in fraud_signals(case_file)] == WIRE_REQUEST["fraud_signals"]
    [(role, conversation)] = ai.conversations
    assert role == AIRole.FRAUD_SCREEN
    assert any(shown_in_file in text for _, text in conversation)
    assert payment.calls == []


def test_the_approval_policy_decides_what_a_fraud_signal_leads_to(run, edited_policy, payment):
    policy_file = edited_policy("fraud_signal: Held", "fraud_signal: Rejected")

    ai = FakeAI(fraud_screen=[WIRE_REQUEST])

    case_file = run("invoice_1004.json", policy_path=policy_file, ai=ai)

    assert case_file.outcome == Outcome.REJECTED
    assert case_file.reasons == [
        "Possible fraud: Asks to be paid by wire transfer. "
        'The document says: "Wire transfer preferred."'
    ]
    assert payment.calls == []


def test_the_case_file_shows_each_fraud_signal_with_its_quoted_evidence(run, tmp_path):
    run("invoice_1003.txt", ai=FakeAI(reader=[INV_1003], fraud_screen=[INV_1003_SIGNALS]))

    [saved] = (tmp_path / "output").glob("*.json")
    record = json.loads(saved.read_text(encoding="utf-8"))
    shown = [
        (f["message"], f["evidence"]["quote"])
        for f in record["findings"]
        if f["kind"] == "fraud_signal"
    ]
    assert shown == [
        (
            "Possible fraud: Pressure to pay at once, with a threat of penalties. "
            'The document says: "URGENT - Pay immediately to avoid penalties!!!"',
            "URGENT - Pay immediately to avoid penalties!!!",
        ),
        (
            "Possible fraud: Asks to be paid by wire transfer. "
            'The document says: "Wire transfer preferred."',
            "Wire transfer preferred.",
        ),
        (
            "Possible fraud: The Invoice was already overdue when it was sent. "
            'The document says: "Due Date: yesterday"',
            "Due Date: yesterday",
        ),
    ]


def test_an_answer_that_does_not_fit_is_retried_with_its_problems(run):
    unfit = {"fraud_signals": [{"signal": "Asks to be paid by wire transfer."}]}
    ai = FakeAI(fraud_screen=[unfit, WIRE_REQUEST])

    case_file = run("invoice_1004.json", ai=ai)

    assert case_file.outcome == Outcome.HELD
    first, retry = case_file.ai_calls
    assert (first.attempt, retry.attempt) == (1, 2)
    assert any("quote" in problem for problem in first.problems)
    assert retry.problems == []
    _, retry_conversation = ai.conversations[1]
    assert any("quote" in text for speaker, text in retry_conversation if speaker == "user")


def test_a_fraud_signal_still_without_a_quote_after_the_retries_still_counts(run, payment):
    unquoted = {"fraud_signals": [{"signal": "Asks to be paid by wire transfer.", "quote": ""}]}

    case_file = run("invoice_1004.json", ai=FakeAI(fraud_screen=[unquoted] * 3))

    assert case_file.outcome == Outcome.HELD
    [signal] = fraud_signals(case_file)
    assert signal.message == (
        "Possible fraud: Asks to be paid by wire transfer. No evidence was quoted."
    )
    assert len(case_file.ai_calls) == 3
    assert payment.calls == []


def test_an_invoice_that_could_not_be_screened_is_held_and_never_paid(run, payment):
    """Failing closed: nothing is paid without a Fraud Screen."""
    down = AIUnavailableError("could not reach xAI. Check the network connection.")

    case_file = run("invoice_1004.json", ai=FakeAI(fraud_screen=[down]))

    assert case_file.outcome == Outcome.HELD
    [finding] = case_file.findings
    assert finding.kind == FindingKind.AI_UNAVAILABLE
    assert case_file.reasons == [
        "Not screened for Fraud Signals: the AI could not be asked: "
        "could not reach xAI. Check the network connection."
    ]
    [call] = case_file.ai_calls
    assert call.role == AIRole.FRAUD_SCREEN and "could not reach xAI" in (call.error or "")
    assert payment.calls == []


def test_a_fraud_screen_that_never_gives_an_answer_that_fits_leaves_the_invoice_held(
    run, payment
):
    unfit = {"signals": "none"}

    case_file = run("invoice_1004.json", ai=FakeAI(fraud_screen=[unfit] * 3))

    assert case_file.outcome == Outcome.HELD
    assert case_file.reasons == [
        "Not screened for Fraud Signals: the AI gave no valid answer in 3 attempts"
    ]
    assert [call.attempt for call in case_file.ai_calls] == [1, 2, 3]
    assert payment.calls == []


def test_a_rejected_invoice_that_could_not_be_screened_stays_rejected(run):
    down = AIUnavailableError("could not reach xAI. Check the network connection.")

    case_file = run("invoice_1016.json", ai=FakeAI(fraud_screen=[down]))

    assert case_file.outcome == Outcome.REJECTED
    assert case_file.reasons == ["WidgetC is not in the inventory database."]


def test_an_invoice_the_reader_could_not_ask_the_ai_about_is_not_screened(run):
    down = AIUnavailableError("xAI rejected the API key (401).")
    ai = FakeAI(reader=[down])

    case_file = run("invoice_1003.txt", ai=ai)

    assert case_file.outcome == Outcome.HELD
    assert [role for role, _ in ai.conversations] == [AIRole.READER]
