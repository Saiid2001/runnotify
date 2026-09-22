"""Notion delivery: the only module that knows Notion's API and page shape.

Unlike Slack, this channel is stateful. One page per topic holds the whole run:
found or created on the first event, then patched in place. The page id, the
``Started At`` latch and the accumulated log live here rather than on the
notifier, so nothing outside this module has to know that Notion behaves
differently from a fire-and-forget webhook.

Property names are configuration, not constants. A database is the user's, and
the defaults below only describe the shape this package documents; setting a
name to an empty value drops that property from the patch, which is how you
adapt to a database that does not have it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from .._http import HttpError, request
from ..channel import BaseChannel, ChannelError, redact
from ..event import Event, Status

__all__ = ["NotionChannel"]

API_ROOT = "https://api.notion.com/v1"

#: Notion caps a ``rich_text`` value at 2000 characters.
RICH_TEXT_LIMIT = 2000

#: Database property names this channel writes, by role.
DEFAULT_PROPERTIES: dict[str, str] = {
    "title": "Name",
    "source": "Source",
    "updated": "Last Updated",
    "message": "Latest Message",
    "status": "Status",
    "started": "Started At",
    "progress": "Progress",
    "log": "Log",
}

#: Status names as they appear in the database's status property.
DEFAULT_STATUS_NAMES: dict[Status, str] = {
    Status.RUNNING: "Running",
    Status.COMPLETED: "Completed",
    Status.CANCELLED: "Cancelled",
    Status.HANG: "Hung",
    Status.ERROR: "Error",
    Status.OOM: "OOM",
}


def _clip(text: str, limit: int = RICH_TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit]


class NotionChannel(BaseChannel):
    """Mirror a run onto a row of a Notion database.

    Args:
        token: Notion integration secret.
        database_id: The database to write rows into. The integration must be
            shared with it.
        min_status: Drop events less severe than this. The default keeps
            everything, which is usually right here — Notion is the durable log.
        properties: Overrides for :data:`DEFAULT_PROPERTIES`, by role. An empty
            value disables writing that property.
        status_names: Overrides for :data:`DEFAULT_STATUS_NAMES`, keyed by status
            name.
    """

    name = "notion"
    _secret_attrs = ("token",)
    legacy_env: ClassVar[dict[str, str]] = {
        "NOTION_API_TOKEN": "token",
        "NOTION_DATABASE_ID": "database_id",
    }

    def __init__(
        self,
        token: str,
        database_id: str,
        *,
        min_status: Status | str = Status.PROGRESS,
        timeout: float = 10.0,
        retries: int = 2,
        backoff: float = 0.5,
        api_version: str = "2022-06-28",
        properties: Mapping[str, str] | None = None,
        status_names: Mapping[str, str] | None = None,
        log_limit: int = RICH_TEXT_LIMIT,
    ) -> None:
        super().__init__(min_status=min_status)
        if not token:
            raise ValueError("NotionChannel requires a token")
        if not database_id:
            raise ValueError("NotionChannel requires a database_id")
        self.token = token
        self.database_id = database_id
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.api_version = api_version
        self.log_limit = log_limit
        self.properties = {**DEFAULT_PROPERTIES, **(properties or {})}
        self.status_names = dict(DEFAULT_STATUS_NAMES)
        for key, value in (status_names or {}).items():
            self.status_names[Status.coerce(key)] = value

        self._page_id: str | None = None
        self._started = False
        self._log: list[str] = []

    # -- API -------------------------------------------------------------- #

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": self.api_version,
        }

    def _call(self, method: str, endpoint: str, body: Any = None) -> Any:
        return request(
            method,
            f"{API_ROOT}/{endpoint}",
            headers=self._headers,
            json_body=body,
            timeout=self.timeout,
            retries=self.retries,
            backoff=self.backoff,
        ).json()

    def _find_or_create_page(self, event: Event) -> str:
        """The page for this topic, created if this run is the first to report."""
        title_prop = self.properties.get("title") or "Name"
        found = self._call(
            "POST",
            f"databases/{self.database_id}/query",
            {"filter": {"property": title_prop, "title": {"equals": event.topic}}},
        )
        results = (found or {}).get("results") or []
        if results:
            return str(results[0]["id"])

        created = self._call(
            "POST",
            "pages",
            {
                "parent": {"database_id": self.database_id},
                "properties": {title_prop: {"title": [{"text": {"content": _clip(event.topic)}}]}},
            },
        )
        page_id = (created or {}).get("id")
        if not page_id:
            raise ChannelError("notion did not return a page id for the new page")
        return str(page_id)

    # -- Payload ---------------------------------------------------------- #

    def _log_line(self, event: Event) -> str:
        stamp = event.timestamp.strftime("%H:%M:%S")
        label = event.status.value.upper()
        return f"[{stamp}] {label}: {event.message}" if event.message else f"[{stamp}] {label}"

    def _rendered_log(self) -> str:
        """The log, clipped to the most recent lines that fit.

        Clipped from the front: when a run overflows the limit the interesting
        lines are the last ones, and keeping the head would pin the record to the
        run's first few minutes.
        """
        text = "\n".join(self._log)
        if len(text) <= self.log_limit:
            return text
        return text[-self.log_limit :]

    def properties_for(self, event: Event) -> dict[str, Any]:
        """The ``properties`` patch for one event.

        Pure apart from the ``Started At`` latch and the log, both of which this
        channel owns.
        """
        props: dict[str, Any] = {}
        names = self.properties
        stamp = event.timestamp.isoformat()

        if names.get("source"):
            props[names["source"]] = {"rich_text": [{"text": {"content": _clip(event.source)}}]}
        if names.get("updated"):
            props[names["updated"]] = {"date": {"start": stamp}}
        if names.get("message"):
            props[names["message"]] = {"rich_text": [{"text": {"content": _clip(event.message)}}]}

        # PROGRESS deliberately leaves the status alone: a progress ping is not a
        # state change, and overwriting would flicker the row out of "Running".
        if names.get("status"):
            mapped = self.status_names.get(event.status)
            if mapped is None and not self._started:
                mapped = self.status_names.get(Status.RUNNING, "Running")
            if mapped:
                props[names["status"]] = {"status": {"name": mapped}}

        if names.get("started") and not self._started:
            props[names["started"]] = {"date": {"start": stamp}}

        if names.get("progress") and event.progress is not None:
            props[names["progress"]] = {"number": event.progress}

        self._log.append(self._log_line(event))
        if names.get("log"):
            props[names["log"]] = {"rich_text": [{"text": {"content": self._rendered_log()}}]}

        return props

    # -- Channel ---------------------------------------------------------- #

    def deliver(self, event: Event) -> None:
        try:
            if self._page_id is None:
                self._page_id = self._find_or_create_page(event)
            props = self.properties_for(event)
            self._call("PATCH", f"pages/{self._page_id}", {"properties": props})
        except HttpError as exc:
            raise ChannelError(f"notion delivery failed: {exc}") from exc
        self._started = True

    def __repr__(self) -> str:
        return (
            f"NotionChannel(token={redact(self.token)!r}, "
            f"database_id={redact(self.database_id)!r}, "
            f"min_status={self.min_status.value!r})"
        )
