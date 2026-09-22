"""The command line: exit codes, and saying enough to debug a silent webhook."""

from __future__ import annotations

from pathlib import Path

import pytest

from runnotify import __version__
from runnotify._cli import EXIT_DELIVERY_FAILED, EXIT_NOT_CONFIGURED, EXIT_OK, main
from runnotify.channel import register, unregister

from .fakes import RecordingChannel

SENT: list = []


class CliChannel(RecordingChannel):
    """Registered under a name the CLI can select, recording into SENT."""

    name = "cli"

    def __init__(self, *, fail: bool = False, min_status: str = "progress") -> None:
        super().__init__(min_status=min_status, fail=bool(fail))

    def deliver(self, event) -> None:  # type: ignore[no-untyped-def]
        if self.fail:
            raise RuntimeError("no route to host")
        SENT.append(event)


@pytest.fixture(autouse=True)
def cli_channel() -> None:
    SENT.clear()
    register("cli", CliChannel, replace=True)
    yield
    unregister("cli")


def config_file(tmp_path: Path, body: str = "[channels.cli]\n") -> str:
    path = tmp_path / "runnotify.toml"
    path.write_text(body, encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------- #


def test_version_is_reported(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--version"])
    assert caught.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_list_channels_names_the_built_ins(capsys: pytest.CaptureFixture) -> None:
    assert main(["--list-channels"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "slack" in out and "notion" in out


def test_a_successful_send_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = main(
        [
            "--topic",
            "crawl",
            "--status",
            "completed",
            "--message",
            "done",
            "--config",
            config_file(tmp_path),
            "--no-env",
        ]
    )
    assert code == EXIT_OK
    assert [e.status.value for e in SENT] == ["completed"]
    assert "completed" in capsys.readouterr().out


def test_exactly_one_event_is_sent(tmp_path: Path) -> None:
    """The exit hook must not append a second, untrue CANCELLED. The predecessor
    did, for every status sent through its generic path."""
    main(
        ["--topic", "crawl", "--status", "progress", "--config", config_file(tmp_path), "--no-env"]
    )
    assert len(SENT) == 1


def test_a_failing_channel_exits_one_and_names_itself(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    code = main(
        [
            "--topic",
            "crawl",
            "--status",
            "error",
            "--config",
            config_file(tmp_path, "[channels.cli]\nfail = true\n"),
            "--no-env",
        ]
    )
    assert code == EXIT_DELIVERY_FAILED
    err = capsys.readouterr().err
    assert "cli" in err and "no route to host" in err


def test_nothing_configured_exits_two_and_says_where_it_looked(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    code = main(["--topic", "crawl", "--status", "completed", "--no-env"])
    assert code == EXIT_NOT_CONFIGURED
    err = capsys.readouterr().err
    assert "no channels configured" in err
    assert "config file:" in err and ".env file:" in err


def test_an_unconfigured_channel_selection_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    code = main(
        [
            "--topic",
            "crawl",
            "--status",
            "completed",
            "--channel",
            "carrier-pigeon",
            "--config",
            config_file(tmp_path),
            "--no-env",
        ]
    )
    assert code == EXIT_NOT_CONFIGURED
    assert "carrier-pigeon" in capsys.readouterr().err


def test_channel_selection_narrows_delivery(tmp_path: Path) -> None:
    body = "[channels.cli]\n[channels.slack]\nwebhook_url = 'https://hooks.invalid/x'\n"
    code = main(
        [
            "--topic",
            "crawl",
            "--status",
            "completed",
            "--channel",
            "cli",
            "--config",
            config_file(tmp_path, body),
            "--no-env",
        ]
    )
    assert code == EXIT_OK
    assert len(SENT) == 1


def test_dry_run_sends_nothing_but_succeeds(tmp_path: Path) -> None:
    code = main(
        [
            "--topic",
            "crawl",
            "--status",
            "completed",
            "--dry-run",
            "--config",
            config_file(tmp_path),
            "--no-env",
        ]
    )
    assert code == EXIT_OK
    assert SENT == []


def test_a_filtered_event_is_reported_as_unconfigured_not_as_success(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Silently exiting 0 having sent nothing is the failure mode worth avoiding."""
    body = "[channels.cli]\nmin_status = 'error'\n"
    code = main(
        [
            "--topic",
            "crawl",
            "--status",
            "progress",
            "--config",
            config_file(tmp_path, body),
            "--no-env",
        ]
    )
    assert code == EXIT_NOT_CONFIGURED
    assert "min_status" in capsys.readouterr().err


def test_a_dotenv_is_read_the_way_a_run_would(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of this: a webhook in .env that the shell never loaded."""
    (tmp_path / ".env").write_text("RUNNOTIFY_CLI_MIN_STATUS=progress\n")
    monkeypatch.chdir(tmp_path)
    assert (
        main(["--topic", "crawl", "--status", "completed", "--config", config_file(tmp_path)])
        == EXIT_OK
    )
    assert len(SENT) == 1


def test_topic_may_come_from_configuration(tmp_path: Path) -> None:
    body = "topic = 'from-config'\n[channels.cli]\n"
    assert (
        main(["--status", "completed", "--config", config_file(tmp_path, body), "--no-env"])
        == EXIT_OK
    )
    assert SENT[0].topic == "from-config"


def test_status_is_required() -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--topic", "crawl"])
    assert caught.value.code == 2


def test_an_unknown_status_is_rejected() -> None:
    with pytest.raises(SystemExit):
        main(["--topic", "crawl", "--status", "finished"])


def test_every_status_is_selectable_from_the_shell(tmp_path: Path) -> None:
    """The predecessor's CLI offered four of seven; oom and progress were missing."""
    path = config_file(tmp_path)
    for status in ("running", "progress", "completed", "cancelled", "hang", "error", "oom"):
        assert main(["--topic", "t", "--status", status, "--config", path, "--no-env"]) == EXIT_OK
    assert len(SENT) == 7
