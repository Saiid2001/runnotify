"""Fixtures. The test doubles themselves live in :mod:`tests.fakes`."""

from __future__ import annotations

import os
from typing import Any

import pytest

from .fakes import FakeOpener, RecordingChannel


@pytest.fixture
def opener() -> FakeOpener:
    return FakeOpener()


@pytest.fixture
def patched_http(monkeypatch: pytest.MonkeyPatch, opener: FakeOpener) -> FakeOpener:
    """Point every channel's HTTP calls at the fake opener, with no real sleeping."""
    import runnotify._http as http_module

    real_request = http_module.request

    def fake_request(method: str, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("opener", opener)
        kwargs.setdefault("sleep", lambda _s: None)
        return real_request(method, url, **kwargs)

    monkeypatch.setattr("runnotify.channels.slack.request", fake_request)
    monkeypatch.setattr("runnotify.channels.notion.request", fake_request)
    return opener


@pytest.fixture
def recorder() -> RecordingChannel:
    return RecordingChannel()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own webhook and tokens out of the tests."""
    for var in list(os.environ):
        if var.startswith(("RUNNOTIFY_", "NOTIFY_", "NOTION_")):
            monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def isolated_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Run from an empty directory.

    Config discovery walks upward from the working directory, and this
    repository has a ``pyproject.toml`` at its root. Without this, a test that
    means to assert "no configuration found" would find the project's own.
    """
    monkeypatch.chdir(tmp_path)
