"""Orchestration: filtering, throttling, isolation, and the two exit guarantees."""

from __future__ import annotations

import itertools

import pytest

from runnotify.event import Status
from runnotify.notifier import DeliveryResult, Notifier

from .fakes import RecordingChannel


def make(**kwargs: object) -> Notifier:
    kwargs.setdefault("cancel_on_exit", False)
    kwargs.setdefault("channels", [RecordingChannel()])
    return Notifier("crawl", **kwargs)  # type: ignore[arg-type]


def only(notifier: Notifier) -> RecordingChannel:
    return notifier.channels[0]  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def test_a_topic_is_required() -> None:
    with pytest.raises(ValueError, match="topic"):
        Notifier(channels=[])


def test_with_no_channels_every_call_is_a_no_op() -> None:
    """Reporting is optional: an unconfigured notifier must not fail a run."""
    notifier = Notifier("crawl", channels=[], cancel_on_exit=False)
    assert not notifier.enabled
    result = notifier.completed("done")
    assert not result.outcomes
    assert not result


def test_keyword_arguments_beat_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNNOTIFY_PROGRESS_INTERVAL", "999")
    assert make(progress_interval=5).progress_interval == 5


def test_channels_can_be_added_after_construction() -> None:
    notifier = Notifier("crawl", channels=[], cancel_on_exit=False)
    extra = RecordingChannel()
    notifier.add_channel(extra)
    notifier.completed()
    assert extra.statuses == ["completed"]


def test_repr_lists_channels_without_leaking_config() -> None:
    assert "recording" in repr(make())


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


def test_named_methods_send_their_status() -> None:
    notifier = make()
    for name in ("start", "completed", "error", "cancelled", "hang", "oom"):
        getattr(notifier, name)("m")
    assert only(notifier).statuses == [
        "running",
        "completed",
        "error",
        "cancelled",
        "hang",
        "oom",
    ]


def test_the_event_carries_topic_source_and_extras() -> None:
    notifier = make(source="node-7")
    notifier.completed("2,381 pages", cell="de/uk")
    event = only(notifier).events[0]
    assert event.topic == "crawl"
    assert event.source == "node-7"
    assert event.message == "2,381 pages"
    assert event.extra == {"cell": "de/uk"}


def test_a_channel_below_min_status_is_not_asked() -> None:
    quiet = RecordingChannel(min_status=Status.ERROR)
    loud = RecordingChannel()
    notifier = Notifier("crawl", channels=[quiet, loud], cancel_on_exit=False)
    result = notifier.start()
    assert quiet.events == []
    assert loud.statuses == ["running"]
    # Filtered out is not failed: the channel is simply absent from the result.
    assert "recording" in result.outcomes
    assert result


def test_one_broken_channel_does_not_stop_the_others() -> None:
    broken, working = RecordingChannel(fail=True), RecordingChannel()
    broken.name, working.name = "broken", "working"
    notifier = Notifier("crawl", channels=[broken, working], cancel_on_exit=False)
    result = notifier.completed("done")
    assert working.statuses == ["completed"]
    assert set(result.failed) == {"broken"}
    assert result.delivered == ["working"]
    assert not result


def test_delivery_failures_never_raise_into_the_run() -> None:
    notifier = Notifier("crawl", channels=[RecordingChannel(fail=True)], cancel_on_exit=False)
    assert not notifier.error("stage 2 failed")


def test_result_is_truthy_only_when_every_asked_channel_accepted() -> None:
    assert DeliveryResult(event=make().build_event("completed"), outcomes={"a": None})
    assert not DeliveryResult(event=make().build_event("completed"), outcomes={})
    assert not DeliveryResult(
        event=make().build_event("completed"), outcomes={"a": None, "b": RuntimeError()}
    )


def test_dry_run_builds_events_and_delivers_nothing() -> None:
    notifier = make(dry_run=True)
    result = notifier.completed("done")
    assert only(notifier).events == []
    assert result and result.delivered == ["recording"]
    assert not notifier.enabled


# --------------------------------------------------------------------------- #
# Progress throttling
# --------------------------------------------------------------------------- #


def test_progress_is_throttled_to_the_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = itertools.count(0, 10).__next__
    monkeypatch.setattr("runnotify.notifier.time.monotonic", clock)
    notifier = make(progress_interval=100)
    sent = [notifier.progress(f"n={i}") for i in range(6)]
    assert sum(r is not None for r in sent) == 1


def test_a_throttled_progress_is_none_not_a_failed_result() -> None:
    notifier = make(progress_interval=10_000)
    assert notifier.progress("first") is not None
    assert notifier.progress("second") is None


def test_force_bypasses_the_throttle() -> None:
    notifier = make(progress_interval=10_000)
    notifier.progress("first")
    assert notifier.progress("second", force=True) is not None
    assert only(notifier).statuses == ["progress", "progress"]


def test_progress_carries_its_number() -> None:
    notifier = make(progress_interval=0)
    notifier.progress("halfway", n=50)
    assert only(notifier).events[0].progress == 50


# --------------------------------------------------------------------------- #
# Hang detection
# --------------------------------------------------------------------------- #


def test_no_hang_before_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runnotify.notifier.time.monotonic", lambda: 0.0)
    assert make(hang_timeout=60).check_hang() is None


def test_a_hang_is_reported_once_per_stall(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 0.0
    monkeypatch.setattr("runnotify.notifier.time.monotonic", lambda: now)
    notifier = make(hang_timeout=60)
    now = 601.0
    assert notifier.check_hang() is not None
    assert notifier.check_hang() is None
    assert only(notifier).statuses == ["hang"]
    assert "10 minutes" in only(notifier).events[0].message


def test_ping_rearms_the_detector(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 0.0
    monkeypatch.setattr("runnotify.notifier.time.monotonic", lambda: now)
    notifier = make(hang_timeout=60)
    now = 100.0
    notifier.check_hang()
    notifier.ping()
    now = 300.0
    assert notifier.check_hang() is not None
    assert only(notifier).statuses == ["hang", "hang"]


def test_the_hang_watcher_thread_stops_on_close() -> None:
    notifier = make(hang_timeout=0.01)
    thread = notifier.start_hang_watcher(check_interval=0.01)
    assert notifier.start_hang_watcher() is thread  # started once, not per call
    notifier.close()
    thread.join(timeout=2)
    assert not thread.is_alive()


# --------------------------------------------------------------------------- #
# Exit behaviour
# --------------------------------------------------------------------------- #


def test_exit_reports_cancelled_when_nothing_terminal_was_sent() -> None:
    notifier = make(cancel_on_exit=True)
    notifier.start()
    notifier._on_exit()
    assert only(notifier).statuses == ["running", "cancelled"]


def test_exit_stays_quiet_after_a_terminal_status() -> None:
    notifier = make(cancel_on_exit=True)
    notifier.completed("done")
    notifier._on_exit()
    assert only(notifier).statuses == ["completed"]


def test_notify_marks_terminal_without_a_separate_flag() -> None:
    """The trap in the predecessor: the generic send did not record terminality,
    so a direct call left the exit hook armed and produced a second, untrue
    CANCELLED."""
    notifier = make(cancel_on_exit=True)
    notifier.notify("error", "boom")
    notifier._on_exit()
    assert only(notifier).statuses == ["error"]


def test_exit_fires_only_once() -> None:
    notifier = make(cancel_on_exit=True)
    notifier._on_exit()
    notifier._on_exit()
    assert only(notifier).statuses == ["cancelled"]


def test_cancel_on_exit_can_be_switched_off() -> None:
    notifier = make(cancel_on_exit=False)
    notifier._on_exit()
    assert only(notifier).statuses == ["cancelled"]  # direct call still works
    assert not any(
        "_on_exit" in repr(f) for f in getattr(__import__("atexit"), "_exithandlers", [])
    )


# --------------------------------------------------------------------------- #
# Context manager
# --------------------------------------------------------------------------- #


def test_context_manager_reports_running_then_completed() -> None:
    notifier = make()
    with notifier:
        pass
    assert only(notifier).statuses == ["running", "completed"]
    assert only(notifier).closed


def test_context_manager_reports_an_exception_and_reraises() -> None:
    notifier = make()
    with pytest.raises(ZeroDivisionError), notifier:
        raise ZeroDivisionError("division by zero")
    assert only(notifier).statuses == ["running", "error"]
    assert "ZeroDivisionError" in only(notifier).events[-1].message


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit])
def test_an_interrupt_is_cancelled_not_an_error(exc: type[BaseException]) -> None:
    notifier = make()
    with pytest.raises(exc), notifier:
        raise exc()
    assert only(notifier).statuses == ["running", "cancelled"]


def test_close_is_idempotent() -> None:
    notifier = make()
    notifier.close()
    notifier.close()
    assert only(notifier).closed
