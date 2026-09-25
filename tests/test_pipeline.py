"""One JSON Invoice, end to end."""

import json

import pytest

from conftest import INVOICES
from invoice_pipeline import FindingKind, Outcome


def test_clean_json_invoice_is_approved_and_paid(run, payment):
    case_file = run("invoice_1004.json")

    assert case_file.outcome == Outcome.APPROVED
    assert case_file.findings == []
    assert payment.calls == [("Precision Parts Ltd.", 1890.00)]
    assert case_file.payment_result == {"status": "success"}


def test_invoice_with_unknown_item_is_rejected_and_not_paid(run, payment):
    case_file = run("invoice_1016.json")

    assert case_file.outcome == Outcome.REJECTED
    assert [(f.kind, f.evidence["item"]) for f in case_file.findings] == [
        (FindingKind.UNKNOWN_ITEM, "WidgetC")
    ]
    assert payment.calls == []
    assert case_file.payment_result is None


def test_case_file_is_written_to_the_output_folder(run, tmp_path):
    run("invoice_1004.json")

    [saved] = (tmp_path / "output").glob("*.json")
    record = json.loads(saved.read_text(encoding="utf-8"))
    assert record["source_file"].endswith("invoice_1004.json")
    assert record["invoice"]["invoice_number"] == "INV-1004"
    assert record["invoice"]["total"] == 1890.00
    assert record["findings"] == []
    assert record["outcome"] == "Approved"
    assert record["reasons"]
    assert record["payment_result"] == {"status": "success"}


@pytest.mark.parametrize(
    "missing, finding",
    [("total", FindingKind.MISSING_TOTAL), ("vendor", FindingKind.MISSING_VENDOR)],
)
def test_invoice_missing_vendor_or_total_is_rejected_and_never_paid(
    run, tmp_path, payment, missing, finding
):
    data = json.loads((INVOICES / "invoice_1004.json").read_text(encoding="utf-8"))
    data[missing] = None
    invoice_file = tmp_path / "invoice.json"
    invoice_file.write_text(json.dumps(data), encoding="utf-8")

    case_file = run(invoice_file)

    assert case_file.outcome == Outcome.REJECTED
    assert [f.kind for f in case_file.findings] == [finding]
    assert payment.calls == []


@pytest.mark.parametrize(
    "invoice, stages",
    [
        ("invoice_1004.json", ["Ingestion", "Fraud Screen", "Validation", "Approval", "Payment"]),
        ("invoice_1016.json", ["Ingestion", "Fraud Screen", "Validation", "Approval"]),
    ],
    ids=["approved", "rejected"],
)
def test_each_stage_is_reported_as_it_starts(run, invoice, stages):
    reported: list[str] = []

    run(invoice, on_stage=reported.append)

    assert reported == stages
