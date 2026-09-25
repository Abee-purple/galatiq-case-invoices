"""Text and PDF Invoices are read by the AI Reader (scripted by the fake AI)."""

import copy
from typing import Any

from pypdf import PdfWriter

from invoice_pipeline import AIRole, AIUnavailableError, FakeAI, FindingKind, Outcome

INV_1001: dict[str, Any] = {
    "invoice": {
        "invoice_number": "INV-1001",
        "vendor": "Widgets Inc.",
        "invoice_date": "2026-01-15",
        "due_date": "2026-02-01",
        "currency": "USD",
        "line_items": [
            {"item": "WidgetA", "quantity": 10, "unit_price": 250.00},
            {"item": "WidgetB", "quantity": 5, "unit_price": 500.00},
        ],
        "subtotal": 5000.00,
        "tax": 0.00,
        "total": 5000.00,
        "payment_terms": "Net 15",
    },
    "guessed_fields": [],
}


def reading(changes: dict | None = None, **invoice_changes) -> dict:
    """The Reader's answer for INV-1001, with some fields changed."""
    answer = copy.deepcopy(INV_1001)
    answer["invoice"].update(invoice_changes)
    answer.update(changes or {})
    return answer


def reader_calls(case_file):
    """The Reader's AI calls; every Invoice is also put to the Fraud Screen."""
    return [call for call in case_file.ai_calls if call.role == AIRole.READER]


def test_a_text_invoice_read_by_the_ai_is_approved_and_paid(run, payment):
    case_file = run("invoice_1001.txt", ai=FakeAI(reader=[reading()]))

    assert case_file.outcome == Outcome.APPROVED
    assert case_file.findings == []
    assert payment.calls == [("Widgets Inc.", 5000.00)]


def test_a_reading_that_does_not_fit_the_invoice_shape_is_retried_then_proceeds(run, payment):
    unfit = reading(line_items=[{"item": "WidgetA", "quantity": "ten", "unit_price": 250.00}])

    case_file = run("invoice_1001.txt", ai=FakeAI(reader=[unfit, reading()]))

    assert case_file.outcome == Outcome.APPROVED
    first, retry = reader_calls(case_file)
    assert (first.attempt, retry.attempt) == (1, 2)
    assert any("quantity" in problem for problem in first.problems)
    assert retry.problems == []
    assert payment.calls == [("Widgets Inc.", 5000.00)]


def test_a_reading_whose_totals_do_not_add_up_is_retried_then_proceeds(run):
    misread = reading(total=5500.00)

    case_file = run("invoice_1001.txt", ai=FakeAI(reader=[misread, reading()]))

    assert case_file.outcome == Outcome.APPROVED
    first, retry = reader_calls(case_file)
    assert any("5,500.00" in problem for problem in first.problems)
    assert retry.problems == []


def test_a_reader_that_never_gives_a_valid_reading_leaves_the_invoice_held(run, payment):
    unfit = reading(line_items=[{"item": "WidgetA", "quantity": "ten", "unit_price": 250.00}])

    case_file = run("invoice_1001.txt", ai=FakeAI(reader=[unfit, unfit, unfit]))

    assert case_file.outcome == Outcome.HELD
    assert [f.kind for f in case_file.findings] == [FindingKind.UNREADABLE_DOCUMENT]
    assert [call.attempt for call in reader_calls(case_file)] == [1, 2, 3]
    assert case_file.invoice is None
    assert payment.calls == []


def test_totals_that_still_do_not_add_up_after_retries_are_held_with_the_reading(run, payment):
    """It may be a misread the AI can't fix, so a person checks it rather than it being Rejected."""
    misread = reading(total=5500.00)

    case_file = run("invoice_1001.txt", ai=FakeAI(reader=[misread, misread, misread]))

    assert case_file.outcome == Outcome.HELD
    [finding] = case_file.findings
    assert finding.kind == FindingKind.UNREADABLE_DOCUMENT
    assert any("5,500.00" in problem for problem in finding.evidence["problems"])
    assert case_file.invoice is not None and case_file.invoice.total == 5500.00
    assert len(reader_calls(case_file)) == 3
    assert payment.calls == []


