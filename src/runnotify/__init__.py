"""Status reporting for long, unattended runs.

A run that is meant to be left alone fails in ways nobody is watching for: it
dies partway, it wedges on something that never returns, or the kernel kills it
and leaves no trace. Each ends with a short result that looks like a result.
This package reports those endings to wherever you read them.

    from runnotify import Notifier

    with Notifier("nightly-crawl") as run:
        for i, item in enumerate(items):
            process(item)
            run.ping()
            run.progress(f"{i}/{len(items)}", n=i)

The context manager reports RUNNING on entry and COMPLETED, CANCELLED or ERROR
on exit. Without it, call the status methods directly; an exit hook still reports
CANCELLED if the process ends with nothing terminal sent, and
:meth:`~runnotify.notifier.Notifier.start_oom_watchdog` covers the ``SIGKILL``
case the interpreter never sees.

Delivery is pluggable. Slack and Notion ship here; anything satisfying
:class:`~runnotify.channel.Channel` can be passed to the notifier, registered by
name for configuration, or published by another distribution through the
``runnotify.channels`` entry-point group.

Reporting is optional by construction: with nothing configured, every call is a
no-op and the run is unchanged.
"""

from __future__ import annotations

import logging

__version__ = "0.1.0"

from .channel import (
    BaseChannel,
    Channel,
    ChannelError,
    available,
    build,
    build_all,
    register,
    unregister,
)
from .channels import NotionChannel, SlackChannel
from .config import Config, WatchdogConfig
from .event import Event, Status
from .notifier import DeliveryResult, Notifier

__all__ = [
    "BaseChannel",
    "Channel",
    "ChannelError",
    "Config",
    "DeliveryResult",
    "Event",
    "Notifier",
    "NotionChannel",
    "SlackChannel",
    "Status",
    "WatchdogConfig",
    "__version__",
    "available",
    "build",
    "build_all",
    "register",
    "unregister",
]

# A library configures no handlers. Without this, a host application that never
# calls logging.basicConfig sees "No handlers could be found" noise from a
# package whose entire job is to stay out of the way.
logging.getLogger("runnotify").addHandler(logging.NullHandler())
