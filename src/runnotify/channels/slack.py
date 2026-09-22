"""Slack delivery: the only module that knows Slack's payload shape.

One POST per event to an incoming webhook. Stateless — nothing is remembered
between events, so a dropped delivery costs that event and nothing else.

Slack has two kinds of webhook and they want different payloads. A classic
incoming webhook renders the ``text`` field and ignores what it does not
recognise; a Workflow Builder webhook maps named top-level keys onto workflow
variables and may reject keys it was not configured for. :attr:`SlackChannel.style`
selects between them, and the default sends both so either kind works out of the
box. Pin ``style = "workflow"`` if your workflow rejects the extra key.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from .._http import HttpError, request
from ..channel import BaseChannel, ChannelError, redact
from ..event import Event, Status

__all__ = ["SlackChannel"]

#: Rendered beside the status name. Every status has one, including OOM.
DEFAULT_EMOJI: dict[Status, str] = {
    Status.PROGRESS: "📊",
    Status.RUNNING: "🚀",
    Status.COMPLETED: "✅",
    Status.CANCELLED: "⚠️",
    Status.HANG: "⏰",
    Status.ERROR: "❌",
    Status.OOM: "💀",
}

#: Accepted values for ``style``.
STYLES = ("both", "workflow", "text")


class SlackChannel(BaseChannel):
    """Post run status to a Slack incoming webhook.

    Args:
        webhook_url: The incoming webhook. Required; a channel without one
            cannot deliver and refuses to be constructed rather than failing
            silently at the first event.
        min_status: Drop events less severe than this. ``"completed"`` gives
            terminal events only, which is the usual choice when Slack is the
            alerting channel and something else holds the full log.
        style: ``"both"`` (default), ``"workflow"`` or ``"text"``.
        emoji: Overrides for individual status emoji, keyed by status name.
        username, icon_emoji, slack_channel: Optional classic-webhook overrides,
            sent only when set.
    """

    name = "slack"
    _secret_attrs = ("webhook_url",)
    _repr_attrs = ("style",)
    legacy_env: ClassVar[dict[str, str]] = {"NOTIFY_WEBHOOK_URL": "webhook_url"}

    def __init__(
        self,
        webhook_url: str,
        *,
        min_status: Status | str = Status.PROGRESS,
        style: str = "both",
        timeout: float = 10.0,
        retries: int = 2,
        backoff: float = 0.5,
        emoji: Mapping[str, str] | None = None,
        username: str | None = None,
        icon_emoji: str | None = None,
        slack_channel: str | None = None,
    ) -> None:
        super().__init__(min_status=min_status)
        if not webhook_url:
            raise ValueError("SlackChannel requires a webhook_url")
        if style not in STYLES:
            raise ValueError(f"style must be one of {STYLES}, got {style!r}")
        self.webhook_url = webhook_url
        self.style = style
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.username = username
        self.icon_emoji = icon_emoji
        self.slack_channel = slack_channel
        self.emoji = dict(DEFAULT_EMOJI)
        for key, value in (emoji or {}).items():
            self.emoji[Status.coerce(key)] = value

    def emoji_for(self, status: Status) -> str:
        return self.emoji.get(status, "📌")

    def summary(self, event: Event) -> str:
        """The one-line rendering used for the ``text`` field."""
        head = f"{self.emoji_for(event.status)} *{event.status.value.upper()}* — {event.topic}"
        if event.progress is not None:
            head += f" ({event.progress:g})"
        parts = [head, f"_{event.source}_"]
        if event.message:
            parts.insert(1, event.message)
        return "\n".join(parts)

    def payload(self, event: Event) -> dict[str, Any]:
        """The JSON body for one event."""
        body: dict[str, Any] = {}
        if self.style in ("both", "workflow"):
            body.update(
                {
                    "source": event.source,
                    "status": f"{self.emoji_for(event.status)} {event.status.value.upper()}",
                    "topic": event.topic,
                    "details": event.message,
                }
            )
            if event.progress is not None:
                body["progress"] = event.progress
        if self.style in ("both", "text"):
            body["text"] = self.summary(event)
        for key, value in (event.extra or {}).items():
            body.setdefault(key, value)
        if self.username:
            body["username"] = self.username
        if self.icon_emoji:
            body["icon_emoji"] = self.icon_emoji
        if self.slack_channel:
            body["channel"] = self.slack_channel
        return body

    def deliver(self, event: Event) -> None:
        try:
            request(
                "POST",
                self.webhook_url,
                json_body=self.payload(event),
                timeout=self.timeout,
                retries=self.retries,
                backoff=self.backoff,
            )
        except HttpError as exc:
            raise ChannelError(f"slack delivery failed: {exc}") from exc

    def __repr__(self) -> str:
        return (
            f"SlackChannel(webhook_url={redact(self.webhook_url)!r}, "
            f"style={self.style!r}, min_status={self.min_status.value!r})"
        )
