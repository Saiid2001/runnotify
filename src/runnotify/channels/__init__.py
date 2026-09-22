"""Built-in delivery channels, registered by name.

Importing this package is what makes ``slack`` and ``notion`` selectable in
configuration. Channels shipped by other distributions are found separately,
through the ``runnotify.channels`` entry-point group — see
:mod:`runnotify.channel`.
"""

from __future__ import annotations

from ..channel import register
from .notion import NotionChannel
from .slack import SlackChannel

__all__ = ["NotionChannel", "SlackChannel"]

register(SlackChannel.name, SlackChannel)
register(NotionChannel.name, NotionChannel)
