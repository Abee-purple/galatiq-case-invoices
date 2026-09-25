"""The results page: this run's Invoices, the processing history, and each Case File.

It has no pipeline logic. It reads Case Files and the processing history, and its Process
panel hands files to the same pipeline entry point as the CLI. Its Approval Policy editor
saves to the same policy file the CLI reads. `python main.py` starts it after a run. To open
it by hand:

    streamlit run results_page.py -- --db inventory.db --output output [--run-after N --run-until M]
"""

from __future__ import annotations

import argparse
import itertools
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import pandas as pd
import streamlit as st
from streamlit.delta_generator import DeltaGenerator

from invoice_pipeline import (
    PolicyError,
    Stage,
    invoice_files,
    is_supported,
    load_policy,
    make_ai_client,
    process_files,
    save_policy,
)
from invoice_pipeline.database import (
    ensure_database,
    last_processed_id,
    normalise_invoice_number,
    processing_history,
)
from invoice_pipeline.ingestion import SUPPORTED_FORMATS
from invoice_pipeline.models import (
    AIRole,
    CaseFile,
    Finding,
    FindingKind,
    Invoice,
    Outcome,
    ProcessedInvoice,
    ReviewRound,
)
from invoice_pipeline.settings import API_KEY_VAR, MODEL_VAR, read_setting

ROOT = Path(__file__).resolve().parent
SAMPLES = ROOT / "data" / "invoices"
POLICY_PATH = ROOT / "config" / "approval_policy.yaml"
ENV_FILE = ROOT / ".env"

BadgeColour = Literal["green", "orange", "red"]
OUTCOME_COLOURS: dict[Outcome, BadgeColour] = {
    Outcome.APPROVED: "green",
    Outcome.HELD: "orange",
    Outcome.REJECTED: "red",
}
# Translucent, so the table stays readable in both light and dark themes.
OUTCOME_BACKGROUNDS = {
    Outcome.APPROVED.value: "rgba(33, 195, 84, 0.22)",
    Outcome.HELD.value: "rgba(255, 170, 0, 0.28)",
    Outcome.REJECTED.value: "rgba(255, 75, 75, 0.25)",
}
FINDING_LABELS = {
    FindingKind.SHORTFALL: "Shortfall",
    FindingKind.UNKNOWN_ITEM: "Unknown Item",
    FindingKind.INVALID_QUANTITY: "Invalid quantity",
    FindingKind.ARITHMETIC_MISMATCH: "Arithmetic mismatch",
    FindingKind.MISSING_VENDOR: "Missing Vendor",
    FindingKind.MISSING_TOTAL: "Missing total",
    FindingKind.MISSING_DUE_DATE: "Missing due date",
    FindingKind.NON_USD_CURRENCY: "Non-USD currency",
    FindingKind.DUPLICATE: "Duplicate",
    FindingKind.UNREADABLE_DOCUMENT: "Unreadable document",
    FindingKind.GUESSED_FIELD: "Guessed Field",
    FindingKind.AI_UNAVAILABLE: "AI unavailable",
    FindingKind.FRAUD_SIGNAL: "Fraud Signal",
}
# Shown in their own sections of the Case File, not under Findings.
OWN_SECTION = {FindingKind.GUESSED_FIELD, FindingKind.FRAUD_SIGNAL}


def page_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "inventory.db")
    parser.add_argument("--output", type=Path, default=ROOT / "output")
    parser.add_argument(
        "--run-after", type=int, help="This run is the Invoices processed after this history row"
    )
    parser.add_argument("--run-until", type=int, help="... up to and including this one")
    args, _ = parser.parse_known_args(sys.argv[1:])
    return args


@dataclass(frozen=True)
class Run:
    """A batch of Invoices: the history rows after `after`, up to and including `until`."""

    after: int
    until: int | None  # None: every row since
    source: str  # where it was started, for the caption


