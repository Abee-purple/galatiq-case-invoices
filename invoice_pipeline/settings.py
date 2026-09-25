"""Settings from the environment or a .env file; the environment wins."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, set_key

API_KEY_VAR = "XAI_API_KEY"
MODEL_VAR = "XAI_MODEL"


def read_setting(name: str, env_file: Path) -> str | None:
    value = os.environ.get(name)
    if not value and env_file.exists():
        value = dotenv_values(env_file).get(name)
    return (value or "").strip() or None


def save_setting(name: str, value: str, env_file: Path) -> None:
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.touch()
    set_key(env_file, name, value)
