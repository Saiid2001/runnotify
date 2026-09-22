# Contributing

## Setup

```bash
uv sync --group dev
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

## The constraints worth knowing before you change something

**No runtime dependencies.** This package is imported by every run in a study; a
dependency here is a dependency there, and a resolver conflict here would be one
in work that has nothing to do with notifications. Standard library only.

**No vendor knowledge outside `runnotify/channels/`.** `notifier.py`,
`channel.py`, `event.py`, `config.py`, `_http.py`, `_cli.py` and `_watchdog.py`
must not mention Slack, Notion, or any other backend outside a docstring.
`tests/test_architecture.py` enforces this by scanning the source, because the
failure it guards against is a convenience that no functional test would notice.
If the core seems to need a special case for one backend, the seam is in the
wrong place.

**A notifier must never be the reason a run fails.** Delivery failures are
caught, recorded and logged. Misconfiguration costs you that channel, not the
job, unless the caller asked for `strict`. A channel that swallows its own errors
breaks this by reporting success it did not achieve — raise instead, and let the
notifier decide.

**Secrets never reach a command line.** The watchdog receives its configuration
on stdin. Anything holding a token or webhook gets a `__repr__` that redacts it.

## Adding a channel

Most channels do not belong here — publish your own distribution advertising the
`runnotify.channels` entry-point group, and it will be discovered on install
with no change to this package. See the README.

A channel is worth adding here only if it is as widely used as Slack. If you do
add one: one module under `runnotify/channels/`, holding every literal that
backend needs, registered in `runnotify/channels/__init__.py`. It must pass the
protocol conformance tests in `tests/test_channels.py`, which run against every
built-in channel by parametrisation.

## Tests

The suite never touches the network. `tests/fakes.py` has a fake HTTP opener
that records requests, so tests assert on the payload a backend would have
received. Retry tests inject a sleep function rather than waiting.

Coverage must stay at or above 90%.

## Releasing

1. Update `__version__` in `src/runnotify/__init__.py` and add a `CHANGELOG.md`
   entry.
2. Tag `vX.Y.Z` and push the tag.

The release workflow checks that the tag matches the packaged version, builds,
and publishes through PyPI's trusted publisher. There is no API token to hold.