@dataclass(frozen=True)
class InvoiceResult:
    """One processed Invoice: its processing history record, and its Case File if it is there."""

    processed: ProcessedInvoice
    case_file: CaseFile | None

    @property
    def invoice(self) -> Invoice | None:
        return self.case_file.invoice if self.case_file else None

    @property
    def label(self) -> str:
        if self.invoice is not None:
            return normalise_invoice_number(self.invoice.invoice_number)
        if self.case_file is not None:  # nothing could be read, so name the file instead
            return Path(self.case_file.source_file).name
        return self.processed.invoice_number or self.processed.case_file

    @property
    def vendor(self) -> str:
        return (self.invoice.vendor if self.invoice else None) or "—"

    @property
    def amount(self) -> str:
        if self.invoice is None or self.invoice.total is None:
            return "—"
        return f"{self.invoice.total:,.2f} {self.invoice.currency}"

    @property
    def main_reason(self) -> str:
        if self.case_file is None:
            return f"Case File {self.processed.case_file} is missing from the output folder."
        return self.case_file.main_reason

    @property
    def source_file(self) -> str:
        return Path(self.case_file.source_file).name if self.case_file else "—"


@st.cache_data(show_spinner=False)
def _read_case_file(path: str) -> CaseFile:
    """Case Files are never overwritten, so each is read once. A failed read raises, and
    Streamlit doesn't cache that, so it is tried again next time."""
    return CaseFile.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_case_file(path: str) -> CaseFile | None:
    try:
        return _read_case_file(path)
    except (OSError, ValueError):
        return None


def load_rows(db_path: Path, output_dir: Path, run: Run | None = None) -> list[InvoiceResult]:
    """Every processed Invoice, or those of `run`, oldest first."""
    if not db_path.exists():
        return []
    after, until = (run.after, run.until) if run else (0, None)
    return [
        InvoiceResult(processed, load_case_file(str(output_dir / processed.case_file)))
        for processed in processing_history(db_path, after=after, until=until)
    ]


def invoices(count: int) -> str:
    return f"{count} Invoice{'' if count == 1 else 's'}"


def md(text: str) -> str:
    """Escape text from an Invoice or the AI so Markdown shows it as written ($ is maths)."""
    return re.sub(r"([\\`*_{}\[\]()<>#+\-.!|$~])", r"\\\1", text)


def outcome_badge(outcome: Outcome) -> None:
    st.badge(outcome.value, color=OUTCOME_COLOURS[outcome])


def invoice_table(rows: list[InvoiceResult], key: str) -> list[InvoiceResult]:
    """The Invoices with colour-coded Outcomes; returns those selected."""
    frame = pd.DataFrame(
        {
            "Invoice": [row.label for row in rows],
            "Vendor": [row.vendor for row in rows],
            "Amount": [row.amount for row in rows],
            "Outcome": [row.processed.outcome.value for row in rows],
            "Main reason": [row.main_reason for row in rows],
            "Processed": [f"{row.processed.processed_at:%Y-%m-%d %H:%M:%S}" for row in rows],
            "File": [row.source_file for row in rows],
        }
    )
    styled = frame.style.map(
        lambda outcome: f"background-color: {OUTCOME_BACKGROUNDS[outcome]}", subset=["Outcome"]
    )
    event = st.dataframe(
        styled,
        key=key,
        hide_index=True,
        on_select="rerun",
        selection_mode="multi-row",
        column_config={"Main reason": st.column_config.TextColumn(width="large")},
    )
    selected = [rows[i] for i in event.selection.rows]
    if not selected and len(rows) == 1:
        return rows  # a lone Invoice is selected without a click
    return selected


def show_selected(selected: list[InvoiceResult], key: str) -> None:
    """Offer to process the selected Invoices again, and open the Case File of a single one."""
    if not selected:
        return
    process_again_button(selected, key=f"again-{key}")
    if len(selected) == 1:
        show_case_file(selected[0])


def outcome_counts(rows: list[InvoiceResult]) -> None:
    with st.container(horizontal=True):
        for outcome in Outcome:
            count = sum(row.processed.outcome is outcome for row in rows)
            st.badge(f"{count} {outcome.value}", color=OUTCOME_COLOURS[outcome])


