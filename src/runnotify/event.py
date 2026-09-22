"""The unit a notifier delivers: one status event about one run.

An :class:`Event` is what channels consume. It carries no vendor detail and no
delivery state, so the same event can be handed to every configured channel and
to the out-of-process watchdog.

:class:`Status` is a closed set. Its ``severity`` ordering is what per-channel
``min_status`` filtering compares against, and the ordering is chosen so that
``min_status="completed"`` selects exactly the terminal statuses.
"""

from __future__ import annotations

import enum
import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

__all__ = ["Event", "Status", "default_source", "utcnow"]


def utcnow() -> datetime:
    """Timezone-aware UTC now. Centralised so tests can monkeypatch one name."""
    return datetime.now(UTC)


def default_source() -> str:
    """Identifier for the machine a run is on."""
    return os.environ.get("NOTIFY_SOURCE") or socket.gethostname()


class Status(enum.Enum):
    """What happened to a run.

    ``severity`` orders the members for ``min_status`` filtering; every terminal
    status sorts at or above :attr:`COMPLETED`, so a channel configured with
    ``min_status="completed"`` receives terminal events and nothing else.
    """

    PROGRESS = "progress"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    HANG = "hang"
    ERROR = "error"
    OOM = "oom"

    @property
    def severity(self) -> int:
        return _SEVERITY[self]

    @property
    def is_terminal(self) -> bool:
        """Whether this status ends the run.

        The notifier reads this instead of tracking a separate flag, so calling
        :meth:`Notifier.notify` directly cannot leave the exit hook armed after a
        terminal status has already gone out.
        """
        return self.severity >= Status.COMPLETED.severity

    @classmethod
    def coerce(cls, value: Status | str) -> Status:
        """Accept a member or its name in any case. Raises on anything else."""
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            known = ", ".join(s.value for s in cls)
            raise ValueError(f"unknown status {value!r}; expected one of: {known}") from None

    def __str__(self) -> str:
        return self.value


_SEVERITY: dict[Status, int] = {
    Status.PROGRESS: 10,
    Status.RUNNING: 20,
    Status.COMPLETED: 30,
    Status.CANCELLED: 40,
    Status.HANG: 50,
    Status.ERROR: 60,
    Status.OOM: 70,
}


@dataclass(frozen=True, slots=True)
class Event:
    """One status event, as handed to every channel.

    ``extra`` is free-form and channel-specific: a channel that understands a key
    may use it, and one that does not must ignore it.
    """

    topic: str
    status: Status
    message: str = ""
    source: str = field(default_factory=default_source)
    timestamp: datetime = field(default_factory=utcnow)
    progress: float | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.topic:
            raise ValueError("topic must not be empty")
        object.__setattr__(self, "status", Status.coerce(self.status))

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    def to_dict(self) -> dict[str, Any]:
        """Plain-JSON form, used to hand the event to the watchdog subprocess."""
        return {
            "topic": self.topic,
            "status": self.status.value,
            "message": self.message,
            "source": self.source,
            "timestamp": self.timestamp.isoformat(),
            "progress": self.progress,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Event:
        ts = data.get("timestamp")
        return cls(
            topic=data["topic"],
            status=Status.coerce(data["status"]),
            message=data.get("message", ""),
            source=data.get("source", ""),
            timestamp=datetime.fromisoformat(ts) if ts else utcnow(),
            progress=data.get("progress"),
            extra=dict(data.get("extra") or {}),
        )
