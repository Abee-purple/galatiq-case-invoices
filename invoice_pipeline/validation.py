"""Validation: pure code checks that turn an Invoice into Findings."""

from __future__ import annotations

import re
from datetime import datetime
from typing import NamedTuple

from .models import Finding, FindingKind, Invoice, ItemNameCleanup, ProcessedInvoice


class ValidationResult(NamedTuple):
    findings: list[Finding]
    item_name_cleanups: list[ItemNameCleanup]


CENT = 0.01
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y")


def validate(
    invoice: Invoice, stock: dict[str, int], approved_before: list[ProcessedInvoice]
) -> ValidationResult:
    """`approved_before` holds earlier Approved Invoices with this number and Vendor."""
    stock_findings, cleanups = _check_stock(invoice, stock)
    findings = [
        *_check_required_fields(invoice),
        *_check_quantities(invoice),
        *stock_findings,
        *check_arithmetic(invoice),
        *_check_currency(invoice),
        *_check_duplicate(approved_before),
    ]
    return ValidationResult(findings, cleanups)


def _check_required_fields(invoice: Invoice) -> list[Finding]:
    findings: list[Finding] = []
    if not (invoice.vendor or "").strip():
        findings.append(
            Finding(
                kind=FindingKind.MISSING_VENDOR,
                message="Invoice has no Vendor name, so there is no one to pay.",
                evidence={"vendor": invoice.vendor},
            )
        )
    if invoice.total is None:
        findings.append(
            Finding(
                kind=FindingKind.MISSING_TOTAL,
                message="Invoice has no total, so there is no amount to pay.",
                evidence={"total": None},
            )
        )
    if not invoice.due_date or not _is_date(invoice.due_date):
        findings.append(
            Finding(
                kind=FindingKind.MISSING_DUE_DATE,
                message=(
                    f"Due date '{invoice.due_date}' could not be read as a date."
                    if invoice.due_date
                    else "Invoice has no due date."
                ),
                evidence={"due_date": invoice.due_date},
            )
        )
    return findings


def _is_date(text: str) -> bool:
    for fmt in DATE_FORMATS:
        try:
            datetime.strptime(text.strip(), fmt)
            return True
        except ValueError:
            pass
    return False


def _check_quantities(invoice: Invoice) -> list[Finding]:
    return [
        Finding(
            kind=FindingKind.INVALID_QUANTITY,
            message=f"{line.item} has a quantity of {line.quantity:g}; quantities must be positive.",
            evidence={"item": line.item, "quantity": line.quantity},
        )
        for line in invoice.line_items
        if line.quantity <= 0
    ]


def check_arithmetic(invoice: Invoice) -> list[Finding]:
    """Line totals, subtotal, tax, shipping and total must agree to within a cent."""
    findings: list[Finding] = []

    def mismatch(message: str, stated: float, expected: float, **evidence: object) -> None:
        findings.append(
            Finding(
                kind=FindingKind.ARITHMETIC_MISMATCH,
                message=message,
                evidence={**evidence, "stated": stated, "expected": round(expected, 2)},
            )
        )

    lines_sum = 0.0
    for line in invoice.line_items:
        expected = line.quantity * line.unit_price
        if line.line_total is not None and _differs(line.line_total, expected):
            mismatch(
                f"{line.item}: {line.quantity:g} x {line.unit_price:,.2f} is {expected:,.2f}, "
                f"but the line total says {line.line_total:,.2f}.",
                line.line_total,
                expected,
                item=line.item,
            )
        lines_sum += line.line_total if line.line_total is not None else expected

    if invoice.subtotal is not None and _differs(invoice.subtotal, lines_sum):
        mismatch(
            f"Line Items add up to {lines_sum:,.2f}, but the subtotal says {invoice.subtotal:,.2f}.",
            invoice.subtotal,
            lines_sum,
            field="subtotal",
        )

    if invoice.total is not None:
        base = invoice.subtotal if invoice.subtotal is not None else lines_sum
        expected_total = base + (invoice.tax or 0) + (invoice.shipping or 0)
        if _differs(invoice.total, expected_total):
            added = "tax and shipping" if invoice.shipping else "tax"
            mismatch(
                f"Subtotal plus {added} is {expected_total:,.2f}, "
                f"but the total says {invoice.total:,.2f}.",
                invoice.total,
                expected_total,
                field="total",
            )
    return findings


def _differs(stated: float, expected: float) -> bool:
    return abs(stated - expected) > CENT + 1e-9


def _name_key(name: str) -> str:
    return re.sub(r"[\s\-]", "", name).lower()


def inventory_item(name: str, stock: dict[str, int]) -> str | None:
    """The inventory item a billed name matches, ignoring spaces, case and dashes."""
    return _inventory_names(stock).get(_name_key(name))


def _inventory_names(stock: dict[str, int]) -> dict[str, str]:
    return {_name_key(item): item for item in stock}


def _check_stock(
    invoice: Invoice, stock: dict[str, int]
) -> tuple[list[Finding], list[ItemNameCleanup]]:
    """Stock is checked per item, so splitting an item across lines can't hide a Shortfall."""
    inventory_names = _inventory_names(stock)
    billed: dict[str, float] = {}
    cleanups: list[ItemNameCleanup] = []
    for line in invoice.line_items:
        item = inventory_names.get(_name_key(line.item), line.item)
        if item != line.item and all(c.billed_as != line.item for c in cleanups):
            cleanups.append(ItemNameCleanup(billed_as=line.item, matched_to=item))
        # Zero or negative quantities get their own Finding and must not offset real ones.
        billed[item] = billed.get(item, 0) + max(line.quantity, 0)

    findings: list[Finding] = []
    for item, quantity in billed.items():
        if item not in stock:
            findings.append(
                Finding(
                    kind=FindingKind.UNKNOWN_ITEM,
                    message=f"{item} is not in the inventory database.",
                    evidence={"item": item},
                )
            )
        elif quantity > stock[item]:
            findings.append(
                Finding(
                    kind=FindingKind.SHORTFALL,
                    message=(
                        f"Invoice bills {quantity:g} x {item} in total "
                        f"but only {stock[item]} are in Stock."
                    ),
                    evidence={"item": item, "billed": quantity, "stock": stock[item]},
                )
            )
    return findings, cleanups


def _check_currency(invoice: Invoice) -> list[Finding]:
    if invoice.currency.upper() == "USD":
        return []
    return [
        Finding(
            kind=FindingKind.NON_USD_CURRENCY,
            message=f"Invoice is in {invoice.currency}, not USD; it cannot be paid at face value.",
            evidence={"currency": invoice.currency},
        )
    ]


def _check_duplicate(approved_before: list[ProcessedInvoice]) -> list[Finding]:
    if not approved_before:
        return []
    original = approved_before[0]
    return [
        Finding(
            kind=FindingKind.DUPLICATE,
            message=(
                f"{original.invoice_number} from this Vendor was already Approved "
                f"on {original.processed_at:%Y-%m-%d %H:%M} (Case File {original.case_file})."
            ),
            evidence={
                "invoice_number": original.invoice_number,
                "outcome": original.outcome.value,
                "processed_at": original.processed_at.isoformat(),
                "case_file": original.case_file,
            },
        )
    ]
