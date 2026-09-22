# runnotify

Status reporting for long, unattended runs — to Slack, to Notion, or to a channel
you write yourself.

A job that is meant to be left alone fails in ways nobody is watching for. It
dies on item 40 of 900. It wedges on something that never returns. The kernel
kills it and leaves no traceback anywhere. Each of those ends with a short result
that looks like a result. `runnotify` tells you which one happened.

- **No dependencies.** Standard library only, on Python 3.11+.
- **Optional by construction.** With nothing configured, every call is a no-op
  and your run is unchanged. A notifier must never be the reason a job fails.
- **Covers the endings you can't catch.** An exit hook reports processes that
  end without saying so; a detached watchdog reports the ones killed with
  `SIGKILL`, which no `atexit` hook will ever see.
- **Pluggable.** Slack and Notion ship here. A channel is one small class, and a
  separate package can publish one that is discovered on install.

```bash
pip install runnotify
```

## Use

```python
from runnotify import Notifier

with Notifier("nightly-crawl") as run:
    for i, item in enumerate(items):
        process(item)
        run.ping()  # "still moving"
        run.progress(f"{i}/{len(items)}", n=i)
```

The context manager reports `RUNNING` on entry and, on exit, `COMPLETED`,
`CANCELLED` (on `KeyboardInterrupt` / `SystemExit`) or `ERROR` with the tail of
the traceback. Exceptions are never suppressed.

Without the context manager, call the statuses yourself:

```python
run = Notifier("nightly-crawl")
run.start("2,400 pages queued")
...
run.completed("2,381 pages, 19 unreachable")
run.error("stage 2 failed: connection timeout")
```

If the process ends with nothing terminal sent, an exit hook reports `CANCELLED`
rather than leaving the run looking alive forever.

### Surviving the OOM killer

`atexit` runs for every ending Python can observe. `SIGKILL` is not one of them,
and `SIGKILL` is what the Linux OOM killer sends.

```python
run = Notifier("big-embedding-job")
run.start_oom_watchdog()  # detached child, disarmed by any terminal status
```

The watchdog polls whether your process still exists and, if it disappears
without a terminal status, reports `OOM` through your own channels — including
any you added. POSIX only; on Windows it declines to arm and says so, because
the liveness probe it uses would terminate the process it is watching.

### Detecting a hang

```python
run = Notifier("crawl", hang_timeout=1800)
run.start_hang_watcher()  # background thread
...
run.ping()  # call wherever real progress happens
```

Nothing pings for `hang_timeout` seconds and a single `HANG` goes out. `ping()`
re-arms it, so one stall produces one message.

## Configure

Four layers, each overriding the one below: **keyword arguments → environment
variables → TOML file → defaults.**

```toml
# runnotify.toml  (or [tool.runnotify] in pyproject.toml)
hang_timeout = 1800
progress_interval = 300

[channels.slack]
webhook_url = "$NOTIFY_WEBHOOK_URL"    # read from the environment
min_status  = "completed"              # terminal events only — this is the alert

[channels.notion]
token       = "$NOTION_API_TOKEN"
database_id = "$NOTION_DATABASE_ID"
min_status  = "progress"               # everything — this is the log

[watchdog]
oom = true
```

A value that is exactly `$VAR` or `${VAR}` is read from the environment, and if
that variable is unset the key is **dropped** rather than set to an empty string
— a missing secret reads as *not configured*, not as configured with nothing.

`min_status` is the knob that makes two channels worth having: severity runs
`progress < running < completed < cancelled < hang < error < oom`, so
`min_status = "completed"` selects exactly the terminal statuses.

Environment variables work without any file:

| Variable | Effect |
| --- | --- |
| `NOTIFY_WEBHOOK_URL` | Slack incoming webhook |
| `NOTION_API_TOKEN`, `NOTION_DATABASE_ID` | Notion database |
| `NOTIFY_SOURCE` | Machine identifier (default: hostname) |
| `RUNNOTIFY_<CHANNEL>_<OPTION>` | Any option of any channel, e.g. `RUNNOTIFY_SLACK_MIN_STATUS` |
| `RUNNOTIFY_TOPIC`, `RUNNOTIFY_DRY_RUN`, `RUNNOTIFY_STRICT`, … | Top-level settings |

By default a channel that cannot be configured costs you that channel and
nothing else — the run starts, the other channels report, and the problem is
logged and available on `notifier.problems`. Pass `strict=True` when a missing
webhook should stop the job instead.

## From the shell

```bash
runnotify --topic nightly-crawl --status completed --message "2,381 pages"
runnotify --list-channels
runnotify --topic x --status error --dry-run -v     # build it, send nothing
```

It reads the nearest `.env` the way a run would, and names the file it read when
nothing is configured — the common case is a webhook sitting in a `.env` that
the shell never loaded.

Exit codes: `0` delivered, `1` a channel failed, `2` nothing was configured.

## Writing a channel

A channel consumes events and delivers them. That is the whole contract:

```python
from runnotify import BaseChannel, Event, Notifier, register


class ConsoleChannel(BaseChannel):
    name = "console"

    def deliver(self, event: Event) -> None:
        print(f"[{event.status}] {event.topic}: {event.message}")


# 1. pass it in directly
Notifier("my-run", channels=[ConsoleChannel()])

# 2. or register it, and select it by name in runnotify.toml
register("console", ConsoleChannel)
```

Raise on failure — the notifier isolates and records it, and a channel that
swallows its own errors reports success it did not achieve. Subclassing
`BaseChannel` gets you `min_status` filtering, a `close()` no-op and a `repr`
that redacts secrets; satisfying the `Channel` protocol directly also works.

To publish one from your own package, advertise the entry-point group:

```toml
[project.entry-points."runnotify.channels"]
discord = "runnotify_discord:DiscordChannel"
```

Installing that distribution makes `[channels.discord]` selectable. Nothing in
`runnotify` changes, and the new channel receives OOM reports from the watchdog
for free.

## Notion setup

Share a database with your integration and give it these properties. Rename any
of them via `[channels.notion.properties]`; set one to `""` to skip it.

| Property | Type | Role |
| --- | --- | --- |
| `Name` | Title | The topic |
| `Status` | Status | `Running` / `Completed` / `Error` / `Hung` / `Cancelled` / `OOM` |
| `Source` | Text | Machine |
| `Latest Message` | Text | Most recent message |
| `Log` | Text | Accumulated history |
| `Started At`, `Last Updated` | Date | |
| `Progress` | Number | |

A `PROGRESS` event deliberately leaves `Status` alone, so a progress ping does
not flicker the row out of `Running`.

## License

MIT
