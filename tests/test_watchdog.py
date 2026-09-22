"""The out-of-process OOM watchdog: arming, disarming, and what it reports.

The watchdog exists for the one ending the interpreter never observes. These
tests drive its loop directly with an injected clock and liveness probe; the
subprocess itself is exercised end to end by the last test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from runnotify import _watchdog
from runnotify.channel import register, unregister
from runnotify.event import Status
from runnotify.notifier import Notifier

from .fakes import RecordingChannel


def config(tmp_path: Path, **kwargs: object) -> dict:
    base = {
        "pid": 424242,
        "sentinel": str(tmp_path / "sentinel"),
        "poll_interval": 0.0,
        "topic": "crawl",
        "source": "host-1",
        "channels": {},
    }
    base.update(kwargs)
    return base


class Liveness:
    """Alive for ``n`` polls, then gone."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.calls = 0

    def __call__(self, pid: int) -> bool:
        self.calls += 1
        return self.calls <= self.n


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


def test_a_disarmed_parent_exit_reports_nothing(tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel"
    sentinel.touch()
    recorder = RecordingChannel()
    register("recording", type(recorder), replace=True)
    try:
        code = _watchdog.run(
            config(tmp_path, channels={"recording": {}}),
            sleep=lambda _s: None,
            alive=Liveness(2),
        )
    finally:
        unregister("recording")
    assert code == 0
    assert recorder.events == []
    assert not sentinel.exists()  # cleaned up on the way out


def test_an_undisarmed_parent_exit_reports_oom(tmp_path: Path) -> None:
    delivered: list = []

    class Capture(RecordingChannel):
        def deliver(self, event) -> None:  # type: ignore[no-untyped-def]
            delivered.append(event)

    register("capture", Capture, replace=True)
    try:
        code = _watchdog.run(
            config(tmp_path, channels={"capture": {}}),
            sleep=lambda _s: None,
            alive=Liveness(3),
        )
    finally:
        unregister("capture")
    assert code == 0
    assert [e.status for e in delivered] == [Status.OOM]
    assert delivered[0].topic == "crawl"
    assert delivered[0].source == "host-1"
    assert "424242" in delivered[0].message


def test_it_waits_while_the_parent_is_alive(tmp_path: Path) -> None:
    alive = Liveness(5)
    _watchdog.run(config(tmp_path), sleep=lambda _s: None, alive=alive)
    assert alive.calls == 6


def test_permission_error_counts_as_alive() -> None:
    """The process exists; this one merely cannot signal it."""

    def raiser(pid: int, sig: int) -> None:
        raise PermissionError

    import runnotify._watchdog as module

    original = module.os.kill
    module.os.kill = raiser  # type: ignore[assignment]
    try:
        assert module._parent_alive(1) is True
    finally:
        module.os.kill = original  # type: ignore[assignment]


def test_a_live_pid_is_reported_alive() -> None:
    assert _watchdog._parent_alive(os.getpid()) is True


def test_main_rejects_junk_on_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    class Stdin:
        buffer = type("B", (), {"read": staticmethod(lambda: b"not json")})()

    monkeypatch.setattr(_watchdog.sys, "stdin", Stdin())
    assert _watchdog.main([]) == 2


# --------------------------------------------------------------------------- #
# Arming
# --------------------------------------------------------------------------- #


def test_arming_passes_configuration_on_stdin_never_on_the_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command line is world readable through ps; these options are secrets."""
    captured: dict = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            captured["argv"] = argv
            self.stdin = self

        def write(self, data: bytes) -> None:
            captured["stdin"] = json.loads(data.decode())

        def close(self) -> None:
            pass

    monkeypatch.setattr("runnotify.notifier.subprocess.Popen", FakePopen)
    notifier = Notifier(
        "crawl",
        channels=[RecordingChannel()],
        cancel_on_exit=False,
    )
    notifier.config.channels = {"slack": {"webhook_url": "https://hooks.invalid/SECRET"}}
    assert notifier.start_oom_watchdog() is True

    assert captured["argv"][1:] == ["-m", "runnotify._watchdog"]
    assert not any("SECRET" in str(a) for a in captured["argv"])
    assert captured["stdin"]["channels"]["slack"]["webhook_url"].endswith("SECRET")
    assert captured["stdin"]["pid"] == os.getpid()


def test_arming_is_declined_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """os.kill(pid, 0) terminates the target on Windows — it would kill the run."""
    notifier = Notifier("crawl", channels=[RecordingChannel()], cancel_on_exit=False)
    # Patched only around the call: os.name is global, and pathlib reads it too.
    monkeypatch.setattr("runnotify.notifier.os.name", "nt")
    try:
        assert notifier.start_oom_watchdog() is False
    finally:
        monkeypatch.undo()
    assert notifier._sentinel is None


def test_arming_is_skipped_with_no_channels() -> None:
    notifier = Notifier("crawl", channels=[], cancel_on_exit=False)
    assert notifier.start_oom_watchdog() is False


def test_a_terminal_status_disarms_the_watchdog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    notifier = Notifier("crawl", channels=[RecordingChannel()], cancel_on_exit=False)
    sentinel = tmp_path / "sentinel"
    notifier._sentinel = str(sentinel)
    notifier.progress("halfway")
    assert not sentinel.exists()
    notifier.completed("done")
    assert sentinel.exists()


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(os.name == "nt", reason="the watchdog is POSIX only")
def test_the_module_runs_as_a_subprocess_and_exits_cleanly(tmp_path: Path) -> None:
    """`python -m runnotify._watchdog` must actually be runnable."""
    sentinel = tmp_path / "sentinel"
    sentinel.touch()
    payload = json.dumps(
        {
            "pid": 999_999,  # already gone
            "sentinel": str(sentinel),
            "poll_interval": 0.01,
            "topic": "crawl",
            "source": "host",
            "channels": {},
        }
    ).encode()
    proc = subprocess.run(
        [sys.executable, "-m", "runnotify._watchdog"],
        input=payload,
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    assert not sentinel.exists()
