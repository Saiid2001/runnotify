"""Out-of-process watchdog: report a run that was killed without warning.

``atexit`` covers every ending the interpreter gets to observe. ``SIGKILL`` is
not one of them, and ``SIGKILL`` is what the Linux OOM killer sends — so a run
that exhausts memory leaves a short table, no traceback, and no notification.

This module runs as a detached child (``python -m runnotify._watchdog``). It
polls whether its parent still exists and, when the parent disappears without
having disarmed it, delivers an OOM event through that run's own channels. A
channel added by a plugin therefore reports OOM with no change here.

Configuration arrives as one JSON object on **stdin**, never on the command
line: it carries webhook URLs and API tokens, and a command line is world
readable through ``ps``.

POSIX only. The liveness probe is ``os.kill(pid, 0)``, which reports existence
without signalling on POSIX; on Windows the same call terminates the target.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from collections.abc import Mapping
from typing import Any

from .channel import build_all
from .event import Event, Status

__all__ = ["main", "run"]


def _parent_alive(pid: int) -> bool:
    """Whether ``pid`` still names a live process.

    ``PermissionError`` counts as alive: the process exists, this one merely
    cannot signal it.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run(
    config: Mapping[str, Any],
    *,
    sleep: Any = time.sleep,
    alive: Any = _parent_alive,
) -> int:
    """Watch until the parent exits. Returns the process exit code.

    ``sleep`` and ``alive`` are injectable so the loop can be tested without a
    real process or real time.
    """
    pid = int(config["pid"])
    sentinel = str(config["sentinel"])
    poll = float(config.get("poll_interval", 5.0))
    topic = str(config["topic"])
    source = str(config.get("source", ""))
    sections = config.get("channels") or {}

    while True:
        sleep(poll)
        if alive(pid):
            continue

        # The parent is gone. A sentinel means it told us so.
        if os.path.exists(sentinel):
            with contextlib.suppress(OSError):
                os.unlink(sentinel)
            return 0

        channels, _ = build_all(sections)
        event = Event(
            topic=topic,
            status=Status.OOM,
            message=f"Process {pid} was killed before reporting a result — likely OOM",
            source=source,
        )
        failures = 0
        for channel in channels:
            try:
                channel.deliver(event)
            except Exception:
                failures += 1
        return 1 if failures and failures == len(channels) else 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m runnotify._watchdog``."""
    del argv
    try:
        config = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (ValueError, OSError):
        return 2
    if not isinstance(config, dict) or "pid" not in config:
        return 2
    try:
        return run(config)
    except Exception:
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
