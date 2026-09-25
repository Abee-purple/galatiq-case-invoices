"""The Validation checks, observed through the Findings and Outcome."""

import json
from pathlib import Path

import pytest

from conftest import INVOICES
from invoice_pipeline import FindingKind, Outcome


def shortfalls(case_file) -> dict[str, float]:
    return {
        f.evidence["item"]: f.evidence["billed"]
        for f in case_file.findings
        if f.kind == FindingKind.SHORTFALL
    }


def test_quantities_are_added_up_across_line_items_before_the_stock_check(run, payment):
    case_file = run("invoice_1013.json")

    assert case_file.outcome == Outcome.REJECTED
    # No single WidgetA line exceeds its Stock of 15; together they bill 15 + 5 + 2.
    assert shortfalls(case_file) == {"WidgetA": 22, "WidgetB": 18, "GadgetX": 9}
    assert payment.calls == []


def write_invoice(tmp_path, line_items, **overrides) -> Path:
    """A copy of INV-1004 (clean, Approved) with its Line Items and fields replaced."""
    data = json.loads((INVOICES / "invoice_1004.json").read_text(encoding="utf-8"))
    data["line_items"] = line_items
    data["subtotal"] = sum(line["quantity"] * line["unit_price"] for line in line_items)
    data["tax_amount"] = 0
    data["total"] = data["subtotal"]
    data.update(overrides)
    path = tmp_path / "invoice.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_item_names_match_ignoring_spaces_case_and_dashes_and_the_clean_up_is_recorded(
    run, tmp_path
):
    invoice = write_invoice(
        tmp_path,
        [
            {"item": "Widget A", "quantity": 3, "unit_price": 250.00},
            {"item": "widget-b", "quantity": 2, "unit_price": 500.00},
            {"item": "GadgetX", "quantity": 1, "unit_price": 750.00},
        ],
    )

    case_file = run(invoice)

    assert case_file.outcome == Outcome.APPROVED
    assert case_file.findings == []
    assert [(c.billed_as, c.matched_to) for c in case_file.item_name_cleanups] == [
        ("Widget A", "WidgetA"),
        ("widget-b", "WidgetB"),
    ]


def test_differently_written_names_for_one_item_count_towards_the_same_stock(run, tmp_path):
    invoice = write_invoice(
        tmp_path,
        [
            {"item": "WidgetA", "quantity": 10, "unit_price": 250.00},
            {"item": "Widget-A", "quantity": 10, "unit_price": 250.00},
        ],
    )

    case_file = run(invoice)

    assert case_file.outcome == Outcome.REJECTED
    assert shortfalls(case_file) == {"WidgetA": 20}


def test_invoice_with_integrity_problems_is_rejected_with_every_problem_listed(run, payment):
    case_file = run("invoice_1009.json")

    assert case_file.outcome == Outcome.REJECTED
    assert {f.kind for f in case_file.findings} == {
        FindingKind.INVALID_QUANTITY,
        FindingKind.MISSING_VENDOR,
        FindingKind.ARITHMETIC_MISMATCH,
        FindingKind.MISSING_DUE_DATE,
    }
    [invalid_quantity] = [f for f in case_file.findings if f.kind == FindingKind.INVALID_QUANTITY]
    assert invalid_quantity.evidence == {"item": "WidgetA", "quantity": -5}
    assert payment.calls == []


STRUCTURED_SAMPLES = sorted(
    path.name for path in INVOICES.iterdir() if path.suffix in (".json", ".csv", ".xml")
)


@pytest.mark.parametrize("sample", STRUCTURED_SAMPLES)
def test_every_finding_has_a_kind_a_readable_message_and_evidence(run, sample):
    case_file = run(sample)

    for finding in case_file.findings:
        assert isinstance(finding.kind, FindingKind)
        assert finding.message.strip()
        assert finding.evidence


@pytest.mark.parametrize("stated_total, mismatched", [(750.01, False), (750.02, True)])
def test_totals_must_agree_to_within_a_cent(run, tmp_path, stated_total, mismatched):
    invoice = write_invoice(
        tmp_path,
        [{"item": "WidgetA", "quantity": 3, "unit_price": 250.00}],
        total=stated_total,
    )

    case_file = run(invoice)

    kinds = [f.kind for f in case_file.findings]
    assert (FindingKind.ARITHMETIC_MISMATCH in kinds) == mismatched


def test_an_unreadable_due_date_is_held(run, tmp_path, payment):
    invoice = write_invoice(
        tmp_path,
        [{"item": "WidgetA", "quantity": 3, "unit_price": 250.00}],
        due_date="yesterday",
    )

    case_file = run(invoice)

    assert case_file.outcome == Outcome.HELD
    assert [(f.kind, f.evidence) for f in case_file.findings] == [
        (FindingKind.MISSING_DUE_DATE, {"due_date": "yesterday"})
    ]
    assert payment.calls == []


def test_a_negative_quantity_cannot_hide_a_shortfall(run, tmp_path):
    invoice = write_invoice(
        tmp_path,
        [
            {"item": "WidgetA", "quantity": 20, "unit_price": 250.00},
            {"item": "WidgetA", "quantity": -10, "unit_price": 250.00},
        ],
    )

    case_file = run(invoice)

    assert case_file.outcome == Outcome.REJECTED
    assert shortfalls(case_file) == {"WidgetA": 20}
    assert FindingKind.INVALID_QUANTITY in {f.kind for f in case_file.findings}
