"""The AI provider: Grok for real runs, or a fake with scripted answers for tests.

Every AI role asks for a JSON answer shaped by a schema. The caller validates the
answer itself, so a malformed answer can be fed back to the AI as a specific error.
Grok is the only network dependency: everything else runs locally.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

import openai
from langchain_xai import ChatXAI
from pydantic import BaseModel, SecretStr, ValidationError

from .logs import log_event
from .models import AICall, AIRole

DEFAULT_MODEL = "grok-4.7"

MAX_RETRIES = 2
"""How many times an answer with problems is sent back to the AI before giving up."""

log = logging.getLogger(__name__)

Messages = list[tuple[str, str]]
"""A conversation as (speaker, text) pairs; speaker is "system", "user" or "assistant"."""


class AIUnavailableError(Exception):
    """The AI could not be asked at all: no key, an invalid key, or the service is unreachable."""


class AIClient(Protocol):
    def ask(self, role: AIRole, messages: Messages, schema: type[BaseModel]) -> str:
        """Return the AI's answer as JSON text, not yet validated against `schema`."""
        ...


def make_ai_client(api_key: str | None, model: str | None = None) -> AIClient:
    """The one place the AI provider is chosen."""
    if not api_key:
        return NoAI("no xAI API key is configured (set XAI_API_KEY or pass --api_key).")
    return GrokAI(api_key, model or DEFAULT_MODEL)


class GrokAI:
    """Grok via LangChain's xAI integration, using xAI's structured outputs."""

    def __init__(self, api_key: str, model: str) -> None:
        self.model = model
        self._chat = ChatXAI(model=model, api_key=SecretStr(api_key), timeout=60, max_retries=2)

    def ask(self, role: AIRole, messages: Messages, schema: type[BaseModel]) -> str:
        structured = self._chat.with_structured_output(
            schema, method="json_schema", include_raw=True
        )
        try:
            result = structured.invoke(messages)
        except (openai.AuthenticationError, openai.PermissionDeniedError) as e:
            raise AIUnavailableError(
                f"xAI rejected the API key ({e.status_code}). Check XAI_API_KEY or --api_key."
            ) from e
        except openai.NotFoundError as e:
            raise AIUnavailableError(
                f"xAI does not offer the model '{self.model}'. "
                "Set XAI_MODEL to a current Grok model."
            ) from e
        except openai.APIConnectionError as e:
            raise AIUnavailableError(
                f"could not reach xAI ({e}). Check the network connection."
            ) from e
        except openai.APIStatusError as e:
            raise AIUnavailableError(f"xAI returned an error ({e.status_code}): {e.message}") from e
        # The raw text, not LangChain's parse: the Reader validates it and explains any problem.
        assert isinstance(result, dict)
        return str(result["raw"].text)


class NoAI:
    """Stands in when there is no way to ask the AI; every question fails with the reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def ask(self, role: AIRole, messages: Messages, schema: type[BaseModel]) -> str:
        raise AIUnavailableError(self.reason)


Answer = TypeVar("Answer", bound=BaseModel)


@dataclass
class Asked(Generic[Answer]):
    """The outcome of asking the AI, with retries, for an answer that passes its checks."""

    answer: Answer | None
    """The last answer that fit the schema, even if it still had problems."""
    calls: list[AICall]
    unavailable: str | None
    """Why the AI could not be asked at all; None if it answered."""

    @property
    def valid(self) -> bool:
        return self.unavailable is None and not self.calls[-1].problems

    @property
    def failure(self) -> str | None:
        """Why there is no valid answer, in words for a Case File; None if there is one."""
        if self.unavailable is not None:
            return f"the AI could not be asked: {self.unavailable}"
        if not self.valid:
            return f"the AI gave no valid answer in {len(self.calls)} attempts"
        return None


def ask_with_retries(
    ai: AIClient,
    role: AIRole,
    messages: Messages,
    schema: type[Answer],
    check: Callable[[Answer], list[str]],
    advice: str = "",
) -> Asked[Answer]:
    """Ask until the answer fits `schema` and `check` finds no problems, sending the problems
    back (with `advice`) up to MAX_RETRIES times. Every call is recorded and logged."""
    messages = list(messages)
    calls: list[AICall] = []
    last_fit: Answer | None = None
    for attempt in range(1, MAX_RETRIES + 2):
        started = time.perf_counter()
        try:
            text = ai.ask(role, messages, schema)
        except AIUnavailableError as e:
            # Not a bad answer, so asking again won't help.
            calls.append(_record_call(role, attempt, started, answer=None, error=str(e)))
            return Asked(last_fit, calls, str(e))
        try:
            answer = schema.model_validate_json(text)
        except ValidationError as e:
            problems = _schema_problems(e)
        else:
            last_fit = answer
            problems = check(answer)
        calls.append(_record_call(role, attempt, started, answer=text, problems=problems))
        if not problems:
            break
        listed = "\n".join(f"- {problem}" for problem in problems)
        feedback = (
            f"Your answer has these problems:\n{listed}\n"
            f"Re-read the document and answer again. {advice}"
        ).strip()
        messages += [("assistant", text), ("user", feedback)]
    return Asked(last_fit, calls, None)


def _schema_problems(error: ValidationError) -> list[str]:
    """Each way an answer failed its schema, worded to be sent back to the AI."""
    return [
        f"{'.'.join(str(part) for part in e['loc']) or 'answer'}: {e['msg']}"
        for e in error.errors()
    ]


def _record_call(
    role: AIRole,
    attempt: int,
    started: float,
    *,
    answer: str | None,
    problems: list[str] | None = None,
    error: str | None = None,
) -> AICall:
    """Record and log one question put to the AI; `started` is from time.perf_counter()."""
    call = AICall(
        role=role,
        attempt=attempt,
        answer=answer,
        problems=problems or [],
        error=error,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    log_event(
        log,
        "ai call",
        role=call.role.value,
        attempt=attempt,
        duration_ms=call.duration_ms,
        problems=call.problems,
        error=call.error,
    )
    return call


UNSCRIPTED_ANSWERS: dict[AIRole, dict[str, Any]] = {AIRole.FRAUD_SCREEN: {"fraud_signals": []}}
"""What the fake answers for a role a test never scripts: the Fraud Screen finds nothing."""


class FakeAI:
    """Pre-written answers, handed out in order per role, so tests are fast and repeatable.

    An answer is a JSON-able dict, raw text (e.g. something malformed), or an exception to raise.
    A role in UNSCRIPTED_ANSWERS that a test doesn't script gets that answer every time.
    """

    def __init__(self, **answers: list[Any]) -> None:
        self._answers = {AIRole[role.upper()]: list(queue) for role, queue in answers.items()}
        self.conversations: list[tuple[AIRole, Messages]] = []

    def ask(self, role: AIRole, messages: Messages, schema: type[BaseModel]) -> str:
        self.conversations.append((role, list(messages)))
        if role not in self._answers and role in UNSCRIPTED_ANSWERS:
            return json.dumps(UNSCRIPTED_ANSWERS[role])
        queue = self._answers.get(role)
        if not queue:
            raise AssertionError(f"The fake AI has no scripted answer left for the {role.value}.")
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, str) else json.dumps(answer)
