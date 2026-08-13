"""Shared helpers for interactive local-import commands."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Callable, TextIO

from .models import MoodleError


def prompt(input_func: Callable[[str], str], message: str) -> str:
    try:
        return input_func(message).strip()
    except EOFError as exc:
        raise MoodleError("interactive input ended before the import was complete") from exc


def prompt_path(value: str) -> Path:
    direct = Path(value).expanduser()
    if direct.exists():
        return direct
    try:
        parts = shlex.split(value)
    except ValueError as exc:
        raise MoodleError(f"invalid file path: {exc}") from exc
    if len(parts) != 1:
        raise MoodleError("enter exactly one file path")
    return Path(parts[0]).expanduser()


def select_number(
    choices: list[tuple[Any, ...]],
    prompt_message: str,
    input_func: Callable[[str], str],
    output: TextIO,
) -> int:
    valid_ids = {int(choice[0]) for choice in choices}
    while True:
        value = prompt(input_func, prompt_message)
        try:
            selected = int(value)
        except ValueError:
            print("Please enter a list number or ID.", file=output)
            continue
        if 1 <= selected <= len(choices):
            return int(choices[selected - 1][0])
        if selected in valid_ids:
            return selected
        print("That selection is not in the list. Please try again.", file=output)
