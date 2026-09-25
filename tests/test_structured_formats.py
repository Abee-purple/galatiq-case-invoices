"""CSV and XML Invoices are read by code into the standard Invoice shape."""

import pytest

from invoice_pipeline import FindingKind, Outcome


def test_field_value_csv_invoice_is_approved_and_paid(run, payment):
    case_file = run("invoice_1006.csv")

    assert case_file.outcome == Outcome.APPROVED
    assert case_file.findings == []
    assert case_file.invoice.invoice_number == "INV-1006"
    assert [(line.item, line.quantity) for line in case_file.invoice.line_items] == [
        ("WidgetA", 5),
        ("WidgetB", 3),
    ]
    assert payment.calls == [("Acme Industrial Supplies", 2750.00)]


def test_row_per_item_csv_invoice_is_approved_and_paid(run, payment):
    case_file = run("invoice_1015.csv")

    assert case_file.outcome == Outcome.APPROVED
    assert case_file.findings == []
    assert case_file.invoice.total == 6500.00
    assert [(line.item, line.quantity) for line in case_file.invoice.line_items] == [
        ("WidgetA", 10),
        ("WidgetB", 5),
        ("GadgetX", 2),
    ]
    assert payment.calls == [("Reliable Components Inc.", 6500.00)]


def test_row_per_item_csv_billing_more_than_stock_is_rejected(run, payment):
    case_file = run("invoice_1007.csv")

    assert case_file.outcome == Outcome.REJECTED
    assert [f.evidence["item"] for f in case_file.findings if f.kind == FindingKind.SHORTFALL] == [
        "WidgetA",
        "WidgetB",
    ]
    assert payment.calls == []


def test_xml_invoice_in_euros_is_held_for_non_usd_currency(run, payment):
    case_file = run("invoice_1014.xml")

    assert case_file.outcome == Outcome.HELD
    assert case_file.invoice.total == 4125.00
    assert len(case_file.invoice.line_items) == 2
    assert [(f.kind, f.evidence) for f in case_file.findings] == [
        (FindingKind.NON_USD_CURRENCY, {"currency": "EUR"})
    ]
    assert payment.calls == []


@pytest.mark.parametrize(
    "content, problem",
    [
        ("", "is empty"),
        ("field,value\nvendor,Acme\nitem\n", "has no invoice number"),
        ("Vendor,Item,Qty,Unit Price\nAcme,WidgetA,1,250.00\n", "has no invoice number"),
    ],
    ids=["empty", "field/value without number", "rows without number"],
)
def test_a_malformed_csv_gives_a_clear_error(run, tmp_path, content, problem):
    invoice = tmp_path / "broken.csv"
    invoice.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=f"broken.csv {problem}"):
        run(invoice)
