"""Prompt registry (§6.1): versioned task prompts in prompts/<call_id>/<version>.md."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent
_VAR = re.compile(r"\{\{(\w+)\}\}")


@lru_cache(maxsize=None)
def preamble() -> str:
    return (_DIR / "preamble.md").read_text(encoding="utf-8").strip()


@lru_cache(maxsize=None)
def _raw(call_id: str, version: str) -> str:
    return (_DIR / call_id / f"{version}.md").read_text(encoding="utf-8").strip()


def task(call_id: str, version: str, variables: dict[str, str] | None = None) -> str:
    text = _raw(call_id, version)
    variables = variables or {}

    def sub(m: re.Match) -> str:
        if m.group(1) not in variables:
            raise KeyError(f"prompt {call_id}/{version} needs {{{{{m.group(1)}}}}}")
        return str(variables[m.group(1)])

    return _VAR.sub(sub, text)
