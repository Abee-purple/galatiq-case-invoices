"""Duplicates are found in the processing history, which --reset wipes."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from conftest import INVOICES
from invoice_pipeline import FindingKind, Outcome, reset_database


def test_a_revised_version_of_an_approved_invoice_is_held_as_a_duplicate(run, tmp_path, payment):
    first = run("invoice_1004.json")
    [first_case_file] = (tmp_path / "output").glob("*.json")

    second = run("invoice_1004_revised.json")

    assert first.outcome == Outcome.APPROVED
    assert second.outcome == Outcome.HELD
    [duplicate] = second.findings
    assert duplicate.kind == FindingKind.DUPLICATE
    assert duplicate.evidence["invoice_number"] == "INV-1004"
    assert duplicate.evidence["outcome"] == "Approved"
    assert duplicate.evidence["case_file"] == first_case_file.name
    assert payment.calls == [("Precision Parts Ltd.", 1890.00)]


def variant_of(tmp_path, sample: str, name: str, **changes) -> Path:
    data = json.loads((INVOICES / sample).read_text(encoding="utf-8"))
    data.update(changes)
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "original, earlier_outcome",
    [
        # INV-1016 bills an Unknown Item (WidgetC).
        ("invoice_1016.json", Outcome.REJECTED),
        # INV-1014 is in EUR.
        ("invoice_1014.xml", Outcome.HELD),
    ],
)
def test_an_invoice_sent_again_after_being_rejected_or_held_is_processed_normally(
    run, tmp_path, original, earlier_outcome
):
    first = run(original)
    corrected = variant_of(
        tmp_path,
        "invoice_1004.json",
        "corrected.json",
        invoice_number=first.invoice.invoice_number,
        vendor={"name": first.invoice.vendor},
    )

    sent_again = run(corrected)

    assert first.outcome == earlier_outcome
    assert sent_again.outcome == Outcome.APPROVED
    assert sent_again.findings == []


@pytest.mark.parametrize(
    "invoice_number, vendor",
    [
        ("INV 1004", "Precision Parts Ltd."),
        ("1004", "Precision Parts Ltd."),
        ("inv-1004", "PRECISION PARTS LTD"),
    ],
)
def test_invoice_numbers_and_vendors_are_normalised_when_finding_duplicates(
    run, tmp_path, invoice_number, vendor
):
    run("invoice_1004.json")
    resent = variant_of(
        tmp_path,
        "invoice_1004.json",
        "resent.json",
        invoice_number=invoice_number,
        vendor={"name": vendor},
    )

    case_file = run(resent)

    assert case_file.outcome == Outcome.HELD
    assert [f.kind for f in case_file.findings] == [FindingKind.DUPLICATE]


def test_a_different_vendor_with_the_same_invoice_number_is_not_a_duplicate(run, tmp_path):
    run("invoice_1004.json")
    other_vendor = variant_of(
        tmp_path, "invoice_1004.json", "other.json", vendor={"name": "Other Parts Co."}
    )

    assert run(other_vendor).outcome == Outcome.APPROVED


def test_repeated_runs_never_overwrite_a_case_file(run, tmp_path):
    for _ in range(5):
        run("invoice_1004.json")

    assert len(list((tmp_path / "output").glob("*.json"))) == 5


def test_reset_wipes_history_and_restores_seeded_stock(run, tmp_path):
    db_path = tmp_path / "inventory.db"
    run("invoice_1004.json")
    with closing(sqlite3.connect(db_path)) as conn, conn:
        # Arrange: Stock drifted away from the brief's seed (WidgetA 15).
        conn.execute("UPDATE inventory SET stock = 0 WHERE item = 'WidgetA'")

    reset_database(db_path)
    case_file = run("invoice_1004.json")

    assert case_file.outcome == Outcome.APPROVED
    assert case_file.findings == []


def test_invoices_without_a_number_are_never_duplicates_of_each_other(run, tmp_path):
    unnumbered = variant_of(tmp_path, "invoice_1004.json", "unnumbered.json", invoice_number="")
    run(unnumbered)

    assert run(unnumbered).findings == []
