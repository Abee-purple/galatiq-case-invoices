"""The Approval Policy file decides Outcomes and is checked when loaded."""

import shutil
import stat

import pytest
import yaml

from invoice_pipeline import FakeAI, FindingKind, Outcome, PolicyError, load_policy, save_policy

from conftest import DEFAULT_POLICY


def test_changing_shortfall_to_held_in_the_policy_holds_an_invoice_with_only_shortfalls(
    run, edited_policy, payment
):
    policy_file = edited_policy("shortfall: Rejected", "shortfall: Held")

    case_file = run("invoice_1005.json", policy_path=policy_file)

    assert {f.kind for f in case_file.findings} == {FindingKind.SHORTFALL}
    assert case_file.outcome == Outcome.HELD
    assert payment.calls == []


def test_the_most_severe_outcome_across_all_findings_wins(run, edited_policy):
    policy_file = edited_policy("shortfall: Rejected", "shortfall: Held")

    # INV-1007 has Shortfalls (now Held) and an arithmetic mismatch (still Rejected).
    case_file = run("invoice_1007.csv", policy_path=policy_file)

    assert {f.kind for f in case_file.findings} == {
        FindingKind.SHORTFALL,
        FindingKind.ARITHMETIC_MISMATCH,
    }
    assert case_file.outcome == Outcome.REJECTED
    assert case_file.reasons == [
        "Subtotal plus tax is 15,635.00, but the total says 15,525.00."
    ]


@pytest.mark.parametrize(
    "old, new, named_in_error",
    [
        ("shortfall: Rejected", "shortfal: Rejected", "shortfal"),
        ("shortfall: Rejected", "shortfall: Rejectd", "Rejectd"),
        ("shortfall: Rejected", "shortfall: Approved", "Approved"),
        ("  shortfall: Rejected ", "  # shortfall removed ", "shortfall"),
        ("review_threshold: 10000", "review_threshold: ten thousand", "review_threshold"),
        ("findings:", "findings: [", "YAML"),
    ],
    ids=["unknown kind", "unknown outcome", "approved", "missing kind", "bad threshold", "bad yaml"],
)
def test_an_invalid_policy_file_gives_a_clear_error(edited_policy, old, new, named_in_error):
    policy_file = edited_policy(old, new)

    with pytest.raises(PolicyError) as error:
        load_policy(policy_file)

    message = str(error.value)
    assert str(policy_file) in message
    assert named_in_error in message


def test_a_missing_policy_file_gives_a_clear_error(tmp_path):
    missing = tmp_path / "no_such_policy.yaml"

    with pytest.raises(PolicyError, match="could not be read"):
        load_policy(missing)


# Editing the policy from the results page: the page hands save_policy the same settings the
# file holds, and they are checked with the same rules before anything is written.

INV_1002_READING = {
    "invoice": {
        "invoice_number": "1002",
        "vendor": "Gadgets Co.",
        "invoice_date": "2026-01-30",
        "due_date": "2026-01-30",
        "currency": "USD",
        "line_items": [{"item": "GadgetX", "quantity": 20, "unit_price": 750.00}],
        "total": 15000.00,
        "payment_terms": "Net 30",
    },
    "guessed_fields": [],
}


@pytest.fixture
def policy_file(tmp_path):
    """A copy of the default Approval Policy to edit."""
    path = tmp_path / "policy.yaml"
    shutil.copy(DEFAULT_POLICY, path)
    return path


def settings(policy_path, **changes):
    """The settings read from a policy file, as the page edits them, with some Finding kinds
    changed."""
    data = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    data["findings"].update(changes)
    return data


def test_saving_shortfall_as_held_holds_inv_1002_when_it_is_run_again(run, policy_file):
    before = run("invoice_1002.txt", policy_path=policy_file, ai=FakeAI(reader=[INV_1002_READING]))

    save_policy(policy_file, settings(policy_file, shortfall="Held"))
    again = run("invoice_1002.txt", policy_path=policy_file, ai=FakeAI(reader=[INV_1002_READING]))

    assert before.outcome == Outcome.REJECTED
    assert {f.kind for f in again.findings} == {FindingKind.SHORTFALL}
    assert again.outcome == Outcome.HELD


def test_a_saved_policy_is_what_the_file_then_holds(policy_file):
    edited = settings(policy_file, duplicate="Rejected", fraud_signal="Rejected")
    edited["review_threshold"] = 2500.5

    saved = save_policy(policy_file, edited)

    assert load_policy(policy_file) == saved
    assert saved.review_threshold == 2500.5
    assert saved.findings[FindingKind.DUPLICATE] == Outcome.REJECTED
    assert saved.findings[FindingKind.FRAUD_SIGNAL] == Outcome.REJECTED
    assert saved.findings[FindingKind.SHORTFALL] == Outcome.REJECTED


def test_saving_keeps_the_files_comments_and_changes_only_the_edited_lines(policy_file):
    original = DEFAULT_POLICY.read_text(encoding="utf-8")

    save_policy(policy_file, settings(policy_file, shortfall="Held", duplicate="Rejected"))

    expected = original.replace(
        "shortfall: Rejected            # bills", "shortfall: Held                # bills"
    ).replace(
        "duplicate: Held                # same", "duplicate: Rejected            # same"
    )
    assert policy_file.read_text(encoding="utf-8") == expected


@pytest.mark.parametrize(
    "change, named_in_error",
    [
        ({"review_threshold": -5}, "review_threshold"),
        ({"findings": {"shortfall": "Approved"}}, "Approved"),
        ({"findings": {"shortfal": "Held"}}, "shortfal"),
    ],
    ids=["negative threshold", "approved", "unknown kind"],
)
def test_an_invalid_edit_is_refused_with_a_clear_message_and_nothing_is_saved(
    policy_file, change, named_in_error
):
    edited = settings(policy_file, **change.pop("findings", {}))
    edited.update(change)

    with pytest.raises(PolicyError) as error:
        save_policy(policy_file, edited)

    assert str(error.value).startswith("Not saved: ")
    assert named_in_error in str(error.value)
    assert policy_file.read_text(encoding="utf-8") == DEFAULT_POLICY.read_text(encoding="utf-8")


def test_an_edit_that_leaves_out_a_finding_kind_is_refused(policy_file):
    edited = settings(policy_file)
    del edited["findings"]["shortfall"]

    with pytest.raises(PolicyError, match="shortfall"):
        save_policy(policy_file, edited)


def test_a_policy_file_in_another_layout_is_rewritten_plainly(tmp_path):
    policy_file = tmp_path / "policy.yaml"
    compact = settings(DEFAULT_POLICY)
    policy_file.write_text(yaml.safe_dump(compact, default_flow_style=True), encoding="utf-8")

    saved = save_policy(policy_file, settings(DEFAULT_POLICY, shortfall="Held"))

    assert saved.findings[FindingKind.SHORTFALL] == Outcome.HELD
    assert load_policy(policy_file) == saved


def test_a_policy_file_that_cannot_be_written_is_refused_with_a_clear_message(policy_file):
    policy_file.chmod(stat.S_IREAD)
    try:
        with pytest.raises(PolicyError, match="^Not saved: .* could not be written"):
            save_policy(policy_file, settings(policy_file, shortfall="Held"))
    finally:
        policy_file.chmod(stat.S_IREAD | stat.S_IWRITE)

    assert load_policy(policy_file).findings[FindingKind.SHORTFALL] == Outcome.REJECTED
    assert [path.name for path in policy_file.parent.iterdir()] == [policy_file.name]
