"""Test harness: the pipeline entry point, with a throwaway database and output folder."""

from pathlib import Path

import pytest

from invoice_pipeline import FakeAI, load_policy, process_invoice

REPO = Path(__file__).resolve().parent.parent
INVOICES = REPO / "data" / "invoices"
DEFAULT_POLICY = REPO / "config" / "approval_policy.yaml"


class RecordingPayment:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    def __call__(self, vendor: str, amount: float) -> dict:
        self.calls.append((vendor, amount))
        return {"status": "success"}


@pytest.fixture
def payment() -> RecordingPayment:
    return RecordingPayment()


@pytest.fixture
def run(tmp_path, payment):
    """Process a sample Invoice (by file name) or any Invoice file (by path)."""

    def _run(
        invoice: str | Path, policy_path: Path = DEFAULT_POLICY, on_stage=None, ai=None
    ):
        return process_invoice(
            INVOICES / invoice,
            db_path=tmp_path / "inventory.db",
            policy=load_policy(policy_path),
            output_dir=tmp_path / "output",
            payment=payment,
            on_stage=on_stage,
            ai=ai or FakeAI(),
        )

    return _run


@pytest.fixture
def edited_policy(tmp_path):
    """Write a copy of the default Approval Policy with one line changed."""

    def _edit(old: str, new: str) -> Path:
        text = DEFAULT_POLICY.read_text(encoding="utf-8")
        assert old in text, f"{old!r} not in the default policy"
        path = tmp_path / "policy.yaml"
        path.write_text(text.replace(old, new), encoding="utf-8")
        return path

    return _edit
