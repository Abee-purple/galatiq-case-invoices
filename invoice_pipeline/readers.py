"""Structured readers: JSON, CSV and XML Invoices into the standard Invoice shape, by code."""

from __future__ import annotations

import csv
import json
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree

from .models import Invoice, LineItem


def read_structured_invoice(path: Path) -> Invoice:
    return _READERS[path.suffix.lower()](path)


def _read_json(path: Path) -> Invoice:
    data = json.loads(path.read_text(encoding="utf-8"))
    vendor = data.get("vendor")
    vendor_name = vendor.get("name") if isinstance(vendor, dict) else vendor
    return Invoice(
        invoice_number=str(data["invoice_number"]),
        vendor=vendor_name or None,
        invoice_date=data.get("date"),
        due_date=data.get("due_date"),
        currency=data.get("currency") or "USD",
        line_items=[
            LineItem(
                item=line["item"],
                quantity=line["quantity"],
                unit_price=line["unit_price"],
                line_total=line.get("amount"),
                note=line.get("note"),
            )
            for line in data.get("line_items", [])
        ],
        subtotal=data.get("subtotal"),
        tax=data.get("tax_amount"),
        shipping=data.get("shipping"),
        total=data.get("total"),
        payment_terms=data.get("payment_terms") or None,
        notes=data.get("notes"),
    )


def _read_csv(path: Path) -> Invoice:
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = [row for row in csv.reader(f) if any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError(f"{path.name} is empty.")
    header = [_column_name(cell) for cell in rows[0]]
    if header[:2] == ["field", "value"]:
        return _read_field_value_csv(path, rows[1:])
    return _read_row_per_item_csv(path, header, rows[1:])


def _column_name(cell: str) -> str:
    name = cell.strip().lower().replace(" ", "_")
    return {"qty": "quantity", "amount": "line_total"}.get(name, name)


def _read_row_per_item_csv(path: Path, header: list[str], rows: list[list[str]]) -> Invoice:
    """One row per Line Item, followed by `Subtotal:` / `Tax:` / `Total:` summary rows."""
    records = [dict(zip(header, (cell.strip() for cell in row))) for row in rows]
    item_rows = [r for r in records if r.get("item")]
    first = item_rows[0] if item_rows else {}

    summary: dict[str, str] = {}
    for row in rows:
        cells = [cell.strip() for cell in row if cell.strip()]
        if len(cells) == 2 and not dict(zip(header, row)).get("item"):
            label = cells[0].lower()
            key = next(
                (k for k in ("subtotal", "tax", "shipping") if k in label), "total"
            )
            summary[key] = cells[1]

    return Invoice(
        invoice_number=_invoice_number(first.get("invoice_number"), path),
        vendor=first.get("vendor") or None,
        invoice_date=first.get("date") or None,
        due_date=first.get("due_date") or None,
        currency=first.get("currency") or "USD",
        line_items=[
            LineItem(
                item=r["item"],
                quantity=_number(r.get("quantity")),
                unit_price=_number(r.get("unit_price")),
                line_total=_number_or_none(r.get("line_total")),
            )
            for r in item_rows
        ],
        subtotal=_number_or_none(summary.get("subtotal")),
        tax=_number_or_none(summary.get("tax")),
        shipping=_number_or_none(summary.get("shipping")),
        total=_number_or_none(summary.get("total")),
    )


def _read_field_value_csv(path: Path, rows: list[list[str]]) -> Invoice:
    """One `field,value` pair per row; each `item` row starts a new Line Item."""
    fields: dict[str, str] = {}
    lines: list[dict[str, str]] = []
    for field, value, *_ in (row + ["", ""] for row in rows):
        field, value = field.strip().lower(), value.strip()
        if field == "item":
            lines.append({"item": value})
        elif field in ("quantity", "unit_price", "amount", "line_total") and lines:
            lines[-1][field] = value
        else:
            fields[field] = value
    return Invoice(
        invoice_number=_invoice_number(fields.get("invoice_number"), path),
        vendor=fields.get("vendor") or None,
        invoice_date=fields.get("date") or None,
        due_date=fields.get("due_date") or None,
        currency=fields.get("currency") or "USD",
        line_items=[
            LineItem(
                item=line["item"],
                quantity=_number(line.get("quantity")),
                unit_price=_number(line.get("unit_price")),
                line_total=_number_or_none(line.get("amount") or line.get("line_total")),
            )
            for line in lines
        ],
        subtotal=_number_or_none(fields.get("subtotal")),
        tax=_number_or_none(fields.get("tax") or fields.get("tax_amount")),
        shipping=_number_or_none(fields.get("shipping")),
        total=_number_or_none(fields.get("total")),
        payment_terms=fields.get("payment_terms") or None,
        notes=fields.get("notes") or None,
    )


def _read_xml(path: Path) -> Invoice:
    root = ElementTree.parse(path).getroot()

    def text(tag: str) -> str | None:
        # Fields may sit directly under <invoice> or inside <header> / <totals>.
        found = root.find(f".//{tag}")
        return found.text.strip() if found is not None and found.text else None

    return Invoice(
        invoice_number=_invoice_number(text("invoice_number"), path),
        vendor=text("vendor"),
        invoice_date=text("date"),
        due_date=text("due_date"),
        currency=text("currency") or "USD",
        line_items=[
            LineItem(
                item=item.findtext("name", "").strip(),
                quantity=_number(item.findtext("quantity")),
                unit_price=_number(item.findtext("unit_price")),
                line_total=_number_or_none(item.findtext("amount") or item.findtext("line_total")),
            )
            for item in root.iterfind(".//line_items/item")
        ],
        subtotal=_number_or_none(text("subtotal")),
        tax=_number_or_none(text("tax_amount") or text("tax")),
        shipping=_number_or_none(text("shipping")),
        total=_number_or_none(text("total")),
        payment_terms=text("payment_terms"),
        notes=text("notes"),
    )


def _invoice_number(value: str | None, path: Path) -> str:
    if not value or not value.strip():
        raise ValueError(f"{path.name} has no invoice number.")
    return value.strip()


def _number_or_none(text: str | None) -> float | None:
    if text is None or not text.strip():
        return None
    return float(text.replace(",", "").replace("$", "").strip())


def _number(text: str | None) -> float:
    value = _number_or_none(text)
    if value is None:
        raise ValueError("A Line Item is missing a quantity or unit price.")
    return value


_READERS: dict[str, Callable[[Path], Invoice]] = {
    ".csv": _read_csv,
    ".json": _read_json,
    ".xml": _read_xml,
}

STRUCTURED_FORMATS = tuple(sorted(_READERS))
