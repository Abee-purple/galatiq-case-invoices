"""Case Files: one JSON record per processed Invoice."""

from __future__ import annotations

import itertools
import re
from pathlib import Path

from .models import CaseFile


def write_case_file(case_file: CaseFile, output_dir: Path) -> Path:
    """Write a new Case File; an existing one is never overwritten."""
    output_dir.mkdir(parents=True, exist_ok=True)
    invoice = case_file.invoice
    name = invoice.invoice_number if invoice is not None else Path(case_file.source_file).stem
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", name)
    stem = f"{safe_name}_{case_file.processed_at:%Y%m%dT%H%M%S}"
    content = case_file.model_dump_json(indent=2)
    for n in itertools.count(1):
        path = output_dir / (f"{stem}.json" if n == 1 else f"{stem}_{n}.json")
        try:
            with path.open("x", encoding="utf-8") as f:
                f.write(content)
            return path
        except FileExistsError:
            continue
    raise AssertionError("unreachable")
