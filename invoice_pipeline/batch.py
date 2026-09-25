"""A batch of Invoice files, in filename order: the one loop the CLI and the results page share."""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from .ai import AIClient
from .ingestion import is_supported, unsupported_format_message
from .logs import log_event, log_to_file
from .models import CaseFile
from .pipeline import Stage, process_invoice
from .policy import ApprovalPolicy

log = logging.getLogger(__name__)


class InvoiceProgress(Protocol):
    """Told how one Invoice gets on: each Stage as it starts, then how it ended."""

    def stage(self, stage: Stage) -> None: ...

    def processed(self, case_file: CaseFile) -> None: ...

    def not_processed(self, reason: str) -> None: ...


class BatchProgress(Protocol):
    """How a front end shows a batch as it runs."""

    def skipped(self, path: Path, reason: str) -> None: ...

    def invoice(self, path: Path) -> AbstractContextManager[InvoiceProgress]:
        """Entered before the Invoice is processed and left afterwards, even if stopped."""
        ...


def invoice_files(path: Path) -> list[Path]:
    """A single file, or every file in a folder in filename order (hidden files ignored)."""
    if path.is_dir():
        files = (p for p in path.iterdir() if p.is_file() and not p.name.startswith("."))
        return sorted(files, key=lambda p: p.name)
    return [path]


def process_files(
    paths: list[Path],
    *,
    db_path: Path,
    policy: ApprovalPolicy,
    output_dir: Path,
    ai: AIClient,
    progress: BatchProgress,
) -> None:
    """Process each file in turn, logging to the output folder's pipeline.log.

    Unsupported files are skipped, and a file that fails doesn't stop the rest.
    """
    with log_to_file(output_dir / "pipeline.log"):
        for path in paths:
            if not is_supported(path):
                reason = unsupported_format_message(path)
                log_event(log, "file skipped", invoice_file=str(path), reason=reason)
                progress.skipped(path, reason)
                continue
            with progress.invoice(path) as invoice:
                stages: list[Stage] = []

                def on_stage(stage: Stage) -> None:
                    stages.append(stage)
                    invoice.stage(stage)

                try:
                    case_file = process_invoice(
                        path,
                        db_path=db_path,
                        policy=policy,
                        output_dir=output_dir,
                        ai=ai,
                        on_stage=on_stage,
                    )
                except Exception as e:  # one bad file must not stop the batch
                    invoice.not_processed(error_reason(stages, e))
                else:
                    invoice.processed(case_file)


def error_reason(stages: list[Stage], error: Exception) -> str:
    """Why an Invoice that raised has no Case File, given the Stages it had started."""
    detail = str(error) if isinstance(error, ValueError) else f"{type(error).__name__}: {error}"
    if not stages or stages[-1] == Stage.INGESTION:
        return f"could not be read: {detail}"
    return f"error during {stages[-1].value}: {detail}"
