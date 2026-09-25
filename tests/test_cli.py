"""The command line, with --no-ui or a fake results page."""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

import pytest
from dotenv import dotenv_values

import main
from conftest import INVOICES, REPO
from invoice_pipeline import FakeAI, make_ai_client
from invoice_pipeline.database import processing_history


class FakeResultsPage:
    """Stands in for the Streamlit page: records what it was pointed at, and gets Ctrl+C
    while starting or while serving, if told to."""

    url = "http://localhost:8599"

    def __init__(self, *, ctrl_c: Literal["while starting", "while serving"] | None = None):
        self.ctrl_c = ctrl_c
        self.opened_with: dict | None = None
        self.stopped = False

    def __call__(
        self,
        *,
        db_path: Path,
        output_dir: Path,
        run_after: int,
        run_until: int,
        api_key: str | None,
    ) -> "FakeResultsPage":
        self.opened_with = {
            "db_path": db_path,
            "output_dir": output_dir,
            "run_after": run_after,
            "run_until": run_until,
            "api_key": api_key,
        }
        if self.ctrl_c == "while starting":
            raise KeyboardInterrupt
        return self

    def wait(self) -> None:
        if self.ctrl_c == "while serving":
            raise KeyboardInterrupt

    def stop(self) -> None:
        self.stopped = True


def no_results_page(**_: object) -> FakeResultsPage:
    raise AssertionError("opened the results page despite --no-ui")


@pytest.fixture
def env_file(tmp_path):
    return tmp_path / "run" / ".env"


@pytest.fixture
def cli(tmp_path, env_file, monkeypatch, capsys):
    """Run the CLI in-process with a throwaway database, output folder and .env, and no
    xAI key or terminal to prompt in; return (exit code, output).

    The AI is the fake, which finds no Fraud Signals, unless `make_ai` says otherwise.
    With `page`, the CLI runs without --no-ui and opens that fake results page."""
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_MODEL", raising=False)
    monkeypatch.setattr(main, "interactive", lambda: False)

    def _cli(
        *args: str, page: FakeResultsPage | None = None, make_ai=lambda key, model: FakeAI()
    ) -> tuple[int, str]:
        code = main.main(
            [*args] if page else [*args, "--no-ui"],
            db_path=tmp_path / "run" / "inventory.db",
            output_dir=tmp_path / "run" / "output",
            env_file=env_file,
            results_page=page or no_results_page,
            make_ai=make_ai,
        )
        return code, capsys.readouterr().out

    return _cli


@pytest.fixture
def folder(tmp_path):
    """A folder of structured sample Invoices, copied out of filename order."""
    path = tmp_path / "invoices"
    path.mkdir()
    for name in ("invoice_1016.json", "invoice_1006.csv", "invoice_1014.xml"):
        shutil.copy(INVOICES / name, path / name)
    return path


def summary_rows(output: str) -> list[str]:
    return [line for line in output.splitlines() if "INV-" in line]


def test_a_folder_run_prints_a_summary_in_filename_order(cli, folder):
    code, output = cli(f"--invoice_path={folder}")

    assert code == 0
    rows = summary_rows(output)
    assert [re.findall(r"INV-\d+", row)[0] for row in rows] == ["INV-1006", "INV-1014", "INV-1016"]
    assert "Acme Industrial Supplies" in rows[0] and "Approved" in rows[0]
    assert "Held" in rows[1] and "EUR" in rows[1]
    assert "Rejected" in rows[2] and "WidgetC is not in the inventory database" in rows[2]


def test_each_invoice_shows_its_stage_progress(cli, folder):
    _, output = cli(f"--invoice_path={folder}")

    [approved] = [line for line in output.splitlines() if "invoice_1006.csv" in line]
    [rejected] = [line for line in output.splitlines() if "invoice_1016.json" in line]
    for stage in ("Ingestion", "Validation", "Approval", "Payment"):
        assert stage in approved
    assert "Approval" in rejected and "Payment" not in rejected


def test_unsupported_and_unreadable_files_are_reported_without_stopping_the_batch(
    cli, folder, tmp_path
):
    (folder / "invoice_1000.json").write_text("{ not json", encoding="utf-8")
    (folder / "notes.docx").write_text("meeting notes", encoding="utf-8")

    code, output = cli(f"--invoice_path={folder}")

    assert code == 0
    assert len(summary_rows(output)) == 3
    lines = output.splitlines()
    assert any("invoice_1000.json" in line and "could not be read" in line for line in lines)
    assert any("notes.docx" in line and "not a supported Invoice format" in line for line in lines)
    log = tmp_path / "run" / "output" / "pipeline.log"
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    [skipped] = [r for r in records if r["event"] == "file skipped"]
    assert Path(skipped["invoice_file"]).name == "notes.docx"


def test_each_stage_is_recorded_in_the_structured_log(cli, folder, tmp_path):
    cli(f"--invoice_path={folder}")

    log = tmp_path / "run" / "output" / "pipeline.log"
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    finished = [
        (Path(r["invoice_file"]).name, r["stage"]) for r in records if r["event"] == "stage finished"
    ]
    assert finished == [
        ("invoice_1006.csv", "Ingestion"),
        ("invoice_1006.csv", "Fraud Screen"),
        ("invoice_1006.csv", "Validation"),
        ("invoice_1006.csv", "Approval"),
        ("invoice_1006.csv", "Payment"),
        ("invoice_1014.xml", "Ingestion"),
        ("invoice_1014.xml", "Fraud Screen"),
        ("invoice_1014.xml", "Validation"),
        ("invoice_1014.xml", "Approval"),
        ("invoice_1016.json", "Ingestion"),
        ("invoice_1016.json", "Fraud Screen"),
        ("invoice_1016.json", "Validation"),
        ("invoice_1016.json", "Approval"),
    ]
    assert all(isinstance(r["duration_ms"], float) for r in records if r["event"] == "stage finished")


