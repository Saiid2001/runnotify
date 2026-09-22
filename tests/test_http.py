"""Retry, backoff and the line between transient and final failures."""

from __future__ import annotations

import pytest

from runnotify._http import MAX_BACKOFF_S, HttpError, request

from .fakes import FakeOpener, FakeResponse, http_error


def call(opener: FakeOpener, slept: list[float], **kwargs: object) -> object:
    return request(
        "POST",
        "https://example.invalid/hook",
        json_body={"a": 1},
        opener=opener,
        sleep=slept.append,
        **kwargs,  # type: ignore[arg-type]
    )


def test_a_successful_request_does_not_sleep() -> None:
    opener, slept = FakeOpener([FakeResponse(200, b'{"ok":true}')]), []
    response = call(opener, slept)
    assert response.status == 200  # type: ignore[attr-defined]
    assert response.json() == {"ok": True}  # type: ignore[attr-defined]
    assert slept == []


def test_transient_failure_is_retried_then_succeeds() -> None:
    opener = FakeOpener([http_error(503), FakeResponse(200)])
    slept: list[float] = []
    call(opener, slept, retries=2, backoff=0.5)
    assert len(opener.requests) == 2
    assert slept == [0.5]


def test_retries_are_bounded_and_the_last_error_is_raised() -> None:
    opener = FakeOpener([http_error(500), http_error(500), http_error(500)])
    slept: list[float] = []
    with pytest.raises(HttpError) as caught:
        call(opener, slept, retries=2, backoff=0.5)
    assert caught.value.status == 500
    assert len(opener.requests) == 3
    assert slept == [0.5, 1.0]


def test_a_client_error_is_final_and_not_retried() -> None:
    """A 401 will not start working; retrying it only delays the report."""
    opener = FakeOpener([http_error(401)])
    slept: list[float] = []
    with pytest.raises(HttpError) as caught:
        call(opener, slept, retries=3)
    assert caught.value.status == 401
    assert len(opener.requests) == 1
    assert slept == []


def test_retry_after_overrides_the_backoff() -> None:
    opener = FakeOpener([http_error(429, {"Retry-After": "7"}), FakeResponse(200)])
    slept: list[float] = []
    call(opener, slept, retries=2, backoff=0.5)
    assert slept == [7.0]


def test_retry_after_is_capped() -> None:
    """A backend must not be able to park a run for an hour."""
    opener = FakeOpener([http_error(429, {"Retry-After": "3600"}), FakeResponse(200)])
    slept: list[float] = []
    call(opener, slept, retries=2)
    assert slept == [MAX_BACKOFF_S]


def test_network_errors_are_retried() -> None:
    opener = FakeOpener([OSError("connection reset"), FakeResponse(200)])
    slept: list[float] = []
    call(opener, slept, retries=1, backoff=0.25)
    assert len(opener.requests) == 2


def test_content_type_and_custom_headers_are_sent() -> None:
    opener = FakeOpener([FakeResponse(200)])
    request(
        "PATCH",
        "https://example.invalid/x",
        headers={"Authorization": "Bearer tok"},
        json_body={"k": "v"},
        opener=opener,
        sleep=lambda _s: None,
    )
    sent = opener.requests[0]
    assert sent.method == "PATCH"
    assert sent.headers["content-type"] == "application/json"
    assert sent.headers["authorization"] == "Bearer tok"
    assert sent.body == {"k": "v"}


def test_an_error_response_is_closed_not_leaked(recwarn: pytest.WarningsRecorder) -> None:
    """HTTPError is a file-like object holding the response stream.

    Leaving it open leaks a connection, and the ResourceWarning surfaces from a
    garbage collector far from the request that caused it — on one interpreter
    and not another, which is how this reached CI green on Linux and failed on
    macOS.
    """
    import gc

    opener = FakeOpener([http_error(500), http_error(500), FakeResponse(200)])
    call(opener, [], retries=2)
    gc.collect()
    leaks = [w for w in recwarn if issubclass(w.category, ResourceWarning)]
    assert not leaks, [str(w.message) for w in leaks]


def test_a_final_error_is_also_closed(recwarn: pytest.WarningsRecorder) -> None:
    import gc

    opener = FakeOpener([http_error(401)])
    with pytest.raises(HttpError):
        call(opener, [], retries=0)
    gc.collect()
    assert not [w for w in recwarn if issubclass(w.category, ResourceWarning)]