def show_case_file(row: InvoiceResult) -> None:
    st.divider()
    st.subheader(f"Case File: {md(row.label)}")
    case_file = row.case_file
    if case_file is None:
        st.error(row.main_reason)
        return
    with st.container(horizontal=True, vertical_alignment="center"):
        outcome_badge(case_file.outcome)
        st.caption(
            f"{md(row.source_file)} · processed {case_file.processed_at:%Y-%m-%d %H:%M:%S} · "
            f"saved as {md(row.processed.case_file)}"
        )
    st.markdown("\n".join(f"- {md(reason)}" for reason in case_file.reasons))

    st.markdown("#### Invoice")
    show_invoice(case_file.invoice)

    st.markdown("#### Findings")
    show_findings([f for f in case_file.findings if f.kind not in OWN_SECTION])
    if case_file.item_name_cleanups:
        st.caption(
            "Item names matched after ignoring spaces, capitals and dashes: "
            + "; ".join(
                f"“{md(c.billed_as)}” → {md(c.matched_to)}" for c in case_file.item_name_cleanups
            )
        )

    st.markdown("#### Guessed Fields")
    show_guessed_fields([f for f in case_file.findings if f.kind is FindingKind.GUESSED_FIELD])

    st.markdown("#### Fraud Signals")
    show_fraud_signals(case_file)

    st.markdown("#### VP Review and Critique")
    show_review_rounds(case_file.review_rounds)

    st.markdown("#### Payment")
    if case_file.payment_result is None:
        st.markdown("Not paid: only Approved Invoices are paid.")
    else:
        details = ", ".join(f"{k}: {v}" for k, v in case_file.payment_result.items())
        st.markdown(f"Payment made. {md(details)}")

    show_ai_calls(case_file)


def source_path(case_file: CaseFile) -> Path | None:
    """The file the Case File was made from, if it is still there."""
    path = Path(case_file.source_file)
    return next((p for p in (path, ROOT / path) if p.is_file()), None)


def process_again_button(rows: list[InvoiceResult], key: str) -> None:
    """Run the Invoices' files through the pipeline again, e.g. after editing the policy."""
    sources = [source_path(row.case_file) if row.case_file else None for row in rows]
    found = list(dict.fromkeys(source for source in sources if source is not None))
    missing = len(sources) - sum(source is not None for source in sources)
    help_text = (
        "With the current Approval Policy. A copy of an Invoice that was already Approved "
        "is a Duplicate."
    )
    if missing:
        help_text += f" {invoices(missing)} can't be: the original file is gone."
    st.button(
        "Process again" if len(rows) == 1 else f"Process {invoices(len(rows))} again",
        key=key,
        disabled=not found,
        on_click=st.session_state.update,
        kwargs={"process_again": found},
        help=help_text,
    )


def show_invoice(invoice: Invoice | None) -> None:
    if invoice is None:
        st.warning("No Invoice could be read from this file.")
        return
    details, amounts = st.columns(2)
    details.markdown(
        "\n".join(
            f"- **{name}:** {md(value) if value else '—'}"
            for name, value in [
                ("Invoice number", invoice.invoice_number),
                ("Vendor", invoice.vendor),
                ("Invoice date", invoice.invoice_date),
                ("Due date", invoice.due_date),
                ("Payment terms", invoice.payment_terms),
            ]
        )
    )
    amounts.markdown(
        "\n".join(
            f"- **{name}:** {'—' if value is None else md(f'{value:,.2f} {invoice.currency}')}"
            for name, value in [
                ("Subtotal", invoice.subtotal),
                ("Tax", invoice.tax),
                ("Total", invoice.total),
            ]
        )
    )
    if invoice.line_items:
        st.dataframe(
            pd.DataFrame(
                {
                    "Item": [line.item for line in invoice.line_items],
                    "Quantity": [line.quantity for line in invoice.line_items],
                    "Unit price": [line.unit_price for line in invoice.line_items],
                    "Line total": [line.line_total for line in invoice.line_items],
                }
            ),
            hide_index=True,
            column_config={
                "Quantity": st.column_config.NumberColumn(format="%g"),
                "Unit price": st.column_config.NumberColumn(format="%.2f"),
                "Line total": st.column_config.NumberColumn(format="%.2f"),
            },
        )
    else:
        st.markdown("No Line Items.")
    if invoice.notes:
        st.caption(f"Notes: {md(invoice.notes)}")


