# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-09-22

First release. Extracted from a single-file notifier used to report on
long-running measurement jobs, and rebuilt around a channel abstraction.

### Added

- `Notifier` with `RUNNING`, `PROGRESS`, `COMPLETED`, `CANCELLED`, `HANG`,
  `ERROR` and `OOM` statuses, progress throttling and hang detection.
- Context manager support: `RUNNING` on entry; `COMPLETED`, `CANCELLED` or
  `ERROR` with a traceback tail on exit, never suppressing the exception.
- A `Channel` protocol with three ways to reach an implementation — construct
  it, register it by name, or publish it from another distribution through the
  `runnotify.channels` entry-point group.
- Built-in Slack and Notion channels, each the only module knowing its vendor.
- Per-channel `min_status` filtering over an explicit severity ordering, so one
  channel can carry alerts and another the full log.
- Layered configuration: keyword arguments, environment variables, a TOML file
  (`runnotify.toml` or `[tool.runnotify]`), then defaults. A `$VAR` reference to
  an unset variable drops the key rather than emptying it.
- Bounded retries with exponential backoff, honouring `Retry-After`.
- An out-of-process OOM watchdog for the `SIGKILL` case `atexit` cannot observe.
- A `runnotify` command with distinct exit codes and `--dry-run`,
  `--list-channels`, `--channel` and `--version`.
- Type annotations throughout, with a `py.typed` marker.

### Fixed

Relative to the single-file predecessor this package replaces:

- Watchdog configuration is passed on stdin rather than interpolated into a
  `python -c` argument, where the webhook URL and Notion API token were readable
  through `ps` by any user on the machine.
- Terminality is read from the status itself rather than tracked by a flag that
  only the named methods set, which previously let a generic send leave the exit
  hook armed and emit a second, untrue `CANCELLED`.
- The first progress event is no longer throttled. The previous sentinel of
  `0.0` was compared against `time.monotonic()`, which is uptime-based, so on a
  recently booted host the first event was silently dropped.
- The Notion log keeps its most recent lines instead of its first ones.
- `OOM` has a Slack emoji; it was mapped for Notion only and rendered as the
  fallback pin.
- Delivery failures are logged rather than printed, so a library no longer
  writes to a caller's stdout.
- `progress` and `oom` are reachable from the command line.

[Unreleased]: https://github.com/Saiid2001/runnotify/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Saiid2001/runnotify/releases/tag/v0.1.0