@pytest.mark.parametrize("package", ["rich", "langgraph"])
def test_a_missing_dependency_prints_the_install_command_not_a_traceback(package):
    # Simulate an uninstalled package: None in sys.modules makes importing it fail.
    script = (
        f"import runpy, sys; sys.modules[{package!r}] = None; "
        "sys.argv = ['main.py', '--invoice_path=no_such_invoice.json', '--no-ui']; "
        f"runpy.run_path({str(REPO / 'main.py')!r}, run_name='__main__')"
    )

    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=REPO, timeout=60
    )

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    [message] = result.stderr.strip().splitlines()
    assert package in message and "pip install -r requirements.txt" in message


def test_without_a_key_every_invoice_is_held_because_none_can_be_screened(cli, tmp_path):
    """Nothing is paid without a Fraud Screen, not even a clean structured one."""
    folder = tmp_path / "mixed"
    folder.mkdir()
    for name in ("invoice_1001.txt", "invoice_1006.csv"):
        shutil.copy(INVOICES / name, folder / name)

    code, output = cli(f"--invoice_path={folder}", make_ai=make_ai_client)

    assert code == 0
    assert "No xAI API key" in output
    [structured] = summary_rows(output)
    assert "INV-1006" in structured and "Held" in structured
    assert "Not screened for Fraud Signals" in structured
    [text] = [line for line in output.splitlines() if "invoice_1001.txt" in line and "│" in line]
    assert "Held" in text and "no xAI API key is configured" in text


def test_with_no_key_the_command_prompts_for_one_and_offers_to_save_it(
    cli, env_file, monkeypatch
):
    monkeypatch.setattr(main, "interactive", lambda: True)
    monkeypatch.setattr(main, "getpass", lambda prompt: "xai-test-key")
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    code, _ = cli(f"--invoice_path={INVOICES / 'invoice_1004.json'}")

    assert code == 0
    assert dotenv_values(env_file)["XAI_API_KEY"] == "xai-test-key"


def test_the_api_key_flag_skips_the_prompt(cli, env_file, monkeypatch):
    def no_prompt(prompt: str) -> str:
        raise AssertionError("prompted for a key that was passed on the command line")

    monkeypatch.setattr(main, "interactive", lambda: True)
    monkeypatch.setattr(main, "getpass", no_prompt)

    code, _ = cli("--api_key=xai-test-key", f"--invoice_path={INVOICES / 'invoice_1004.json'}")

    assert code == 0
    assert not env_file.exists()


def test_without_no_ui_the_results_page_opens_on_this_run_after_the_summary(
    cli, folder, tmp_path
):
    cli(f"--invoice_path={INVOICES / 'invoice_1004.json'}")  # an earlier run, not this one
    page = FakeResultsPage()

    code, output = cli(f"--invoice_path={folder}", page=page)

    assert code == 0
    assert page.opened_with is not None
    db_path = page.opened_with["db_path"]
    assert db_path == tmp_path / "run" / "inventory.db"
    assert page.opened_with["output_dir"] == tmp_path / "run" / "output"
    this_run = processing_history(
        db_path, after=page.opened_with["run_after"], until=page.opened_with["run_until"]
    )
    assert [row.invoice_number for row in this_run] == ["INV-1006", "INV-1014", "INV-1016"]
    lines = output.strip().splitlines()
    assert output.index("Summary") < output.index(page.url)
    assert page.url in lines[-1] and "Ctrl+C" in lines[-1]


def test_ctrl_c_stops_the_results_page_and_exits_cleanly(cli, folder):
    page = FakeResultsPage(ctrl_c="while serving")

    code, _ = cli(f"--invoice_path={folder}", page=page)

    assert code == 0
    assert page.stopped


def test_ctrl_c_while_the_results_page_starts_exits_cleanly(cli, folder):
    page = FakeResultsPage(ctrl_c="while starting")

    code, output = cli(f"--invoice_path={folder}", page=page)

    assert code == 0
    assert "Ctrl+C" not in output.strip().splitlines()[-1]


def test_the_results_page_does_not_open_when_nothing_was_processed(cli, tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    page = FakeResultsPage()

    code, _ = cli(f"--invoice_path={folder}", page=page)

    assert code == 1
    assert page.opened_with is None


def test_the_api_key_reaches_the_results_page_so_it_can_process_with_the_ai(cli, folder):
    page = FakeResultsPage()

    cli("--api_key=xai-test-key", f"--invoice_path={folder / 'invoice_1006.csv'}", page=page)

    assert page.opened_with is not None
    assert page.opened_with["api_key"] == "xai-test-key"


def test_the_summary_shows_invoice_numbers_normalised(cli, tmp_path):
    folder = tmp_path / "numbers"
    folder.mkdir()
    invoice = json.loads((INVOICES / "invoice_1016.json").read_text(encoding="utf-8"))
    invoice["invoice_number"] = "1016"
    (folder / "invoice_1016.json").write_text(json.dumps(invoice), encoding="utf-8")

    _, output = cli(f"--invoice_path={folder}")

    [row] = summary_rows(output)
    assert "INV-1016" in row