def show_findings(findings: list[Finding]) -> None:
    if not findings:
        st.markdown("No Findings.")
        return
    lines = []
    for finding in findings:
        label = FINDING_LABELS.get(finding.kind, finding.kind.value)
        lines.append(f"- **{label}:** {md(finding.message)}")
        for problem in finding.evidence.get("problems") or []:  # why the AI Reader failed
            lines.append(f"    - {md(str(problem))}")
    st.markdown("\n".join(lines))


def show_guessed_fields(guesses: list[Finding]) -> None:
    if not guesses:
        st.markdown("None: every value was read from the document.")
        return
    st.dataframe(
        pd.DataFrame(
            {
                "Field": [g.evidence.get("field") for g in guesses],
                "The document shows": [g.evidence.get("read") or "(nothing)" for g in guesses],
                "The AI chose": [g.evidence.get("chosen") for g in guesses],
            }
        ),
        hide_index=True,
    )


def show_fraud_signals(case_file: CaseFile) -> None:
    signals = [f for f in case_file.findings if f.kind is FindingKind.FRAUD_SIGNAL]
    for signal in signals:
        st.markdown(f"**{md(signal.evidence.get('signal') or signal.message)}**")
        quote = (signal.evidence.get("quote") or "").strip()
        st.markdown(f"> {md(quote)}" if quote else "_No evidence was quoted._")
    if signals:
        return
    screens = [call for call in case_file.ai_calls if call.role is AIRole.FRAUD_SCREEN]
    if not screens:
        st.markdown("Not screened: the Fraud Screen did not run on this Invoice.")
    elif screens[-1].error or screens[-1].problems:
        why = screens[-1].error or "; ".join(screens[-1].problems)
        st.markdown(f"Not screened: {md(why)}")
    else:
        st.markdown("None found.")


def show_review_rounds(rounds: list[ReviewRound]) -> None:
    if not rounds:
        st.markdown("Not needed: the Approval Policy decided this Invoice on its own.")
        return
    for review_round in rounds:
        with st.container(border=True):
            st.markdown(f"**Round {review_round.round}**")
            if review_round.tool_calls:
                with st.expander(f"Tools the VP Review called ({len(review_round.tool_calls)})"):
                    for call in review_round.tool_calls:
                        st.markdown(f"`{call.tool}`" + (f" for {md(call.input)}" if call.input else ""))
                        st.code(call.result, language=None, wrap_lines=True)
            vp_review = review_round.vp_review
            if vp_review is not None:
                with st.container(horizontal=True, vertical_alignment="center"):
                    st.markdown("VP Review:")
                    outcome_badge(vp_review.decision)
                st.markdown(md(vp_review.justification))
            critique = review_round.critique
            if critique is not None:
                with st.container(horizontal=True, vertical_alignment="center"):
                    st.markdown("Critique:")
                    if critique.verdict == "accept":
                        st.badge("Accepts", color="green")
                    else:
                        st.badge("Objects", color="orange")
                st.markdown(md(critique.summary))
                if critique.objections:
                    st.markdown("\n".join(f"- {md(o)}" for o in critique.objections))
            if review_round.failure:
                st.warning(review_round.failure)


def show_ai_calls(case_file: CaseFile) -> None:
    if not case_file.ai_calls:
        return
    with st.expander(f"AI calls ({len(case_file.ai_calls)})"):
        st.dataframe(
            pd.DataFrame(
                {
                    "Role": [call.role.value for call in case_file.ai_calls],
                    "Attempt": [call.attempt for call in case_file.ai_calls],
                    "Took (s)": [call.duration_ms / 1000 for call in case_file.ai_calls],
                    "Problems": ["; ".join(call.problems) for call in case_file.ai_calls],
                    "Error": [call.error or "" for call in case_file.ai_calls],
                }
            ),
            hide_index=True,
            column_config={"Took (s)": st.column_config.NumberColumn(format="%.1f")},
        )