INV_1012_WITH_GUESSES = {
    "invoice": {
        "invoice_number": "INV 1012",
        "vendor": "QuickShip Distributers",
        "invoice_date": "2026-01-26",
        "due_date": "2026-02-25",
        "currency": "USD",
        "line_items": [
            {"item": "Widget A", "quantity": 12, "unit_price": 250, "line_total": 3000.00},
            {"item": "WidgetB", "quantity": 7, "unit_price": 500, "line_total": 3500.00},
            {"item": "Gadget X", "quantity": 4, "unit_price": 750, "line_total": 3000.00},
        ],
        "subtotal": 9500.00,
        "tax": 475.00,
        "total": 9975.00,
        "payment_terms": "Net 30",
    },
    "guessed_fields": [
        {"field": "invoice_date", "read": "26-Jan-2O26", "chosen": "2026-01-26"},
        {"field": "line_items[1].line_total", "read": "$3,500.O0", "chosen": "3500.00"},
    ],
}


def test_guessed_fields_leave_the_invoice_held_and_show_what_was_read(run, payment):
    case_file = run("invoice_1012.txt", ai=FakeAI(reader=[INV_1012_WITH_GUESSES]))

    assert case_file.outcome == Outcome.HELD
    assert [(f.kind, f.evidence) for f in case_file.findings] == [
        (
            FindingKind.GUESSED_FIELD,
            {"field": "invoice_date", "read": "26-Jan-2O26", "chosen": "2026-01-26"},
        ),
        (
            FindingKind.GUESSED_FIELD,
            {"field": "line_items[1].line_total", "read": "$3,500.O0", "chosen": "3500.00"},
        ),
    ]
    assert payment.calls == []


def test_a_pdf_invoice_is_read_from_its_extracted_text(run, payment):
    inv_1011 = reading(
        invoice_number="INV-1011",
        vendor="Summit Manufacturing Co.",
        invoice_date="2026-01-20",
        due_date="2026-02-20",
        line_items=[
            {"item": "WidgetA", "quantity": 6, "unit_price": 250.00, "line_total": 1500.00},
            {"item": "WidgetB", "quantity": 3, "unit_price": 500.00, "line_total": 1500.00},
        ],
        subtotal=None,
        tax=None,
        total=3000.00,
        payment_terms=None,
    )
    ai = FakeAI(reader=[inv_1011])

    case_file = run("invoice_1011.pdf", ai=ai)

    assert case_file.outcome == Outcome.APPROVED
    [conversation] = [messages for role, messages in ai.conversations if role == AIRole.READER]
    assert any("Summit Manufacturing Co." in text for _, text in conversation)
    assert payment.calls == [("Summit Manufacturing Co.", 3000.00)]


def test_a_pdf_with_no_text_is_held_as_unreadable_without_asking_the_ai(run, tmp_path, payment):
    scanned = tmp_path / "scanned.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(scanned)

    case_file = run(scanned, ai=FakeAI())

    assert case_file.outcome == Outcome.HELD
    assert [f.kind for f in case_file.findings] == [FindingKind.UNREADABLE_DOCUMENT]
    assert case_file.ai_calls == []
    assert payment.calls == []


def test_when_the_ai_cannot_be_reached_the_invoice_is_held_with_the_reason(run, payment):
    down = AIUnavailableError("xAI rejected the API key (401). Check XAI_API_KEY.")

    case_file = run("invoice_1001.txt", ai=FakeAI(reader=[down]))

    assert case_file.outcome == Outcome.HELD
    [finding] = case_file.findings
    assert finding.kind == FindingKind.AI_UNAVAILABLE
    assert "xAI rejected the API key" in finding.message
    [call] = case_file.ai_calls
    assert call.error is not None and "rejected the API key" in call.error
    assert payment.calls == []


