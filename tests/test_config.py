"""Which configuration layer wins, and how a missing secret reads."""

from __future__ import annotations

from pathlib import Path

import pytest

from runnotify.config import Config, find_config_file
from runnotify.notifier import Notifier


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def test_runnotify_toml_is_found_by_walking_upward(tmp_path: Path) -> None:
    write(tmp_path, "runnotify.toml", "topic = 'x'")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_config_file(nested) == tmp_path / "runnotify.toml"


def test_a_pyproject_without_our_table_does_not_shadow_a_config_above(tmp_path: Path) -> None:
    """Otherwise any project root would hide the config the user actually wrote."""
    write(tmp_path, "runnotify.toml", "topic = 'from-runnotify-toml'")
    project = tmp_path / "project"
    project.mkdir()
    write(project, "pyproject.toml", "[project]\nname = 'unrelated'")
    assert find_config_file(project) == tmp_path / "runnotify.toml"


def test_a_pyproject_tool_table_is_used(tmp_path: Path) -> None:
    write(tmp_path, "pyproject.toml", "[tool.runnotify]\ntopic = 'from-pyproject'")
    assert Config.load(search_from=tmp_path, env={}).topic == "from-pyproject"


def test_nothing_found_is_not_an_error(tmp_path: Path) -> None:
    config = Config.load(search_from=tmp_path, env={})
    assert config.channels == {}
    assert config.source_file == ""


def test_a_malformed_file_is_skipped_rather_than_fatal(tmp_path: Path) -> None:
    """A notifier must not be the reason a run fails to start."""
    path = write(tmp_path, "runnotify.toml", "this is not = = toml")
    config = Config.load(path, env={})
    assert config.channels == {}


# --------------------------------------------------------------------------- #
# File layer
# --------------------------------------------------------------------------- #


def test_channel_sections_become_channel_options(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "runnotify.toml",
        """
        hang_timeout = 1800
        [channels.slack]
        webhook_url = "https://hooks.invalid/x"
        min_status = "completed"
        """,
    )
    config = Config.load(path, env={})
    assert config.hang_timeout == 1800
    assert config.channels["slack"]["min_status"] == "completed"


def test_a_var_reference_is_read_from_the_environment(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "runnotify.toml",
        '[channels.slack]\nwebhook_url = "$HOOK"\ntoken = "${OTHER}"',
    )
    config = Config.load(path, env={"HOOK": "https://a", "OTHER": "b"})
    assert config.channels["slack"] == {"webhook_url": "https://a", "token": "b"}


def test_an_unset_var_drops_the_key_instead_of_emptying_it(tmp_path: Path) -> None:
    """A missing secret must read as *not configured*, not as configured with
    nothing — the second silently builds a channel that can never deliver."""
    path = write(tmp_path, "runnotify.toml", '[channels.slack]\nwebhook_url = "$ABSENT"')
    config = Config.load(path, env={})
    assert "webhook_url" not in config.channels["slack"]


def test_enabled_false_excludes_a_channel(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "runnotify.toml",
        '[channels.slack]\nwebhook_url = "u"\nenabled = false\n[channels.notion]\ntoken = "t"',
    )
    config = Config.load(path, env={})
    assert set(config.enabled_channels()) == {"notion"}


def test_watchdog_section(tmp_path: Path) -> None:
    path = write(tmp_path, "runnotify.toml", "[watchdog]\noom = true\npoll_interval = 2.5")
    config = Config.load(path, env={})
    assert config.watchdog.oom is True
    assert config.watchdog.poll_interval == 2.5


# --------------------------------------------------------------------------- #
# Environment layer
# --------------------------------------------------------------------------- #


def test_legacy_variables_still_configure_their_channels(tmp_path: Path) -> None:
    """An existing .env from the single-file predecessor must keep working."""
    config = Config.load(
        search_from=tmp_path,
        env={
            "NOTIFY_WEBHOOK_URL": "https://hooks.invalid/legacy",
            "NOTION_API_TOKEN": "tok",
            "NOTION_DATABASE_ID": "db",
            "NOTIFY_SOURCE": "old-host",
            "NOTIFY_PROGRESS_INTERVAL": "45",
        },
    )
    assert config.channels["slack"]["webhook_url"] == "https://hooks.invalid/legacy"
    assert config.channels["notion"] == {"token": "tok", "database_id": "db"}
    assert config.source == "old-host"
    assert config.progress_interval == 45


def test_environment_overrides_the_file(tmp_path: Path) -> None:
    path = write(tmp_path, "runnotify.toml", '[channels.slack]\nwebhook_url = "from-file"')
    config = Config.load(path, env={"NOTIFY_WEBHOOK_URL": "from-env"})
    assert config.channels["slack"]["webhook_url"] == "from-env"


def test_any_channel_option_is_reachable_from_the_environment(tmp_path: Path) -> None:
    config = Config.load(
        search_from=tmp_path,
        env={"RUNNOTIFY_SLACK_MIN_STATUS": "error", "RUNNOTIFY_SLACK_WEBHOOK_URL": "u"},
    )
    assert config.channels["slack"] == {"min_status": "error", "webhook_url": "u"}


def test_top_level_settings_from_the_environment(tmp_path: Path) -> None:
    config = Config.load(
        search_from=tmp_path,
        env={
            "RUNNOTIFY_TOPIC": "nightly",
            "RUNNOTIFY_HANG_TIMEOUT": "90",
            "RUNNOTIFY_DRY_RUN": "yes",
            "RUNNOTIFY_STRICT": "0",
        },
    )
    assert config.topic == "nightly"
    assert config.hang_timeout == 90
    assert config.dry_run is True
    assert config.strict is False


def test_unrecognised_variables_are_ignored(tmp_path: Path) -> None:
    Config.load(search_from=tmp_path, env={"RUNNOTIFY_NONSENSE_THING": "x"})


# --------------------------------------------------------------------------- #
# Reaching the notifier
# --------------------------------------------------------------------------- #


def test_a_channel_that_cannot_be_built_costs_only_that_channel() -> None:
    config = Config()
    config.topic = "crawl"
    config.channels = {"slack": {}, "notion": {"token": "t", "database_id": "d"}}
    notifier = Notifier(config=config, cancel_on_exit=False)
    assert notifier.channel_names == ["notion"]
    assert any("slack" in p for p in notifier.problems)


def test_strict_turns_a_misconfiguration_into_a_failure() -> None:
    config = Config()
    config.topic = "crawl"
    config.channels = {"slack": {}}
    # TypeError: the required webhook_url never reaches the constructor.
    with pytest.raises((TypeError, ValueError)):
        Notifier(config=config, strict=True, cancel_on_exit=False)


def test_an_unknown_channel_name_names_what_is_available() -> None:
    config = Config()
    config.topic = "crawl"
    config.channels = {"carrier-pigeon": {}}
    with pytest.raises(LookupError, match="slack"):
        Notifier(config=config, strict=True, cancel_on_exit=False)
