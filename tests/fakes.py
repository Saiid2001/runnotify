"""Test doubles: a fake HTTP opener and a channel that records what it was given.

Nothing in the suite touches the network. Channels are exercised against the
opener below, so the tests assert on the payload a backend would have received
rather than on whether one was reachable.
"""

from __future__ import annotations

import io
import json
import urllib.error
from dataclasses import dataclass, field
from typing import Any

from runnotify.channel import BaseChannel
from runnotify.event import Event, Status


@dataclass
class RecordedRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: Any


class FakeResponse:
    """The subset of an ``http.client.HTTPResponse`` that ``_http`` uses."""

    def __init__(self, status: int = 200, body: bytes = b"{}", headers: dict | None = None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


@dataclass
class FakeOpener:
    """Stands in for ``urllib.request.urlopen``.

    ``responses`` is consumed in order; each entry is a :class:`FakeResponse`, an
    exception instance to raise, or a callable taking the request.
    """

    responses: list[Any] = field(default_factory=list)
    requests: list[RecordedRequest] = field(default_factory=list)

    def __call__(self, req: Any, timeout: float | None = None) -> Any:
        body = None
        if req.data:
            body = json.loads(req.data.decode("utf-8"))
        self.requests.append(
            RecordedRequest(
                method=req.get_method(),
                url=req.full_url,
                headers={k.lower(): v for k, v in req.headers.items()},
                body=body,
            )
        )
        if not self.responses:
            return FakeResponse()
        nxt = self.responses.pop(0)
        if callable(nxt) and not isinstance(nxt, FakeResponse):
            nxt = nxt(req)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    @property
    def bodies(self) -> list[Any]:
        return [r.body for r in self.requests]


def http_error(code: int, headers: dict | None = None, body: bytes = b"boom") -> Any:
    return urllib.error.HTTPError(
        url="https://example.invalid",
        code=code,
        msg="error",
        hdrs=headers or {},  # type: ignore[arg-type]
        fp=io.BytesIO(body),
    )


class RecordingChannel(BaseChannel):
    """A channel that keeps every event it was given.

    ``fail=True`` makes every delivery raise, which is how the suite checks that
    one broken channel does not stop the others.
    """

    name = "recording"

    def __init__(self, *, min_status: Status | str = Status.PROGRESS, fail: bool = False):
        super().__init__(min_status=min_status)
        self.events: list[Event] = []
        self.fail = fail
        self.closed = False

    def deliver(self, event: Event) -> None:
        if self.fail:
            raise RuntimeError("channel is broken")
        self.events.append(event)

    def close(self) -> None:
        self.closed = True

    @property
    def statuses(self) -> list[str]:
        return [e.status.value for e in self.events]