def process_panel(output_dir: Path) -> list[Path]:
    """The sidebar's Process panel: the file to process when Process is pressed."""
    with st.sidebar:
        st.header("Process an Invoice")
        if not read_setting(API_KEY_VAR, ENV_FILE):
            st.caption(
                f"No xAI API key ({API_KEY_VAR}), so every Invoice will be Held: none can "
                "be screened for Fraud Signals."
            )
        source = st.segmented_control(
            "From", ["Sample", "Upload"], default="Sample", required=True, key="source"
        )
        if source == "Sample":
            sample = st.selectbox(
                "Sample Invoice",
                [path for path in invoice_files(SAMPLES) if is_supported(path)],
                format_func=lambda path: path.name,
            )
            pressed = st.button("Process", type="primary", disabled=sample is None)
            return [sample] if pressed and sample else []
        upload = st.file_uploader(
            "Invoice file", type=[suffix.lstrip(".") for suffix in SUPPORTED_FORMATS]
        )
        if not st.button("Process", type="primary", disabled=upload is None) or not upload:
            return []
        return [save_upload(upload.name, upload.getvalue(), output_dir / "uploads")]


def policy_panel() -> None:
    """The sidebar's Approval Policy editor. It saves to the policy file the CLI reads, and
    the file's own rules check every edit before it is saved."""
    with st.sidebar:
        st.header("Approval Policy")
        try:
            policy = load_policy(POLICY_PATH)
        except PolicyError as e:
            st.error(str(e))
            return
        st.caption(
            f"What each Finding leads to. Saved to "
            f"`{POLICY_PATH.relative_to(ROOT).as_posix()}`, so "
            "`python main.py` uses it too. It applies to Invoices processed after saving."
        )
        with st.form("policy", border=False):
            threshold = st.number_input(
                "Review threshold (USD)",
                value=float(policy.review_threshold),
                step=1000.0,
                format="%.2f",
                help="An Invoice with no Findings and a total above this goes to VP Review; "
                "at or below it, it is Approved.",
            )
            edited = st.data_editor(
                pd.DataFrame(
                    {
                        "Finding": [FINDING_LABELS[kind] for kind in policy.findings],
                        "Outcome": [outcome.value for outcome in policy.findings.values()],
                    }
                ),
                hide_index=True,
                disabled=["Finding"],
                column_config={
                    "Outcome": st.column_config.SelectboxColumn(
                        options=[Outcome.REJECTED.value, Outcome.HELD.value], required=True
                    )
                },
            )
            if not st.form_submit_button("Save policy"):
                return
        kinds = {label: kind for kind, label in FINDING_LABELS.items()}
        settings = {
            "review_threshold": threshold,
            "findings": {
                kinds[label].value: outcome
                for label, outcome in zip(edited["Finding"], edited["Outcome"])
            },
        }
        try:
            save_policy(POLICY_PATH, settings)
        except PolicyError as e:
            st.error(str(e))
            return
        st.success("Saved. Process an Invoice to apply it.")


def save_upload(name: str, content: bytes, folder: Path) -> Path:
    """Keep an uploaded file, so its Case File's source file still exists; never overwrite."""
    folder.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", Path(name).stem) or "upload"
    stem = f"{datetime.now():%Y%m%dT%H%M%S}_{stem}"
    suffix = Path(name).suffix.lower()
    for n in itertools.count(1):
        path = folder / (f"{stem}{suffix}" if n == 1 else f"{stem}_{n}{suffix}")
        try:
            with path.open("xb") as f:
                f.write(content)
            return path
        except FileExistsError:
            continue
    raise AssertionError("unreachable")


def progress_line(
    name: str, stages: list[Stage], ended: Literal["finished", "failed"] | None = None
) -> str:
    """One Invoice's Stages so far, as the terminal shows them."""
    last = {"finished": "✓", "failed": "✗", None: "…"}[ended]
    steps = [
        f"{stage.value} {last if i == len(stages) - 1 else '✓'}" for i, stage in enumerate(stages)
    ]
    return f"**{md(name)}**  " + " → ".join(steps)


class PageBatch:
    """Shows a batch on the page: one line per Invoice, updated as each Stage starts."""

    def __init__(self) -> None:
        self.failed = False

    def skipped(self, path: Path, reason: str) -> None:
        st.markdown(f"**{md(path.name)}**  skipped: {md(reason)}")

    @contextmanager
    def invoice(self, path: Path) -> Iterator[PageInvoiceLine]:
        line = PageInvoiceLine(path.name, st.empty())
        yield line
        self.failed = self.failed or line.failed


