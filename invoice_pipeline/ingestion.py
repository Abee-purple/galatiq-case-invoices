"""Ingestion: any supported Invoice file into the standard Invoice shape.

Structured files (JSON, CSV, XML) are read by code; text and PDF by the AI Reader.
Either way, the document's full text is kept for the Fraud Screen.
"""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

from .ai import AIClient
from .ai_reader import read_with_ai
from .models import Finding, FindingKind, Reading
from .readers import STRUCTURED_FORMATS, read_structured_invoice

AI_READ_FORMATS = (".pdf", ".txt")
SUPPORTED_FORMATS = tuple(sorted(STRUCTURED_FORMATS + AI_READ_FORMATS))


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_FORMATS


def unsupported_format_message(path: Path) -> str:
    kind = f"{path.suffix} files are" if path.suffix else "A file with no extension is"
    return f"{kind} not a supported Invoice format (supported: {', '.join(SUPPORTED_FORMATS)})."


def ingest(path: Path, ai: AIClient) -> Reading:
    suffix = path.suffix.lower()
    if suffix in STRUCTURED_FORMATS:
        return Reading(read_structured_invoice(path), [], [], _text_of(path))
    if suffix == ".txt":
        return read_with_ai(_text_of(path), ai)
    if suffix == ".pdf":
        return _read_pdf(path, ai)
    raise ValueError(unsupported_format_message(path))


def _read_pdf(path: Path, ai: AIClient) -> Reading:
    """The PDF library extracts the text; the AI Reader only ever sees text."""
    try:
        text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    except Exception as e:  # pypdf raises many kinds of error on a damaged file
        return _unreadable(f"The PDF could not be opened ({type(e).__name__}: {e}).")
    if not text.strip():
        return _unreadable("The PDF has no text to read; it may be a scanned image.")
    return read_with_ai(text, ai)


def _text_of(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def _unreadable(message: str) -> Reading:
    return Reading(None, [Finding(kind=FindingKind.UNREADABLE_DOCUMENT, message=message)], [])
