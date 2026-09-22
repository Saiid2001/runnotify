"""Where a notifier's settings come from, and which source wins.

Four layers, each overriding the one below:

1. keyword arguments to :class:`~runnotify.notifier.Notifier`
2. environment variables
3. a TOML file — ``runnotify.toml``, or ``[tool.runnotify]`` in ``pyproject.toml``
4. the defaults in this module

Layers 2 to 4 are resolved here into a :class:`Config`; layer 1 is applied by the
notifier, which is the only thing that knows what its caller passed.

A string in the TOML file consisting solely of ``$VAR`` or ``${VAR}`` is read
from the environment. If that variable is unset the key is dropped rather than
resolved to an empty string, so a secret that is not present reads as *not
configured* instead of configured with nothing.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .channel import available
from .event import default_source

__all__ = ["Config", "WatchdogConfig", "find_config_file"]

logger = logging.getLogger("runnotify")

#: Searched, in order, at each directory from the start point upward.
CONFIG_FILENAMES = ("runnotify.toml", ".runnotify.toml", "pyproject.toml")

_VAR_ONLY = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?$")

#: Top-level settings that predate the ``RUNNOTIFY_`` prefix. Channel-specific
#: legacy names are not listed here — each channel declares its own through
#: :attr:`~runnotify.channel.BaseChannel.legacy_env`, so this module needs to
#: know nothing about any particular backend.
LEGACY_ENV = {
    "NOTIFY_SOURCE": "source",
    "NOTIFY_PROGRESS_INTERVAL": "progress_interval",
}

#: Top-level settings accepted as ``RUNNOTIFY_<NAME>``, with their parser.
TOP_LEVEL_ENV: dict[str, str] = {
    "TOPIC": "topic",
    "SOURCE": "source",
    "HANG_TIMEOUT": "hang_timeout",
    "PROGRESS_INTERVAL": "progress_interval",
    "CANCEL_ON_EXIT": "cancel_on_exit",
    "DRY_RUN": "dry_run",
    "STRICT": "strict",
}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"expected a boolean, got {value!r}")


def _as_float(value: Any) -> float:
    return float(value)


_COERCE = {
    "hang_timeout": _as_float,
    "progress_interval": _as_float,
    "cancel_on_exit": _as_bool,
    "dry_run": _as_bool,
    "strict": _as_bool,
}


@dataclass(slots=True)
class WatchdogConfig:
    """Settings for the out-of-process OOM watchdog."""

    oom: bool = False
    poll_interval: float = 5.0


@dataclass(slots=True)
class Config:
    """Resolved settings for one notifier."""

    topic: str = ""
    source: str = field(default_factory=default_source)
    hang_timeout: float = 3600.0
    progress_interval: float = 300.0
    cancel_on_exit: bool = True
    dry_run: bool = False
    strict: bool = False
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)
    watchdog: WatchdogConfig = field(default_factory=WatchdogConfig)

    #: Path the TOML layer was read from, for diagnostics. Empty if none.
    source_file: str = ""

    @classmethod
    def load(
        cls,
        path: Path | str | None = None,
        *,
        env: Mapping[str, str] | None = None,
        search_from: Path | str | None = None,
    ) -> Config:
        """Resolve the file and environment layers into a config."""
        environ = os.environ if env is None else env
        config = cls()

        file_path = Path(path) if path else find_config_file(search_from)
        if file_path:
            data = _read_toml(file_path)
            if data:
                _apply_mapping(config, data, environ)
                config.source_file = str(file_path)

        _apply_env(config, environ)
        return config

    def channel_options(self, name: str) -> dict[str, Any]:
        return dict(self.channels.get(name, {}))

    def enabled_channels(self) -> dict[str, dict[str, Any]]:
        """Channel sections that are not explicitly disabled."""
        return {
            name: options for name, options in self.channels.items() if options.get("enabled", True)
        }


def find_config_file(start: Path | str | None = None) -> Path | None:
    """The nearest configuration file at or above ``start``.

    ``pyproject.toml`` counts only when it actually carries a ``[tool.runnotify]``
    table, so an unrelated project root does not shadow a ``runnotify.toml``
    further up.
    """
    current = Path(start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        for filename in CONFIG_FILENAMES:
            candidate = directory / filename
            if not candidate.is_file():
                continue
            if filename == "pyproject.toml" and not _has_tool_table(candidate):
                continue
            return candidate
    return None


def _has_tool_table(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return "runnotify" in (tomllib.load(handle).get("tool") or {})
    except (OSError, tomllib.TOMLDecodeError):
        return False


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        logger.warning("could not read config %s: %s", path, exc)
        return {}
    except tomllib.TOMLDecodeError as exc:
        logger.warning("could not parse config %s: %s", path, exc)
        return {}
    if path.name == "pyproject.toml":
        return dict((data.get("tool") or {}).get("runnotify") or {})
    return data


def _expand(value: Any, environ: Mapping[str, str]) -> Any:
    """Resolve a whole-string ``$VAR`` reference. Returns None when unset."""
    if not isinstance(value, str):
        return value
    match = _VAR_ONLY.match(value.strip())
    if not match:
        return value
    resolved = environ.get(match.group(1))
    if resolved is None:
        logger.debug("config references unset variable %s", match.group(1))
        return None
    return resolved


def _apply_mapping(config: Config, data: Mapping[str, Any], environ: Mapping[str, str]) -> None:
    for key, raw in data.items():
        if key == "channels":
            if not isinstance(raw, Mapping):
                logger.warning("config: [channels] must be a table, ignoring")
                continue
            for name, options in raw.items():
                if not isinstance(options, Mapping):
                    logger.warning("config: [channels.%s] must be a table, ignoring", name)
                    continue
                section = config.channels.setdefault(name.strip().lower(), {})
                for opt, value in options.items():
                    expanded = _expand(value, environ)
                    if expanded is None:
                        section.pop(opt, None)
                        continue
                    section[opt] = expanded
        elif key == "watchdog":
            if not isinstance(raw, Mapping):
                logger.warning("config: [watchdog] must be a table, ignoring")
                continue
            for opt, value in raw.items():
                if opt == "oom":
                    config.watchdog.oom = _as_bool(value)
                elif opt == "poll_interval":
                    config.watchdog.poll_interval = _as_float(value)
                else:
                    logger.warning("config: unknown watchdog option %r", opt)
        elif hasattr(config, key) and key not in ("channels", "watchdog", "source_file"):
            expanded = _expand(raw, environ)
            if expanded is None:
                continue
            coerce = _COERCE.get(key)
            setattr(config, key, coerce(expanded) if coerce else expanded)
        else:
            logger.warning("config: unknown setting %r", key)


def _apply_env(config: Config, environ: Mapping[str, str]) -> None:
    """Overlay environment variables onto ``config``."""
    for var, attr in LEGACY_ENV.items():
        value = environ.get(var)
        if value:
            _set_attr(config, attr, value)

    registered = available()
    for name, factory in registered.items():
        for var, option in getattr(factory, "legacy_env", {}).items():
            value = environ.get(var)
            if value:
                config.channels.setdefault(name, {})[option] = value

    known = {name.upper() for name in registered} | {name.upper() for name in config.channels}

    for var, value in environ.items():
        if not var.startswith("RUNNOTIFY_") or not value:
            continue
        rest = var[len("RUNNOTIFY_") :]
        if rest in TOP_LEVEL_ENV:
            _set_attr(config, TOP_LEVEL_ENV[rest], value)
            continue
        if rest == "OOM_WATCHDOG":
            config.watchdog.oom = _as_bool(value)
            continue
        if rest == "WATCHDOG_POLL_INTERVAL":
            config.watchdog.poll_interval = _as_float(value)
            continue
        if rest in ("CONFIG", "ENV_FILE"):  # consumed by the CLI
            continue
        channel, sep, option = rest.partition("_")
        if sep and channel in known:
            section = config.channels.setdefault(channel.lower(), {})
            section[option.lower()] = value
        else:
            logger.debug("ignoring unrecognised environment variable %s", var)


def _set_attr(config: Config, attr: str, value: str) -> None:
    coerce = _COERCE.get(attr)
    setattr(config, attr, coerce(value) if coerce else value)
