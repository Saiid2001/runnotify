"""Orchestration: turn a call into an event and hand it to every channel.

This module contains no vendor knowledge. It decides *whether* and *when* to
deliver — severity filtering, progress throttling, hang detection, what happens
at exit — and delegates *how* to the channels. A test asserts that no vendor
name appears in this file, because the one thing that makes a channel seam real
is that the core cannot be tempted to special-case a backend.

Failures are isolated. A channel that raises is recorded and the remaining
channels still run: a notifier exists to report on a run, and it must never be
the reason one ends.

The exit hook and the watchdog are two halves of the same guarantee. ``atexit``
covers every ending Python can observe — a clean return, ``sys.exit``, an
unhandled exception, ``KeyboardInterrupt`` — and reports it as CANCELLED if
nothing terminal was sent. It cannot cover ``SIGKILL``, which is exactly what the
Linux OOM killer sends, so that case is left to an out-of-process watchdog.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

from .channel import Channel, build_all
from .config import Config
from .event import Event, Status, utcnow

__all__ = ["DeliveryResult", "Notifier"]

logger = logging.getLogger("runnotify")


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """What happened to one event, per channel.

    Truthy when every channel that wanted the event accepted it. A channel that
    filtered the event out on severity is absent from :attr:`outcomes` — it did
    not fail, it was not asked.
    """

    event: Event
    outcomes: dict[str, BaseException | None] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.outcomes) and all(e is None for e in self.outcomes.values())

    @property
    def delivered(self) -> list[str]:
        return [name for name, exc in self.outcomes.items() if exc is None]

    @property
    def failed(self) -> dict[str, BaseException]:
        return {name: exc for name, exc in self.outcomes.items() if exc is not None}

    def __str__(self) -> str:
        if not self.outcomes:
            return f"{self.event.status.value}: no channel accepted this event"
        parts = [f"{n}=ok" for n in self.delivered]
        parts += [f"{n}=failed({exc})" for n, exc in self.failed.items()]
        return f"{self.event.status.value}: " + ", ".join(parts)


class Notifier:
    """Reports a run's status to every configured channel.

    Args:
        topic: Identifies the run. Channels use it as the key for a row, a
            thread or a title, so two concurrent runs sharing a topic will
            report into the same place.
        channels: Use these channels instead of building any from
            configuration. Anything satisfying
            :class:`~runnotify.channel.Channel` is accepted.
        config: A resolved :class:`~runnotify.config.Config`. Loaded from file
            and environment when omitted.
        config_path: Read this TOML file instead of searching for one.
        source: Machine identifier. Defaults to the hostname.
        hang_timeout: Seconds without a :meth:`ping` before :meth:`check_hang`
            reports a hang.
        progress_interval: Minimum seconds between two :meth:`progress` events.
        cancel_on_exit: Report CANCELLED at interpreter exit if no terminal
            status was sent.
        oom_watchdog: Arm the out-of-process OOM watchdog at construction.
        dry_run: Build and log events without delivering them.
        strict: Raise on a channel that cannot be configured, instead of
            carrying on without it.
    """

    def __init__(
        self,
        topic: str | None = None,
        *,
        channels: Sequence[Channel] | None = None,
        config: Config | None = None,
        config_path: Path | str | None = None,
        source: str | None = None,
        hang_timeout: float | None = None,
        progress_interval: float | None = None,
        cancel_on_exit: bool | None = None,
        oom_watchdog: bool | None = None,
        dry_run: bool | None = None,
        strict: bool | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        cfg = config if config is not None else Config.load(config_path, env=env)

        # Keyword arguments are the top layer: whatever the caller named wins
        # over the file and the environment.
        self.topic = topic or cfg.topic
        if not self.topic:
            raise ValueError("Notifier requires a topic (argument, config or RUNNOTIFY_TOPIC)")
        self.source = source or cfg.source
        self.hang_timeout = cfg.hang_timeout if hang_timeout is None else hang_timeout
        self.progress_interval = (
            cfg.progress_interval if progress_interval is None else progress_interval
        )
        self.cancel_on_exit = cfg.cancel_on_exit if cancel_on_exit is None else cancel_on_exit
        self.dry_run = cfg.dry_run if dry_run is None else dry_run
        self.strict = cfg.strict if strict is None else strict
        self.config = cfg

        self.problems: list[str] = []
        if channels is not None:
            self.channels: list[Channel] = list(channels)
        else:
            self.channels, self.problems = build_all(cfg.enabled_channels(), strict=self.strict)

        self._terminal_sent = False
        self._closed = False
        self._last_ping = time.monotonic()
        # None, not 0.0: monotonic() is uptime-based, so a zero sentinel would
        # throttle the first progress event on a host that booted recently.
        self._last_progress: float | None = None
        self._hang_sent = False
        self._hang_stop = threading.Event()
        self._hang_thread: threading.Thread | None = None
        self._sentinel: str | None = None
        self._lock = threading.Lock()

        if self.cancel_on_exit and self.enabled:
            atexit.register(self._on_exit)

        if self.enabled:
            logger.info(
                "reporting %r from %s via %s",
                self.topic,
                self.source,
                ", ".join(self.channel_names) or "no channels",
            )
        else:
            logger.debug("no channels configured; reporting is off")

        want_watchdog = cfg.watchdog.oom if oom_watchdog is None else oom_watchdog
        if want_watchdog:
            self.start_oom_watchdog(poll_interval=cfg.watchdog.poll_interval)

    # -- Introspection ---------------------------------------------------- #

    @property
    def enabled(self) -> bool:
        """Whether anything will actually be delivered."""
        return bool(self.channels) and not self.dry_run

    @property
    def channel_names(self) -> list[str]:
        return [getattr(c, "name", type(c).__name__) for c in self.channels]

    def add_channel(self, channel: Channel) -> None:
        """Attach another channel to an existing notifier."""
        self.channels.append(channel)

    def __repr__(self) -> str:
        return (
            f"Notifier(topic={self.topic!r}, source={self.source!r}, "
            f"channels={self.channel_names!r}, dry_run={self.dry_run!r})"
        )

    # -- Sending ---------------------------------------------------------- #

    def build_event(
        self,
        status: Status | str,
        message: str = "",
        progress: float | None = None,
        **extra: Any,
    ) -> Event:
        return Event(
            topic=self.topic,
            status=Status.coerce(status),
            message=message,
            source=self.source,
            timestamp=utcnow(),
            progress=progress,
            extra=extra,
        )

    def notify(
        self,
        status: Status | str,
        message: str = "",
        progress: float | None = None,
        **extra: Any,
    ) -> DeliveryResult:
        """Deliver one event to every channel that wants it.

        Never raises on a delivery failure; inspect the result, or enable
        ``strict`` at construction to fail on misconfiguration instead.
        """
        event = self.build_event(status, message, progress, **extra)
        return self.dispatch(event)

    def dispatch(self, event: Event) -> DeliveryResult:
        """Deliver an already-built event. The single path every send takes."""
        if event.is_terminal:
            self._terminal_sent = True
            self._disarm_watchdog()

        outcomes: dict[str, BaseException | None] = {}
        for channel in self.channels:
            name = getattr(channel, "name", type(channel).__name__)
            try:
                if not self._wants(channel, event):
                    continue
            except Exception as exc:  # a broken filter must not stop delivery
                logger.warning("channel %r should_deliver failed: %s", name, exc)
            if self.dry_run:
                logger.info("dry run: would deliver %s to %s", event.status.value, name)
                outcomes[name] = None
                continue
            try:
                channel.deliver(event)
                outcomes[name] = None
            except Exception as exc:
                logger.warning("channel %r failed to deliver %s: %s", name, event.status.value, exc)
                outcomes[name] = exc

        return DeliveryResult(event=event, outcomes=outcomes)

    @staticmethod
    def _wants(channel: Channel, event: Event) -> bool:
        should = getattr(channel, "should_deliver", None)
        return True if should is None else bool(should(event))

    # -- Named statuses --------------------------------------------------- #

    def start(self, message: str = "", **extra: Any) -> DeliveryResult:
        """Mark the run as running. Call once, at the beginning of work."""
        return self.notify(Status.RUNNING, message, **extra)

    running = start

    def completed(self, message: str = "", **extra: Any) -> DeliveryResult:
        return self.notify(Status.COMPLETED, message, **extra)

    def error(self, message: str = "", **extra: Any) -> DeliveryResult:
        return self.notify(Status.ERROR, message, **extra)

    def cancelled(self, message: str = "", **extra: Any) -> DeliveryResult:
        return self.notify(Status.CANCELLED, message, **extra)

    def hang(self, message: str = "", **extra: Any) -> DeliveryResult:
        return self.notify(Status.HANG, message, **extra)

    def oom(self, message: str = "", **extra: Any) -> DeliveryResult:
        return self.notify(Status.OOM, message, **extra)

    def progress(
        self, message: str = "", n: float | None = None, *, force: bool = False, **extra: Any
    ) -> DeliveryResult | None:
        """Report progress, no more often than ``progress_interval``.

        Returns None when the event was throttled — which is not a failure, and
        is why this is distinguishable from a falsy :class:`DeliveryResult`.
        """
        with self._lock:
            now = time.monotonic()
            if (
                not force
                and self._last_progress is not None
                and now - self._last_progress < self.progress_interval
            ):
                return None
            self._last_progress = now
        return self.notify(Status.PROGRESS, message, progress=n, **extra)

    # -- Hang detection --------------------------------------------------- #

    def ping(self) -> None:
        """Reset the hang timer. Call from wherever the run makes progress."""
        with self._lock:
            self._last_ping = time.monotonic()
            self._hang_sent = False

    def check_hang(self) -> DeliveryResult | None:
        """Report a hang if nothing has pinged within ``hang_timeout``.

        Reports at most once per stall; :meth:`ping` re-arms it.
        """
        with self._lock:
            if self._hang_sent:
                return None
            elapsed = time.monotonic() - self._last_ping
            if elapsed <= self.hang_timeout:
                return None
            self._hang_sent = True
        minutes = int(elapsed // 60)
        return self.hang(f"No progress for {minutes} minutes")

    def start_hang_watcher(self, check_interval: float = 300.0) -> threading.Thread:
        """Check for a hang on a background thread until :meth:`close`."""
        if self._hang_thread is not None:
            return self._hang_thread

        def watch() -> None:
            while not self._hang_stop.wait(check_interval):
                try:
                    self.check_hang()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("hang watcher error: %s", exc)

        thread = threading.Thread(target=watch, name="runnotify-hang", daemon=True)
        thread.start()
        self._hang_thread = thread
        return thread

    # -- OOM watchdog ----------------------------------------------------- #

    def start_oom_watchdog(self, poll_interval: float = 5.0) -> bool:
        """Arm a detached process that reports OOM if this one is killed.

        The watchdog polls whether this process still exists and, if it
        disappears without a terminal status having been sent, delivers an OOM
        event through the same channels. Any terminal status disarms it, as does
        a clean interpreter exit.

        Configuration reaches the child on **stdin**, never on the command line:
        the options carry webhook URLs and API tokens, and a command line is
        readable by every user on the machine.

        POSIX only. The liveness probe is ``os.kill(pid, 0)``, which reports
        existence without signalling on POSIX; on Windows ``os.kill`` terminates
        the target instead, so arming there would kill the run it is watching.
        Returns whether the watchdog was armed.
        """
        if os.name == "nt":
            logger.info(
                "OOM watchdog not armed: its liveness probe terminates the process on "
                "Windows, and the OOM killer it watches for is a Linux facility"
            )
            return False
        if not self.channels:
            logger.debug("OOM watchdog not armed: no channels configured")
            return False
        if self._sentinel is not None:
            return True

        pid = os.getpid()
        sentinel = os.path.join(tempfile.gettempdir(), f".runnotify-oom-{pid}")
        payload = {
            "pid": pid,
            "sentinel": sentinel,
            "poll_interval": poll_interval,
            "topic": self.topic,
            "source": self.source,
            "channels": self.config.enabled_channels(),
        }
        try:
            child = subprocess.Popen(
                [sys.executable, "-m", "runnotify._watchdog"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            logger.warning("could not start the OOM watchdog: %s", exc)
            return False

        assert child.stdin is not None
        try:
            child.stdin.write(json.dumps(payload).encode("utf-8"))
            child.stdin.close()
        except OSError as exc:
            logger.warning("could not configure the OOM watchdog: %s", exc)
            return False

        self._sentinel = sentinel
        # A clean exit of any kind disarms it; SIGKILL runs no hooks, which is
        # the case the watchdog exists for.
        atexit.register(self._disarm_watchdog)
        logger.debug("OOM watchdog armed for pid %d", pid)
        return True

    def _disarm_watchdog(self) -> None:
        if not self._sentinel:
            return
        try:
            Path(self._sentinel).touch()
        except OSError as exc:  # pragma: no cover - temp dir unwritable
            logger.debug("could not disarm the OOM watchdog: %s", exc)

    # -- Lifecycle -------------------------------------------------------- #

    def _on_exit(self) -> None:
        """Report CANCELLED if the interpreter is exiting untold."""
        if self._terminal_sent or self._closed:
            return
        self._terminal_sent = True
        try:
            self.dispatch(
                self.build_event(Status.CANCELLED, "Process exited without a terminal status")
            )
        except Exception as exc:  # pragma: no cover - interpreter teardown
            logger.debug("exit notification failed: %s", exc)

    def close(self) -> None:
        """Stop the hang watcher and release every channel. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._hang_stop.set()
        for channel in self.channels:
            closer = getattr(channel, "close", None)
            if closer is None:
                continue
            try:
                closer()
            except Exception as exc:
                logger.warning("channel close failed: %s", exc)

    def __enter__(self) -> Notifier:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        """Report the block's outcome, then close.

        A clean exit is COMPLETED; ``KeyboardInterrupt`` and ``SystemExit`` are
        CANCELLED; anything else is ERROR carrying the exception's last frames.
        The exception is never suppressed.
        """
        try:
            if exc_type is None:
                self.completed()
            elif issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
                self.cancelled(f"Interrupted: {exc_type.__name__}")
            else:
                self.error(_format_exception(exc_type, exc, tb))
        finally:
            self.close()
        return False


def _format_exception(
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    tb: TracebackType | None,
    *,
    limit: int = 3,
    max_chars: int = 1500,
) -> str:
    """A short traceback tail, suitable for a chat message."""
    if exc_type is None:
        return ""
    lines: Iterable[str] = traceback.format_exception(exc_type, exc, tb, limit=limit)
    text = "".join(lines).strip()
    return text if len(text) <= max_chars else "..." + text[-max_chars:]
