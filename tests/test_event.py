"""Status ordering and the Event value type."""

from __future__ import annotations

import pytest

from runnotify.event import Event, Status

TERMINAL = {Status.COMPLETED, Status.CANCELLED, Status.HANG, Status.ERROR, Status.OOM}


@pytest.mark.parametrize("status", list(Status))
def test_terminal_statuses_are_exactly_the_endings(status: Status) -> None:
    assert status.is_terminal is (status in TERMINAL)


def test_min_status_completed_selects_exactly_the_terminal_statuses() -> None:
    """The severity ordering exists to make this one filter correct."""
    floor = Status.COMPLETED.severity
    selected = {s for s in Status if s.severity >= floor}
    assert selected == TERMINAL


def test_severity_is_a_total_order() -> None:
    severities = [s.severity for s in Status]
    assert len(set(severities)) == len(severities)


def test_coerce_accepts_names_and_members() -> None:
    assert Status.coerce("ERROR") is Status.ERROR
    assert Status.coerce("  error ") is Status.ERROR
    assert Status.coerce(Status.OOM) is Status.OOM


def test_coerce_names_the_alternatives_when_it_fails() -> None:
    with pytest.raises(ValueError, match="completed"):
        Status.coerce("finished")


def test_event_coerces_its_status() -> None:
    assert Event(topic="t", status="hang").status is Status.HANG  # type: ignore[arg-type]


def test_event_requires_a_topic() -> None:
    with pytest.raises(ValueError, match="topic"):
        Event(topic="", status=Status.ERROR)


def test_event_round_trips_through_json_form() -> None:
    """The watchdog receives events this way, so the round trip must be lossless."""
    original = Event(
        topic="crawl",
        status=Status.OOM,
        message="killed",
        source="host",
        progress=7.5,
        extra={"cell": "de/uk"},
    )
    restored = Event.from_dict(original.to_dict())
    assert restored.topic == original.topic
    assert restored.status == original.status
    assert restored.message == original.message
    assert restored.source == original.source
    assert restored.progress == original.progress
    assert restored.extra == original.extra
    assert restored.timestamp == original.timestamp


def test_event_is_immutable() -> None:
    event = Event(topic="t", status=Status.RUNNING)
    with pytest.raises((AttributeError, TypeError)):
        event.topic = "other"  # type: ignore[misc]
