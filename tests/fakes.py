"""Test doubles: a fake HTTP opener and a channel that records what it was given.

Nothing in the suite touches the network. Channels are exercised against the
opener below, so the tests assert on the payload a backend would have received
rather than on whether one was reachable.
"""

from __future__ import annotations

import io
import json
import urllib.error
import warnings
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


class WarningBody(io.BytesIO):
    """A response body that complains, like a real one, if it is never closed.

    A real ``HTTPError`` wraps the response stream — a socket, or a tempfile once
    urllib spools it — and both emit a ``ResourceWarning`` from ``__del__`` when
    collected unclosed. ``io.BytesIO`` does not, so a fake built on it makes an
    unclosed error look clean and hides exactly the leak this reproduces.
    """

    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self.was_closed = False

    def close(self) -> None:
        self.was_closed = True
        super().close()

    def __del__(self) -> None:
        if not self.was_closed:
            warnings.warn(f"Implicitly cleaning up {self!r}", ResourceWarning, stacklevel=2)


def http_error(code: int, headers: dict | None = None, body: bytes = b"boom") -> Any:
    return urllib.error.HTTPError(
        url="https://example.invalid",
        code=code,
        msg="error",
        hdrs=headers or {},  # type: ignore[arg-type]
        fp=WarningBody(body),
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
