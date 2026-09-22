"""The channel seam: what a delivery backend must provide, and how one is found.

A channel consumes :class:`~runnotify.event.Event` objects and delivers them
somewhere. Nothing in this module or in :mod:`runnotify.notifier` knows about
any particular service; each vendor lives in exactly one module under
:mod:`runnotify.channels` and is the only place that knows that vendor's payload
shape, endpoints and error semantics.

There are three ways to reach a channel, in ascending order of coupling:

1. **Construct it.** Any object satisfying :class:`Channel` can be passed to
   ``Notifier(channels=[...])``. Nothing needs to be registered.
2. **Register it by name.** :func:`register` puts a class in the process-wide
   registry, which is what lets configuration select it by name.
3. **Ship it in a package.** A distribution advertising the
   ``runnotify.channels`` entry-point group is discovered on install, so a
   third-party channel becomes selectable by name without any change here.

A channel must raise on failure. The notifier isolates and records failures; a
channel that swallows its own errors reports success it did not achieve.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from importlib.metadata import entry_points
from typing import Any, ClassVar, Protocol, runtime_checkable

from .event import Event, Status

__all__ = [
    "BaseChannel",
    "Channel",
    "ChannelError",
    "available",
    "build",
    "build_all",
    "get",
    "redact",
    "register",
    "unregister",
]

logger = logging.getLogger("runnotify")

#: Entry-point group a distribution advertises to publish a channel.
ENTRY_POINT_GROUP = "runnotify.channels"


class ChannelError(RuntimeError):
    """Raised by a channel when delivery fails."""


def redact(value: str | None, keep: int = 4) -> str:
    """A secret rendered for logs and reprs.

    Channels hold webhook URLs and API tokens, and those objects end up in
    tracebacks and debug output. Everything but a short tail is replaced, and the
    length is not revealed.
    """
    if not value:
        return "unset"
    tail = value[-keep:] if len(value) > keep else ""
    return f"***{tail}" if tail else "***"


@runtime_checkable
class Channel(Protocol):
    """What the notifier requires of a delivery backend.

    Implementations usually subclass :class:`BaseChannel`, which supplies
    ``should_deliver`` and ``close``; satisfying this protocol directly is also
    supported and is the lighter option for a one-off channel in a user's own
    script.
    """

    name: str

    def deliver(self, event: Event) -> None:
        """Deliver one event, or raise.

        Must not return normally unless the event was accepted by the backend.
        """
        ...

    def should_deliver(self, event: Event) -> bool:
        """Whether this channel wants this event at all."""
        ...

    def close(self) -> None:
        """Release any resources. Called once, and must tolerate being a no-op."""
        ...


class BaseChannel:
    """Convenience base: severity filtering, a redacting repr, and a no-op close.

    Subclasses set :attr:`name`, list any secret-bearing attributes in
    :attr:`_secret_attrs`, and implement :meth:`deliver`.
    """

    #: Registry key and the name used in configuration.
    name: str = "base"

    #: Attribute names whose values are secrets and must never be rendered.
    _secret_attrs: tuple[str, ...] = ()

    #: Attribute names included in ``repr`` verbatim.
    _repr_attrs: tuple[str, ...] = ()

    #: Environment variables that configure this channel under a name predating
    #: the ``RUNNOTIFY_<CHANNEL>_<OPTION>`` convention, mapped to the option they
    #: set. Declared here so the configuration layer can honour them without
    #: knowing which channel they belong to.
    legacy_env: ClassVar[dict[str, str]] = {}

    def __init__(self, *, min_status: Status | str = Status.PROGRESS) -> None:
        self.min_status = Status.coerce(min_status)

    def should_deliver(self, event: Event) -> bool:
        return event.status.severity >= self.min_status.severity

    def deliver(self, event: Event) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def close(self) -> None:
        return None

    def __repr__(self) -> str:
        parts = [f"min_status={self.min_status.value!r}"]
        parts += [f"{a}={getattr(self, a, None)!r}" for a in self._repr_attrs]
        parts += [f"{a}={redact(getattr(self, a, None))!r}" for a in self._secret_attrs]
        return f"{type(self).__name__}({', '.join(parts)})"


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

_REGISTRY: dict[str, type[Channel]] = {}
_ENTRY_POINTS_LOADED = False


def register(name: str, factory: type[Channel], *, replace: bool = False) -> None:
    """Make a channel class selectable by ``name`` in configuration.

    Refuses to shadow an existing name unless ``replace`` is set, so a plugin
    cannot silently take over a built-in channel.
    """
    key = name.strip().lower()
    if not key:
        raise ValueError("channel name must not be empty")
    if key in _REGISTRY and not replace and _REGISTRY[key] is not factory:
        raise ValueError(
            f"channel {key!r} is already registered to "
            f"{_REGISTRY[key].__module__}.{_REGISTRY[key].__qualname__}; "
            "pass replace=True to override"
        )
    _REGISTRY[key] = factory


def unregister(name: str) -> None:
    """Remove a channel from the registry. Silent if it was not there."""
    _REGISTRY.pop(name.strip().lower(), None)


def _load_entry_points() -> None:
    """Discover channels published by installed distributions.

    Runs once per process, on first lookup. A plugin that fails to import is
    logged and skipped: a broken third-party channel must not stop a run from
    reporting through the channels that do work.
    """
    global _ENTRY_POINTS_LOADED
    if _ENTRY_POINTS_LOADED:
        return
    _ENTRY_POINTS_LOADED = True
    try:
        points: Iterable[Any] = entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # pragma: no cover - importlib.metadata failure
        logger.debug("channel entry points unavailable: %s", exc)
        return
    for point in points:
        try:
            loaded = point.load()
        except Exception as exc:
            logger.warning("channel plugin %r failed to load: %s", point.name, exc)
            continue
        try:
            register(point.name, loaded)
        except ValueError as exc:
            logger.warning("channel plugin %r not registered: %s", point.name, exc)


def available() -> dict[str, type[Channel]]:
    """Every channel selectable by name, built-in and plugin alike."""
    _load_entry_points()
    return dict(_REGISTRY)


def get(name: str) -> type[Channel]:
    """The channel class registered under ``name``."""
    _load_entry_points()
    key = name.strip().lower()
    try:
        return _REGISTRY[key]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise LookupError(f"unknown channel {name!r}; available: {known}") from None


def build(name: str, options: Mapping[str, Any]) -> Channel:
    """Construct a registered channel from a configuration mapping.

    Keys are passed as keyword arguments. ``enabled`` is consumed by the caller
    and never reaches the constructor.
    """
    factory = get(name)
    kwargs = {k: v for k, v in options.items() if k != "enabled"}
    try:
        return factory(**kwargs)
    except TypeError as exc:
        raise TypeError(f"channel {name!r} rejected its configuration: {exc}") from exc


def build_all(
    sections: Mapping[str, Mapping[str, Any]], *, strict: bool = False
) -> tuple[list[Channel], list[str]]:
    """Construct every configured channel, returning the failures rather than raising.

    A channel that cannot be built is a configuration problem, and by default it
    costs you that channel and nothing more: the run still starts and the other
    channels still report. ``strict`` turns the first problem into an exception,
    which is the right setting for a deployment where a missing webhook should
    stop the job rather than quietly halve its reporting.

    Returns the channels that were built and a human-readable problem per channel
    that was not.
    """
    channels: list[Channel] = []
    problems: list[str] = []
    for name, options in sections.items():
        try:
            channels.append(build(name, options))
        except (LookupError, TypeError, ValueError) as exc:
            problem = f"channel {name!r} not configured: {exc}"
            if strict:
                raise
            logger.warning("%s", problem)
            problems.append(problem)
    return channels, problems
