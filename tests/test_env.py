""".env discovery and parsing."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from runnotify._env import find_env_file, load_env, parse_env


def test_finds_the_nearest_file_walking_upward(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("A=1\n")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_env_file(nested) == tmp_path / ".env"


def test_returns_none_when_there_is_nothing(tmp_path: Path) -> None:
    assert find_env_file(tmp_path) is None


def test_parses_the_usual_shapes() -> None:
    parsed = parse_env(
        """
        # a comment

        NOTIFY_WEBHOOK_URL=https://hooks.invalid/x
        export NOTION_API_TOKEN=secret
        QUOTED="with spaces"
        SINGLE='also quoted'
        TRAILING=value   # explanation
        """
    )
    assert parsed == {
        "NOTIFY_WEBHOOK_URL": "https://hooks.invalid/x",
        "NOTION_API_TOKEN": "secret",
        "QUOTED": "with spaces",
        "SINGLE": "also quoted",
        "TRAILING": "value",
    }


def test_a_hash_inside_quotes_is_kept() -> None:
    assert parse_env('URL="https://x/#frag"') == {"URL": "https://x/#frag"}


def test_malformed_lines_are_skipped_not_fatal() -> None:
    """A stray line in someone's .env must not stop a run from reporting."""
    assert parse_env("garbage\n=novalue\nGOOD=1") == {"GOOD": "1"}


def test_an_exported_variable_wins_over_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A value set in the shell is a deliberate override."""
    monkeypatch.setenv("RUNNOTIFY_TOPIC", "from-shell")
    path = tmp_path / ".env"
    path.write_text("RUNNOTIFY_TOPIC=from-file\nRUNNOTIFY_SOURCE=from-file\n")
    applied = load_env(path)
    assert os.environ["RUNNOTIFY_TOPIC"] == "from-shell"
    assert os.environ["RUNNOTIFY_SOURCE"] == "from-file"
    assert applied == {"RUNNOTIFY_SOURCE": "from-file"}


def test_override_forces_the_file_to_win(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNNOTIFY_TOPIC", "from-shell")
    path = tmp_path / ".env"
    path.write_text("RUNNOTIFY_TOPIC=from-file\n")
    load_env(path, override=True)
    assert os.environ["RUNNOTIFY_TOPIC"] == "from-file"


def test_a_missing_file_is_not_an_error(tmp_path: Path) -> None:
    assert load_env(tmp_path / "absent") == {}