class PageInvoiceLine:
    def __init__(self, name: str, placeholder: DeltaGenerator) -> None:
        self.name = name
        self.placeholder = placeholder
        self.stages: list[Stage] = []
        self.failed = False

    def stage(self, stage: Stage) -> None:
        self.stages.append(stage)
        self.placeholder.markdown(progress_line(self.name, self.stages))

    def processed(self, case_file: CaseFile) -> None:
        colour = OUTCOME_COLOURS[case_file.outcome]
        self.placeholder.markdown(
            f"{progress_line(self.name, self.stages, 'finished')}  "
            f":{colour}-badge[{case_file.outcome.value}]"
        )

    def not_processed(self, reason: str) -> None:
        self.failed = True
        self.placeholder.markdown(
            f"{progress_line(self.name, self.stages, 'failed')}  :red[{md(reason)}]"
        )


def process_from_page(files: list[Path], db_path: Path, output_dir: Path) -> None:
    """Hand the files to the same batch loop as the CLI, showing each Stage as it starts.

    Afterwards they are "This run". Clicking elsewhere on the page meanwhile stops the
    batch at the start of the next Stage; nothing is recorded or paid for that Invoice.
    """
    try:
        policy = load_policy(POLICY_PATH)
    except PolicyError as e:
        st.error(str(e))
        return
    ai = make_ai_client(read_setting(API_KEY_VAR, ENV_FILE), read_setting(MODEL_VAR, ENV_FILE))
    ensure_database(db_path)
    before = last_processed_id(db_path)
    label = files[0].name if len(files) == 1 else invoices(sum(map(is_supported, files)))
    batch = PageBatch()
    try:
        with st.status(f"Processing {label}…", expanded=True) as status:
            process_files(
                files, db_path=db_path, policy=policy, output_dir=output_dir, ai=ai, progress=batch
            )
            status.update(
                label=f"Processed {label}" + (", with problems" if batch.failed else ""),
                state="error" if batch.failed else "complete",
                expanded=batch.failed,
            )
    finally:  # even if stopped part-way, what did finish is this run
        source = f"processed from this page at {datetime.now():%H:%M}"
        st.session_state["run"] = Run(before, last_processed_id(db_path), source)
        st.session_state["view"] = "This run"


def main() -> None:
    args = page_args()
    st.set_page_config(page_title="Invoice results", page_icon="🧾", layout="wide")
    st.title("Invoice results")
    if "run" not in st.session_state:
        st.session_state["run"] = (
            None
            if args.run_after is None
            else Run(args.run_after, args.run_until, "from `python main.py`")
        )

    files = process_panel(args.output)
    policy_panel()
    again: list[Path] | None = st.session_state.pop("process_again", None)
    if again:
        files = again
    if not files and not load_rows(args.db, args.output):
        empty_state = st.empty()
        with empty_state.container():
            st.info("No Invoices have been processed yet.")
            if st.button("Process all sample invoices", type="primary"):
                files = invoice_files(SAMPLES)
        if files:
            empty_state.empty()  # the progress takes its place
    if files:
        process_from_page(files, args.db, args.output)

    history = load_rows(args.db, args.output)
    if not history:
        return
    run: Run | None = st.session_state["run"]
    this_run = [] if run is None else load_rows(args.db, args.output, run)

    run_tab, history_tab = st.tabs(["This run", "History"], key="view", on_change="rerun")
    with run_tab:
        if run is None:
            st.info(
                "Process an Invoice with the panel on the left, or run `python main.py`, "
                "to see those Invoices here."
            )
        elif not this_run:
            st.info("This run processed no Invoices.")
        else:
            outcome_counts(this_run)
            st.caption(
                f"{invoices(len(this_run))} {run.source}. Select an Invoice to open its Case "
                "File, or several to process them again."
            )
            show_selected(invoice_table(this_run, key=f"run-{run.after}"), key="run")
    with history_tab:
        outcome_counts(history)
        st.caption(
            f"{invoices(len(history))} processed so far. Copies of the same Invoice sit "
            "together, so Duplicates are side by side; click a column heading to sort "
            "differently. Select an Invoice to open its Case File, or several to process "
            "them again."
        )
        by_invoice = sorted(
            history, key=lambda row: (row.processed.invoice_number, row.processed.processed_at)
        )
        show_selected(invoice_table(by_invoice, key="history"), key="history")


if __name__ == "__main__":  # Streamlit runs the page as __main__
    main()
