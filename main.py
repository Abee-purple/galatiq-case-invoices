"""Command line entry point: python main.py --invoice_path=<file or folder> [--no-ui] [--reset]"""

from __future__ import annotations

import argparse
import io
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from getpass import getpass
from pathlib import Path
from typing import Protocol

ROOT = Path(__file__).resolve().parent


def missing_dependency_message(module: str | None) -> str:
    package = (module or "unknown").partition(".")[0]
    requirements = str(ROOT / "requirements.txt")
    try:
        relative = os.path.relpath(requirements)
        if not relative.startswith(".."):
            requirements = relative
    except ValueError:  # on another drive
        pass
    if " " in requirements:
        requirements = f'"{requirements}"'
    python = Path(sys.executable).stem
    return (
        f"Missing Python package '{package}'. "
        f"Install the requirements with: {python} -m pip install -r {requirements}"
    )


try:
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.text import Text

    from invoice_pipeline import (
        AIClient,
        CaseFile,
        Outcome,
        PolicyError,
        Stage,
        invoice_files,
        load_policy,
        make_ai_client,
        process_files,
        reset_database,
    )
    from invoice_pipeline.database import (
        ensure_database,
        last_processed_id,
        normalise_invoice_number,
    )
    from invoice_pipeline.page_server import PageServerError, start_page_server
    from invoice_pipeline.settings import API_KEY_VAR, MODEL_VAR, read_setting, save_setting
except ModuleNotFoundError as e:
    if (e.name or "").startswith("invoice_pipeline"):
        raise  # our own code, not an uninstalled dependency
    sys.exit(missing_dependency_message(e.name))

DB_PATH = ROOT / "inventory.db"
POLICY_PATH = ROOT / "config" / "approval_policy.yaml"
OUTPUT_DIR = ROOT / "output"
ENV_FILE = ROOT / ".env"
RESULTS_PAGE = ROOT / "results_page.py"

OUTCOME_STYLES = {Outcome.APPROVED: "green", Outcome.HELD: "yellow", Outcome.REJECTED: "red"}


class ResultsPage(Protocol):
    url: str

    def wait(self) -> None: ...

    def stop(self) -> None: ...


class OpenResultsPage(Protocol):
    def __call__(
        self,
        *,
        db_path: Path,
        output_dir: Path,
        run_after: int,
        run_until: int,
        api_key: str | None,
    ) -> ResultsPage: ...


def open_results_page(
    *, db_path: Path, output_dir: Path, run_after: int, run_until: int, api_key: str | None
) -> ResultsPage:
    """Serve the results page for this run: history rows after `run_after`, up to `run_until`.

    The page gets this run's key so it can process Invoices with the AI too, even one
    passed with --api_key or pasted without saving. It goes in the page's environment,
    not its command line, which other programs can read.
    """
    return start_page_server(
        RESULTS_PAGE,
        [
            *("--db", str(db_path), "--output", str(output_dir)),
            *("--run-after", str(run_after), "--run-until", str(run_until)),
        ],
        log_file=output_dir / "results_page.log",
        env={API_KEY_VAR: api_key} if api_key else {},
    )


