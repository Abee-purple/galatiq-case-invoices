"""The Approval Policy: an editable file mapping Finding kinds to Outcomes."""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from .models import Finding, FindingKind, Invoice, Outcome


class ApprovalPolicy(BaseModel):
    review_threshold: float
    findings: dict[FindingKind, Outcome]


class Decision(str, Enum):
    """What the Approval Policy concludes: an Outcome, or a hand-off to VP Review."""

    APPROVED = "Approved"
    HELD = "Held"
    REJECTED = "Rejected"
    NEEDS_VP_REVIEW = "NeedsVPReview"


_SEVERITY = {Outcome.APPROVED: 0, Outcome.HELD: 1, Outcome.REJECTED: 2}


class PolicyError(ValueError):
    """The Approval Policy file is invalid; the run must stop rather than guess."""


_FINDING_OUTCOMES = (Outcome.REJECTED, Outcome.HELD)


def load_policy(path: Path) -> ApprovalPolicy:
    path = Path(path)

    def fail(problem: str) -> PolicyError:
        return PolicyError(f"Invalid Approval Policy in {path}: {problem}")

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise fail(f"the file could not be read ({_why(e)}).") from None
    except yaml.YAMLError as e:
        raise fail(f"the file is not valid YAML ({e.__class__.__name__}).") from None
    try:
        return _checked(data)
    except _PolicyProblem as problem:
        raise fail(str(problem)) from None


def save_policy(path: Path, settings: Mapping[str, Any]) -> ApprovalPolicy:
    """Check new settings with the same rules as loading the file, then write them to it.

    The file's comments and layout are kept: only the values that changed are rewritten.
    Nothing is written if the settings are invalid.
    """
    try:
        policy = _checked(settings)
    except _PolicyProblem as problem:
        raise PolicyError(f"Not saved: {problem}") from None
    path = Path(path)
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    except OSError as e:
        raise PolicyError(f"Not saved: {path} could not be read ({_why(e)}).") from None
    text = _with_values(current, policy)
    if _read_back(text) != policy:  # a layout we can't edit line by line; its comments go
        text = _plain_yaml(policy)
    temporary = path.with_name(f"{path.name}.saving")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    except OSError as e:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise PolicyError(f"Not saved: {path} could not be written ({_why(e)}).") from None
    return policy


class _PolicyProblem(ValueError):
    """What is wrong with some policy settings, in words that fit after "Invalid ...:"."""


def _checked(data: object) -> ApprovalPolicy:
    if not isinstance(data, Mapping):
        raise _PolicyProblem("expected 'review_threshold' and 'findings' settings.")

    threshold = data.get("review_threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or threshold < 0:
        raise _PolicyProblem(f"review_threshold must be a non-negative amount, not {threshold!r}.")

    mapping = data.get("findings")
    if not isinstance(mapping, Mapping):
        raise _PolicyProblem("'findings' must map each Finding kind to Rejected or Held.")

    known = {kind.value: kind for kind in FindingKind}
    allowed = {outcome.value: outcome for outcome in _FINDING_OUTCOMES}
    findings: dict[FindingKind, Outcome] = {}
    for name, outcome in mapping.items():
        if name not in known:
            raise _PolicyProblem(
                f"unknown Finding kind '{name}'. Known kinds: {', '.join(known)}."
            )
        if outcome not in allowed:
            raise _PolicyProblem(
                f"'{name}' is set to '{outcome}', but it must be Rejected or Held."
            )
        findings[known[name]] = allowed[outcome]

    missing = [name for name in known if known[name] not in findings]
    if missing:
        raise _PolicyProblem(f"no Outcome is set for Finding kind(s): {', '.join(missing)}.")

    return ApprovalPolicy(review_threshold=threshold, findings=findings)


# One `name: value  # comment` setting on its own line, at any depth. Setting names are
# unique in the file, and save_policy reads the result back, so a line this misjudges can't
# be saved silently.
_SETTING = re.compile(
    r"^(?P<head>[ \t]*(?P<name>\w+):[ \t]*)(?P<value>[^#\s](?:[^#\n]*[^#\s])?)"
    r"[ \t]*(?P<comment>#.*)?$",
    re.MULTILINE,
)


def _with_values(text: str, policy: ApprovalPolicy) -> str:
    """The file's text with each setting's value replaced, keeping comments in their column."""
    values = {
        "review_threshold": _yaml_number(policy.review_threshold),
        **_settings(policy)["findings"],
    }

    def replaced(setting: re.Match[str]) -> str:
        value = values.get(setting["name"])
        if value is None or value == setting["value"]:
            return setting[0]
        line = setting["head"] + value
        if setting["comment"] is None:
            return line
        column = setting.start("comment") - setting.start()
        return line + " " * max(1, column - len(line)) + setting["comment"]

    return _SETTING.sub(replaced, text)


def _read_back(text: str) -> ApprovalPolicy | None:
    try:
        return _checked(yaml.safe_load(text))
    except (yaml.YAMLError, _PolicyProblem):
        return None


def _settings(policy: ApprovalPolicy) -> dict[str, Any]:
    """The policy as the file holds it."""
    return {
        "review_threshold": policy.review_threshold,
        "findings": {kind.value: outcome.value for kind, outcome in policy.findings.items()},
    }


def _plain_yaml(policy: ApprovalPolicy) -> str:
    return "# Approval Policy: saved from the results page.\n\n" + yaml.safe_dump(
        _settings(policy), sort_keys=False
    )


def _why(error: OSError) -> str:
    return error.strerror or error.__class__.__name__


def _yaml_number(amount: float) -> str:
    return str(int(amount)) if float(amount).is_integer() else repr(float(amount))


def decide(
    policy: ApprovalPolicy, findings: list[Finding], invoice: Invoice | None
) -> tuple[Decision, list[str]]:
    """The most severe Outcome across all Findings wins."""
    if findings:
        worst = max((policy.findings[f.kind] for f in findings), key=_SEVERITY.__getitem__)
        reasons = [f.message for f in findings if policy.findings[f.kind] == worst]
        return Decision(worst.value), reasons
    if invoice is None:
        # Ingestion always raises a Finding for this; never Approve what wasn't read.
        return Decision.HELD, ["No Invoice could be read from the file."]
    total = invoice.total
    if not (invoice.vendor or "").strip() or total is None:
        # Validation always raises a Finding for these; never Approve what can't be paid.
        return Decision.HELD, ["The Invoice has no Vendor or no total, so it cannot be paid."]
    if total > policy.review_threshold:
        return Decision.NEEDS_VP_REVIEW, [
            f"Total {total:,.2f} is above the review threshold of {policy.review_threshold:,.2f}."
        ]
    return Decision.APPROVED, [
        f"No Findings and the total is within the review threshold of {policy.review_threshold:,.2f}."
    ]