def line(item: str, quantity: float, unit_price: float, line_total: float, note=None) -> dict:
    return {
        "item": item,
        "quantity": quantity,
        "unit_price": unit_price,
        "line_total": line_total,
        "note": note,
    }


INV_1013_AS_PRINTED = {
    "invoice": {
        "invoice_number": "INV-1013",
        "vendor": "Atlas Industrial Supply",
        "invoice_date": "2026-01-24",
        "due_date": "2026-03-24",
        "currency": "USD",
        "line_items": [
            line("WidgetA", 15, 250.00, 3750.00),
            line("WidgetB", 10, 500.00, 5000.00),
            line("GadgetX", 5, 750.00, 3750.00),
            line("WidgetA", 5, 240.00, 1200.00, "Volume discount"),
            line("WidgetB", 8, 480.00, 3840.00, "Volume discount"),
            line("GadgetX", 3, 750.00, 2250.00, "Expedited"),
            line("WidgetA", 2, 250.00, 500.00, "Replacement"),
            line("GadgetX", 1, 750.00, 750.00, "Sample"),
        ],
        "subtotal": 21040.00,
        "tax": 1472.80,
        "total": 22562.80,  # as printed: 50.00 more than subtotal plus tax
        "payment_terms": "Net 60",
    },
    "guessed_fields": [],
}


def test_a_document_whose_own_totals_disagree_is_decided_on_them_not_held_as_unreadable(
    run, payment
):
    """The Reader gave the printed totals every time, so the document is wrong, not the
    reading: Validation judges it, just as it does the JSON copy of INV-1013."""
    ai = FakeAI(reader=[INV_1013_AS_PRINTED] * 3)

    case_file = run("invoice_1013.pdf", ai=ai)

    assert case_file.outcome == Outcome.REJECTED
    kinds = [f.kind for f in case_file.findings]
    assert FindingKind.UNREADABLE_DOCUMENT not in kinds
    assert FindingKind.ARITHMETIC_MISMATCH in kinds and FindingKind.SHORTFALL in kinds
    assert len(reader_calls(case_file)) == 3
    assert payment.calls == []


INV_1010 = {
    "invoice": {
        "invoice_number": "INV-1010",
        "vendor": "Consolidated Materials Group",
        "invoice_date": "2026-01-27",
        "due_date": "2026-02-26",
        "currency": "USD",
        "line_items": [
            line("WidgetA", 8, 250.00, 2000.00),
            line("WidgetB", 4, 500.00, 2000.00),
            line("GadgetX", 2, 750.00, 1500.00),
            line("WidgetA", 4, 300.00, 1200.00, "rush order"),
        ],
        "subtotal": 6700.00,
        "tax": 335.00,
        "shipping": 150.00,
        "total": 7185.00,
        "payment_terms": "Net 30",
    },
    "guessed_fields": [],
}


def test_shipping_counts_towards_the_total_and_a_line_note_stays_off_the_item_name(
    run, payment
):
    case_file = run("invoice_1010.txt", ai=FakeAI(reader=[INV_1010]))

    assert case_file.outcome == Outcome.APPROVED
    assert case_file.invoice is not None
    assert case_file.invoice.shipping == 150.00
    assert case_file.invoice.line_items[3].note == "rush order"
    assert payment.calls == [("Consolidated Materials Group", 7185.00)]


def test_a_misread_line_is_still_held_even_if_every_number_is_somewhere_in_the_document(
    run, payment
):
    """A quantity of 5 appears elsewhere on INV-1013, but 5 x 250.00 isn't the printed
    3,750.00: that's the Reader's mistake, not the document's, so a person checks it."""
    misread = copy.deepcopy(INV_1013_AS_PRINTED)
    misread["invoice"]["line_items"][0]["quantity"] = 5

    case_file = run("invoice_1013.pdf", ai=FakeAI(reader=[misread] * 3))

    assert case_file.outcome == Outcome.HELD
    assert [f.kind for f in case_file.findings] == [FindingKind.UNREADABLE_DOCUMENT]
    assert payment.calls == []
