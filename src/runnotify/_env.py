"""Finding and reading a ``.env`` file, with no dependencies.

The CLI is the tool people reach for when a webhook is not working, and it is
invoked by hand rather than from a script that has already loaded its own
environment. Without this it reports a missing variable that is, as far as the
file is concerned, set.

Existing environment variables win: a value exported in the shell is a
deliberate override and must not be replaced by a file.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["find_env_file", "load_env", "parse_env"]


def find_env_file(start: Path | str | None = None, *, name: str = ".env") -> Path | None:
    """The nearest ``name`` at or above ``start``, or None."""
    current = Path(start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def parse_env(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines.

    Handles ``export`` prefixes, ``#`` comments, blank lines, and single or
    double quoted values. Anything that is not a assignment is skipped rather
    than raising: a malformed line in someone's ``.env`` must not stop a run from
    reporting.
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        elif "#" in value:
            value = value.split("#", 1)[0].strip()
        values[key] = value
    return values


def load_env(path: Path | str, *, override: bool = False) -> dict[str, str]:
    """Read ``path`` into :data:`os.environ`. Returns what was applied."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    applied: dict[str, str] = {}
    for key, value in parse_env(text).items():
        if override or key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied
