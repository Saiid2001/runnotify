"""The three ways a channel is reached, and the guard on shadowing a built-in."""

from __future__ import annotations

import pytest

from runnotify import channel as registry
from runnotify.channel import BaseChannel, build, build_all, get, register, unregister
from runnotify.event import Event


class Dummy(BaseChannel):
    name = "dummy"

    def __init__(self, *, value: str = "", min_status: str = "progress") -> None:
        super().__init__(min_status=min_status)
        self.value = value
        self.delivered: list[Event] = []

    def deliver(self, event: Event) -> None:
        self.delivered.append(event)


@pytest.fixture(autouse=True)
def clean_registry() -> None:
    yield
    unregister("dummy")
    unregister("plugin")


def test_built_ins_are_registered_on_import() -> None:
    assert {"slack", "notion"} <= set(registry.available())


def test_register_then_build_by_name() -> None:
    register("dummy", Dummy)
    built = build("dummy", {"value": "v"})
    assert isinstance(built, Dummy)
    assert built.value == "v"


def test_names_are_case_insensitive() -> None:
    register("Dummy", Dummy)
    assert get("DUMMY") is Dummy


def test_enabled_is_consumed_and_never_reaches_the_constructor() -> None:
    register("dummy", Dummy)
    assert build("dummy", {"value": "v", "enabled": True}).value == "v"


def test_registering_over_a_built_in_requires_saying_so() -> None:
    """A plugin must not be able to silently replace the Slack channel."""
    with pytest.raises(ValueError, match="already registered"):
        register("slack", Dummy)
    register("slack", Dummy, replace=True)
    assert get("slack") is Dummy
    registry.unregister("slack")
    from runnotify.channels.slack import SlackChannel

    register("slack", SlackChannel)


def test_registering_the_same_class_twice_is_harmless() -> None:
    register("dummy", Dummy)
    register("dummy", Dummy)


def test_an_unknown_name_lists_what_exists() -> None:
    with pytest.raises(LookupError) as caught:
        get("carrier-pigeon")
    assert "slack" in str(caught.value)


def test_an_empty_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        register("  ", Dummy)


def test_bad_options_name_the_channel() -> None:
    register("dummy", Dummy)
    with pytest.raises(TypeError, match="dummy"):
        build("dummy", {"nonexistent_option": 1})


def test_build_all_skips_what_it_cannot_build_and_reports_it() -> None:
    register("dummy", Dummy)
    channels, problems = build_all({"dummy": {"value": "ok"}, "slack": {}})
    assert [c.name for c in channels] == ["dummy"]
    assert len(problems) == 1 and "slack" in problems[0]


def test_build_all_strict_raises_instead() -> None:
    with pytest.raises((TypeError, ValueError)):
        build_all({"slack": {}}, strict=True)


# --------------------------------------------------------------------------- #
# Entry points — the seam that lets another distribution publish a channel.
# --------------------------------------------------------------------------- #


class FakeEntryPoint:
    def __init__(self, name: str, target: object) -> None:
        self.name = name
        self._target = target

    def load(self) -> object:
        if isinstance(self._target, Exception):
            raise self._target
        return self._target


def reset_discovery() -> None:
    registry._ENTRY_POINTS_LOADED = False


def test_a_published_channel_becomes_selectable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "entry_points", lambda group: [FakeEntryPoint("plugin", Dummy)])
    reset_discovery()
    try:
        assert get("plugin") is Dummy
    finally:
        reset_discovery()


def test_a_plugin_that_fails_to_import_does_not_break_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken third-party channel must not stop the working ones reporting."""
    monkeypatch.setattr(
        registry,
        "entry_points",
        lambda group: [
            FakeEntryPoint("broken", ImportError("no such module")),
            FakeEntryPoint("plugin", Dummy),
        ],
    )
    reset_discovery()
    try:
        names = registry.available()
        assert "plugin" in names
        assert "broken" not in names
        assert "slack" in names
    finally:
        reset_discovery()


def test_discovery_runs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake(group: str) -> list[object]:
        calls.append(group)
        return []

    monkeypatch.setattr(registry, "entry_points", fake)
    reset_discovery()
    try:
        registry.available()
        registry.available()
        assert calls == [registry.ENTRY_POINT_GROUP]
    finally:
        reset_discovery()
