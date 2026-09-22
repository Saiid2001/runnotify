"""The ``runnotify`` command: send one event from a shell.

Built for the two things a command line is good for here — wiring a notification
into a shell script or a job runner, and finding out why reporting is not
working. It loads a ``.env`` the way a run would, names the file it read, and
says which channel failed rather than reporting a single opaque boolean.

Exit codes: ``0`` delivered, ``1`` at least one channel failed, ``2`` nothing was
configured to deliver to, or the arguments were wrong.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from . import __version__
from ._env import find_env_file, load_env
from .channel import available
from .config import Config
from .event import Status
from .notifier import Notifier

__all__ = ["build_parser", "main"]

EXIT_OK = 0
EXIT_DELIVERY_FAILED = 1
EXIT_NOT_CONFIGURED = 2


def build_parser() -> argparse.ArgumentParser:
    # The channel list is read from the registry rather than written out, so
    # installing a plugin documents it here without an edit.
    installed = ", ".join(sorted(available())) or "none installed"
    parser = argparse.ArgumentParser(
        prog="runnotify",
        description=f"Report the status of a run. Channels available: {installed}.",
        epilog="Configuration: command line > environment > runnotify.toml > defaults.",
    )
    parser.add_argument("--version", action="version", version=f"runnotify {__version__}")
    parser.add_argument("--topic", help="Identifies the run. Required unless configured.")
    parser.add_argument(
        "--status",
        type=str.lower,
        choices=[s.value for s in Status],
        help="What to report.",
    )
    parser.add_argument("--message", default="", help="Message body.")
    parser.add_argument("--progress", type=float, default=None, help="Numeric progress value.")
    parser.add_argument("--source", default=None, help="Machine identifier (default: hostname).")
    parser.add_argument(
        "--channel",
        action="append",
        dest="channels",
        metavar="NAME",
        help="Deliver only to this channel. Repeatable.",
    )
    parser.add_argument("--config", default=None, help="TOML config file to use.")
    parser.add_argument("--env-file", default=None, help="Read this .env instead of searching.")
    parser.add_argument("--no-env", action="store_true", help="Do not read any .env file.")
    parser.add_argument("--dry-run", action="store_true", help="Build the event, deliver nothing.")
    parser.add_argument(
        "--list-channels", action="store_true", help="List selectable channels and exit."
    )
    parser.add_argument("-v", "--verbose", action="count", default=0, help="Repeatable.")
    return parser


def _configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


def _load_dotenv(args: argparse.Namespace) -> str:
    """Read the nearest ``.env``, returning the path used for diagnostics."""
    if args.no_env:
        return ""
    if args.env_file:
        return str(args.env_file) if load_env(args.env_file) is not None else ""
    found = find_env_file(Path.cwd())
    if found:
        load_env(found)
        return str(found)
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    if args.list_channels:
        for name, factory in sorted(available().items()):
            print(f"{name}\t{factory.__module__}.{factory.__qualname__}")
        return EXIT_OK

    if not args.status:
        parser.error("--status is required (or use --list-channels)")

    env_file = _load_dotenv(args)

    config = Config.load(args.config, env=os.environ)
    if args.channels:
        wanted = {c.strip().lower() for c in args.channels}
        unknown = wanted - set(config.channels)
        if unknown:
            print(
                f"runnotify: no configuration for channel(s): {', '.join(sorted(unknown))}",
                file=sys.stderr,
            )
            return EXIT_NOT_CONFIGURED
        config.channels = {k: v for k, v in config.channels.items() if k in wanted}

    try:
        notifier = Notifier(
            topic=args.topic,
            config=config,
            source=args.source,
            dry_run=args.dry_run,
            # One event, then exit: the exit hook would otherwise append a
            # second, untrue CANCELLED to every non-terminal status sent here.
            cancel_on_exit=False,
        )
    except ValueError as exc:
        parser.error(str(exc))  # raises SystemExit

    if not notifier.channels:
        print(_not_configured_message(config, env_file, notifier.problems), file=sys.stderr)
        return EXIT_NOT_CONFIGURED

    result = notifier.notify(args.status, args.message, progress=args.progress)
    notifier.close()

    if result:
        print(f"sent {args.status} for {notifier.topic!r} via {', '.join(result.delivered)}")
        return EXIT_OK

    for name, failure in result.failed.items():
        print(f"runnotify: {name} failed: {failure}", file=sys.stderr)
    if not result.outcomes:
        print(
            f"runnotify: no channel accepted a {args.status} event "
            "(check each channel's min_status)",
            file=sys.stderr,
        )
        return EXIT_NOT_CONFIGURED
    return EXIT_DELIVERY_FAILED


def _not_configured_message(config: Config, env_file: str, problems: list[str]) -> str:
    """Say what was looked at, so the next step is obvious."""
    lines = ["runnotify: no channels configured, nothing sent."]
    lines += [f"  {problem}" for problem in problems]
    lines.append(f"  config file: {config.source_file or 'none found'}")
    lines.append(f"  .env file:   {env_file or 'none found'}")
    lines.append(f"  selectable:  {', '.join(sorted(available())) or 'none'}")
    lines.append("  add a [channels.<name>] section to runnotify.toml, or see --help")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
