"""A small JSON-over-HTTP client with bounded retries, built on the stdlib.

Shared by the channels so that timeout, retry and backoff behave the same
everywhere and are configured the same way. There is no third-party HTTP
dependency: a notifier is imported by every run in a study, and a dependency
here would be a dependency there.

Transient failures are retried; a request that fails is retried at most
``retries`` times with exponential backoff, and ``Retry-After`` is honoured when
the server sends one. Statuses outside :data:`RETRY_STATUSES` are final — a 401
from a bad token will not be retried, because it will not succeed.
"""

from __future__ import annotations

import contextlib
import email.utils
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["HttpError", "Response", "request"]

logger = logging.getLogger("runnotify")

#: Statuses worth trying again: rate limits, and the server-side 5xx family that
#: commonly reflects a momentary condition.
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Ceiling on any single backoff sleep, including a server's ``Retry-After``. A
#: notifier must not park a run for minutes because a backend asked it to.
MAX_BACKOFF_S = 30.0


class HttpError(RuntimeError):
    """A request that did not succeed, after any retries were exhausted."""

    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """``Retry-After`` as seconds, accepting both the numeric and date forms."""
    raw = None
    for key, value in headers.items():
        if key.lower() == "retry-after":
            raw = value
            break
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    # The header's other legal form is an HTTP date. Anything else is a server
    # sending nonsense, and the caller's own backoff is the better answer.
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, parsed.timestamp() - time.time())


def request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Any = None,
    timeout: float = 10.0,
    retries: int = 2,
    backoff: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> Response:
    """Perform one JSON request, retrying transient failures.

    ``sleep`` and ``opener`` are injectable so tests can drive the retry path
    without real time or real sockets.

    Raises :class:`HttpError` when every attempt fails.
    """
    all_headers = {"Content-Type": "application/json", **(headers or {})}
    data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
    attempts = max(1, retries + 1)
    last: HttpError | None = None

    for attempt in range(attempts):
        if attempt:
            delay = min(backoff * (2 ** (attempt - 1)), MAX_BACKOFF_S)
            if last is not None and last.status in (429, 503):
                delay = min(getattr(last, "retry_after", None) or delay, MAX_BACKOFF_S)
            logger.debug("retrying %s %s in %.1fs (attempt %d)", method, url, delay, attempt + 1)
            sleep(delay)

        req = urllib.request.Request(url, data=data, headers=dict(all_headers), method=method)
        try:
            with opener(req, timeout=timeout) as resp:
                body = resp.read()
                status = getattr(resp, "status", None) or resp.getcode()
                return Response(status=int(status), headers=dict(resp.headers), body=body)
        except urllib.error.HTTPError as exc:
            body_text = _safe_body(exc)
            last = HttpError(
                f"{method} {url} failed with HTTP {exc.code}", status=exc.code, body=body_text
            )
            last.retry_after = _retry_after_seconds(dict(exc.headers or {}))  # type: ignore[attr-defined]
            if exc.code not in RETRY_STATUSES:
                raise last from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = HttpError(f"{method} {url} failed: {exc}")

    assert last is not None  # attempts >= 1, so a failure path always set this
    raise last


def _safe_body(exc: urllib.error.HTTPError) -> str:
    """The error body, truncated, or an empty string if it cannot be read.

    Closes the error. ``HTTPError`` is itself a file-like object holding the
    response stream, and an unclosed one is a leaked connection that surfaces
    only later, as a ``ResourceWarning`` raised from a garbage collector far
    from the request that caused it.
    """
    try:
        return exc.read().decode("utf-8", "replace")[:500]
    except Exception:  # pragma: no cover - the stream may already be closed
        return ""
    finally:
        with contextlib.suppress(Exception):
            exc.close()
