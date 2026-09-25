"""The local database: Stock seeded from the brief, and the record of processed Invoices."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .models import CaseFile, Invoice, Outcome, ProcessedInvoice

SEED_STOCK = {"WidgetA": 15, "WidgetB": 10, "GadgetX": 5, "FakeItem": 0}


def ensure_database(db_path: Path) -> None:
    """Create and seed the database on first use."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS inventory (item TEXT PRIMARY KEY, stock INTEGER)")
        conn.executemany(
            "INSERT OR IGNORE INTO inventory (item, stock) VALUES (?, ?)", SEED_STOCK.items()
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS processed_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_number TEXT NOT NULL,
                vendor TEXT NOT NULL,
                amount REAL,
                currency TEXT NOT NULL,
                outcome TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                case_file TEXT NOT NULL
            )"""
        )


def reset_database(db_path: Path) -> None:
    """Wipe processing history and restore the seeded Stock."""
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("DROP TABLE IF EXISTS inventory")
        conn.execute("DROP TABLE IF EXISTS processed_invoices")
    ensure_database(db_path)


def load_stock(db_path: Path) -> dict[str, int]:
    """Stock is read-only during processing. Approving an Invoice doesn't reduce it, so
    each Invoice is checked against the full Stock, whatever order they come in."""
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute("SELECT item, stock FROM inventory").fetchall()
    return {item: stock for item, stock in rows}


def record_processed(db_path: Path, case_file: CaseFile, case_file_name: str) -> None:
    invoice = case_file.invoice
    if invoice is None:  # nothing could be read: recorded, but never matches as a Duplicate
        number, vendor, amount, currency = "", "", None, ""
    else:
        number = normalise_invoice_number(invoice.invoice_number)
        vendor = normalise_vendor(invoice.vendor)
        amount, currency = invoice.total, invoice.currency
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            """INSERT INTO processed_invoices
               (invoice_number, vendor, amount, currency, outcome, processed_at, case_file)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                number,
                vendor,
                amount,
                currency,
                case_file.outcome.value,
                case_file.processed_at.isoformat(),
                case_file_name,
            ),
        )


def find_approved(db_path: Path, invoice: Invoice) -> list[ProcessedInvoice]:
    """Earlier Approved Invoices with the same invoice number and Vendor, oldest first."""
    number = normalise_invoice_number(invoice.invoice_number)
    vendor = normalise_vendor(invoice.vendor)
    if number == "INV-" or not vendor:
        # Without both, there is nothing to match on.
        return []
    return _processed_invoices(
        db_path,
        "invoice_number = ? AND vendor = ? AND outcome = ?",
        (number, vendor, Outcome.APPROVED.value),
    )


def vendor_history(db_path: Path, vendor: str | None) -> list[ProcessedInvoice]:
    """Every earlier Invoice from the Vendor, whatever its Outcome, oldest first."""
    normalised = normalise_vendor(vendor)
    if not normalised:
        return []
    return _processed_invoices(db_path, "vendor = ?", (normalised,))


def processing_history(
    db_path: Path, *, after: int = 0, until: int | None = None
) -> list[ProcessedInvoice]:
    """The Invoices processed after history row `after`, up to and including row `until`
    (all of them by default), oldest first."""
    if until is None:
        return _processed_invoices(db_path, "id > ?", (after,))
    return _processed_invoices(db_path, "id > ? AND id <= ?", (after, until))


def last_processed_id(db_path: Path) -> int:
    """The newest history row, so a run can later ask for the Invoices processed after it."""
    with closing(sqlite3.connect(db_path)) as conn:
        (last,) = conn.execute("SELECT MAX(id) FROM processed_invoices").fetchone()
    return last or 0


def _processed_invoices(
    db_path: Path, where: str, params: tuple[str | int, ...]
) -> list[ProcessedInvoice]:
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            f"""SELECT invoice_number, vendor, amount, currency, outcome, processed_at, case_file
               FROM processed_invoices WHERE {where} ORDER BY id""",
            params,
        ).fetchall()
    return [
        ProcessedInvoice(
            invoice_number=number,
            vendor=vendor,
            amount=amount,
            currency=currency,
            outcome=Outcome(outcome),
            processed_at=datetime.fromisoformat(processed_at),
            case_file=case_file,
        )
        for number, vendor, amount, currency, outcome, processed_at, case_file in rows
    ]


def normalise_invoice_number(number: str) -> str:
    """'INV 1012', 'inv-1012' and '1012' all become 'INV-1012'."""
    digits = re.sub(r"[^A-Z0-9]", "", number.upper()).removeprefix("INV")
    return f"INV-{digits}"


def normalise_vendor(vendor: str | None) -> str:
    """'Precision Parts Ltd.' and 'precision parts ltd' compare equal."""
    return " ".join(re.sub(r"[^a-z0-9]", " ", (vendor or "").lower()).split())