def main(
    argv: list[str] | None = None,
    *,
    db_path: Path = DB_PATH,
    output_dir: Path = OUTPUT_DIR,
    env_file: Path = ENV_FILE,
    results_page: OpenResultsPage = open_results_page,
    make_ai: Callable[[str | None, str | None], AIClient] = make_ai_client,
) -> int:
    parser = argparse.ArgumentParser(description="Process vendor Invoices.")
    parser.add_argument(
        "--invoice_path",
        type=Path,
        help="Invoice file, or a folder of them (processed in filename order)",
    )
    parser.add_argument(
        "--api_key", help=f"xAI API key (otherwise {API_KEY_VAR} from the environment or .env)"
    )
    parser.add_argument("--no-ui", action="store_true", help="Don't open the results page")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Wipe processing history and restore the seeded Stock before processing",
    )
    args = parser.parse_args(argv)
    if args.invoice_path is None and not args.reset:
        parser.error("--invoice_path is required (or use --reset on its own)")
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            # Progress symbols must survive a redirected stdout on Windows (cp1252).
            stream.reconfigure(encoding="utf-8")
    console = Console()
    errors = Console(stderr=True)

    # Check the policy before anything is wiped, so a typo can't cost the history.
    if args.invoice_path is not None:
        try:
            policy = load_policy(POLICY_PATH)
        except PolicyError as e:
            errors.print(Text(str(e), style="red"))
            return 1

    if args.reset:
        reset_database(db_path)
        console.print(f"Processing history wiped and Stock restored in {db_path}", soft_wrap=True)
    if args.invoice_path is None:
        return 0

    if not args.invoice_path.exists():
        errors.print(Text(f"No file or folder at {args.invoice_path}", style="red"))
        return 1

    api_key = resolve_api_key(args.api_key, env_file, console)
    ai = make_ai(api_key, read_setting(MODEL_VAR, env_file))

    ensure_database(db_path)
    run_after = last_processed_id(db_path)  # this run is the Invoices processed after it
    batch = TerminalBatch(console)
    process_files(
        invoice_files(args.invoice_path),
        db_path=db_path,
        policy=policy,
        output_dir=output_dir,
        ai=ai,
        progress=batch,
    )

    case_files = batch.case_files
    if case_files:
        console.print(summary_table(case_files))
    else:
        console.print("No Invoices were processed.", style="red")
    if batch.skipped_files:
        skipped = ", ".join(batch.skipped_files)
        console.print(Text(f"Skipped, not a supported format: {skipped}", style="dim"))
    for name, reason in batch.failures:
        console.print(Text(f"Not processed: {name} {reason}", style="red"))
    console.print(f"Case Files: {output_dir}", soft_wrap=True)
    console.print(f"Logs:       {output_dir / 'pipeline.log'}", soft_wrap=True)
    if not case_files:
        return 1
    if not args.no_ui:
        show_results_page(
            console,
            errors,
            results_page,
            db_path=db_path,
            output_dir=output_dir,
            run_after=run_after,
            run_until=last_processed_id(db_path),
            api_key=api_key,
        )
    return 0


def show_results_page(
    console: Console,
    errors: Console,
    results_page: OpenResultsPage,
    *,
    db_path: Path,
    output_dir: Path,
    run_after: int,
    run_until: int,
    api_key: str | None,
) -> None:
    """Open the page on this run's results and keep serving it until Ctrl+C."""
    console.print("Opening the results page…")
    try:
        page = results_page(
            db_path=db_path,
            output_dir=output_dir,
            run_after=run_after,
            run_until=run_until,
            api_key=api_key,
        )
    except KeyboardInterrupt:  # Ctrl+C while it starts; the launcher stops it
        return
    except ModuleNotFoundError as e:
        errors.print(Text(missing_dependency_message(e.name), style="red"), soft_wrap=True)
        return
    except PageServerError as e:
        errors.print(Text(str(e), style="red"), soft_wrap=True)
        return
    console.print(
        f"Results page: {page.url}  (press Ctrl+C here to stop)", style="bold", soft_wrap=True
    )
    try:
        page.wait()
    except KeyboardInterrupt:
        pass
    finally:
        page.stop()


def resolve_api_key(flag: str | None, env_file: Path, console: Console) -> str | None:
    """--api_key, then the environment or .env, then ask (offering to save it to .env)."""
    key = flag or read_setting(API_KEY_VAR, env_file)
    if key:
        return key
    without_ai = (
        "Continuing without AI: every Invoice will be Held, because none can be "
        "screened for Fraud Signals. "
        f"Set {API_KEY_VAR} in .env or pass --api_key to use the AI."
    )
    if not interactive():
        console.print(f"No xAI API key found. {without_ai}", style="yellow")
        return None
    console.print(
        "No xAI API key found. The AI needs one to read text and PDF Invoices, "
        "screen every Invoice for Fraud Signals, and give VP Reviews."
    )
    key = getpass("Paste your xAI API key (or press Enter to skip): ").strip()
    if not key:
        console.print(without_ai, style="yellow")
        return None
    if input(f"Save it to {env_file} for next time? [Y/n] ").strip().lower() in ("", "y", "yes"):
        save_setting(API_KEY_VAR, key, env_file)
        console.print(f"Saved to {env_file}", soft_wrap=True)
    return key


def interactive() -> bool:
    """Whether someone is at a terminal to answer a prompt."""
    if sys.stdin is None or not sys.stdin.isatty():
        return False
    if sys.platform == "win32":
        # The NUL device (`< NUL`) claims to be a terminal; only a real console has a mode.
        import ctypes
        import msvcrt
        from ctypes import wintypes

        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
        return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(wintypes.DWORD())))
    return True


class TerminalBatch:
    """Shows a batch in the terminal, one live line per Invoice, and keeps the results for
    the summary."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self.case_files: list[CaseFile] = []
        self.skipped_files: list[str] = []
        self.failures: list[tuple[str, str]] = []

    def skipped(self, path: Path, reason: str) -> None:
        self.console.print(Text(f"{path.name}  skipped: {reason}", style="dim"))
        self.skipped_files.append(path.name)

    @contextmanager
    def invoice(self, path: Path) -> Iterator[InvoiceLine]:
        line = InvoiceLine(path.name)
        with Live(line, console=self.console, refresh_per_second=10) as live:
            line.refresh = live.refresh
            yield line
        if not self.console.is_terminal:
            self.console.line()  # Live only ends its line itself on a terminal
        if line.case_file is not None:
            self.case_files.append(line.case_file)
        elif line.error is not None:
            self.failures.append((path.name, line.error))


class InvoiceLine:
    """One live line per Invoice: the Stages it has been through, then its Outcome."""

    def __init__(self, file_name: str) -> None:
        self.file_name = file_name
        self.stages: list[Stage] = []
        self.case_file: CaseFile | None = None
        self.error: str | None = None
        self.refresh = lambda: None

    def stage(self, stage: Stage) -> None:
        self.stages.append(stage)
        self.refresh()

    def processed(self, case_file: CaseFile) -> None:
        self.case_file = case_file

    def not_processed(self, reason: str) -> None:
        self.error = reason

    def __rich__(self) -> Text:
        line = Text(f"{self.file_name}  ", style="bold")
        for i, stage in enumerate(self.stages):
            if i:
                line.append(" → ", style="dim")
            if i < len(self.stages) - 1 or self.case_file is not None:
                line.append(f"{stage.value} ✓")
            elif self.error is not None:
                line.append(f"{stage.value} ✗", style="red")
            else:
                line.append(f"{stage.value} …")
        line.append("  ")
        if self.case_file is not None:
            line.append_text(styled_outcome(self.case_file.outcome))
        elif self.error is not None:
            line.append(self.error, style="red")
        return line


def styled_outcome(outcome: Outcome) -> Text:
    return Text(outcome.value, style=f"bold {OUTCOME_STYLES[outcome]}")


def summary_table(case_files: list[CaseFile]) -> Table:
    table = Table(title="Summary")
    table.add_column("Invoice", no_wrap=True)
    table.add_column("Vendor")
    table.add_column("Amount", justify="right", no_wrap=True)
    table.add_column("Outcome", no_wrap=True)
    table.add_column("Main reason", overflow="fold")
    for case_file in case_files:
        invoice = case_file.invoice
        if invoice is None:  # nothing could be read, so name the file instead
            number, vendor, amount = Path(case_file.source_file).name, "-", "-"
        else:
            number = normalise_invoice_number(invoice.invoice_number)
            vendor = invoice.vendor or "-"
            amount = "-" if invoice.total is None else f"{invoice.total:,.2f} {invoice.currency}"
        table.add_row(
            Text(number),
            Text(vendor),
            amount,
            styled_outcome(case_file.outcome),
            Text(case_file.main_reason),
        )
    return table


if __name__ == "__main__":
    sys.exit(main())
