"""What each channel puts on the wire, and the protocol they both satisfy."""

from __future__ import annotations

import pytest

from runnotify.channel import Channel, ChannelError
from runnotify.channels.notion import NotionChannel
from runnotify.channels.slack import DEFAULT_EMOJI, SlackChannel
from runnotify.event import Event, Status

from .fakes import FakeOpener, FakeResponse, http_error

WEBHOOK = "https://hooks.slack.invalid/services/T/B/secret"


def event(status: Status = Status.COMPLETED, **kwargs: object) -> Event:
    defaults = {"topic": "crawl", "message": "done", "source": "host-1"}
    defaults.update(kwargs)
    return Event(status=status, **defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Protocol conformance — every channel, built-in or not, must pass this.
# --------------------------------------------------------------------------- #

CHANNELS = [
    pytest.param(SlackChannel(WEBHOOK), id="slack"),
    pytest.param(NotionChannel("tok", "db"), id="notion"),
]


@pytest.mark.parametrize("channel", CHANNELS)
def test_channel_satisfies_the_protocol(channel: Channel) -> None:
    assert isinstance(channel, Channel)
    assert isinstance(channel.name, str) and channel.name


@pytest.mark.parametrize("channel", CHANNELS)
def test_close_is_a_safe_no_op(channel: Channel) -> None:
    channel.close()
    channel.close()


@pytest.mark.parametrize("channel", CHANNELS)
def test_repr_never_reveals_a_secret(channel: Channel) -> None:
    text = repr(channel)
    assert WEBHOOK not in text
    assert "secret" not in text
    assert "tok" not in text or "***" in text


@pytest.mark.parametrize("channel", CHANNELS)
def test_min_status_filters_by_severity(channel: Channel) -> None:
    channel.min_status = Status.COMPLETED  # type: ignore[attr-defined]
    assert channel.should_deliver(event(Status.ERROR))
    assert channel.should_deliver(event(Status.COMPLETED))
    assert not channel.should_deliver(event(Status.PROGRESS))
    assert not channel.should_deliver(event(Status.RUNNING))


# --------------------------------------------------------------------------- #
# Slack
# --------------------------------------------------------------------------- #


def test_slack_requires_a_webhook() -> None:
    with pytest.raises(ValueError, match="webhook_url"):
        SlackChannel("")


def test_slack_rejects_an_unknown_style() -> None:
    with pytest.raises(ValueError, match="style"):
        SlackChannel(WEBHOOK, style="fancy")


def test_workflow_style_keeps_the_original_key_names() -> None:
    """These four keys are what an existing Slack workflow is wired to."""
    payload = SlackChannel(WEBHOOK, style="workflow").payload(event())
    assert set(payload) == {"source", "status", "topic", "details"}
    assert payload["status"] == "✅ COMPLETED"
    assert payload["topic"] == "crawl"
    assert payload["details"] == "done"
    assert payload["source"] == "host-1"


def test_text_style_sends_only_what_a_classic_webhook_reads() -> None:
    payload = SlackChannel(WEBHOOK, style="text").payload(event())
    assert set(payload) == {"text"}
    assert "COMPLETED" in payload["text"]


def test_both_style_satisfies_either_kind_of_webhook() -> None:
    payload = SlackChannel(WEBHOOK, style="both").payload(event())
    assert {"source", "status", "topic", "details", "text"} <= set(payload)


def test_every_status_has_its_own_emoji() -> None:
    """OOM in particular: it was mapped for one backend and not the other."""
    assert set(DEFAULT_EMOJI) == set(Status)
    assert len(set(DEFAULT_EMOJI.values())) == len(Status)


def test_progress_value_reaches_the_payload() -> None:
    payload = SlackChannel(WEBHOOK).payload(event(Status.PROGRESS, progress=42))
    assert payload["progress"] == 42


def test_extra_fields_are_passed_through_but_never_shadow_the_core_keys() -> None:
    payload = SlackChannel(WEBHOOK).payload(event(extra={"cell": "de", "topic": "spoofed"}))
    assert payload["cell"] == "de"
    assert payload["topic"] == "crawl"


def test_emoji_can_be_overridden_by_configuration() -> None:
    channel = SlackChannel(WEBHOOK, emoji={"error": "🔥"})
    assert channel.payload(event(Status.ERROR))["status"] == "🔥 ERROR"


def test_slack_delivers_by_posting_once(patched_http: FakeOpener) -> None:
    SlackChannel(WEBHOOK).deliver(event())
    assert len(patched_http.requests) == 1
    assert patched_http.requests[0].method == "POST"
    assert patched_http.requests[0].url == WEBHOOK


def test_slack_raises_channel_error_on_failure(patched_http: FakeOpener) -> None:
    patched_http.responses.append(http_error(400))
    with pytest.raises(ChannelError, match="slack"):
        SlackChannel(WEBHOOK, retries=0).deliver(event())


# --------------------------------------------------------------------------- #
# Notion
# --------------------------------------------------------------------------- #


def notion_query(results: list[dict] | None = None) -> FakeResponse:
    import json

    return FakeResponse(200, json.dumps({"results": results or []}).encode())


def notion_page(page_id: str = "page-1") -> FakeResponse:
    import json

    return FakeResponse(200, json.dumps({"id": page_id}).encode())


@pytest.mark.parametrize("missing", ["token", "database_id"])
def test_notion_requires_its_credentials(missing: str) -> None:
    kwargs = {"token": "t", "database_id": "d"}
    kwargs[missing] = ""
    with pytest.raises(ValueError, match=missing):
        NotionChannel(**kwargs)  # type: ignore[arg-type]


def test_first_event_creates_the_page_then_patches_it(patched_http: FakeOpener) -> None:
    patched_http.responses += [notion_query([]), notion_page("new"), FakeResponse(200)]
    NotionChannel("tok", "db").deliver(event(Status.RUNNING))
    methods = [(r.method, r.url.rsplit("/v1/", 1)[-1]) for r in patched_http.requests]
    assert methods == [
        ("POST", "databases/db/query"),
        ("POST", "pages"),
        ("PATCH", "pages/new"),
    ]


def test_an_existing_page_is_reused_not_duplicated(patched_http: FakeOpener) -> None:
    patched_http.responses += [notion_query([{"id": "found"}]), FakeResponse(200)]
    NotionChannel("tok", "db").deliver(event())
    assert [r.method for r in patched_http.requests] == ["POST", "PATCH"]
    assert patched_http.requests[-1].url.endswith("pages/found")


def test_the_page_is_looked_up_once_across_many_events(patched_http: FakeOpener) -> None:
    patched_http.responses += [notion_query([{"id": "p"}])] + [FakeResponse(200)] * 3
    channel = NotionChannel("tok", "db")
    for _ in range(3):
        channel.deliver(event(Status.PROGRESS))
    queries = [r for r in patched_http.requests if r.url.endswith("query")]
    assert len(queries) == 1


def test_started_at_is_written_once(patched_http: FakeOpener) -> None:
    patched_http.responses += [notion_query([{"id": "p"}])] + [FakeResponse(200)] * 2
    channel = NotionChannel("tok", "db")
    channel.deliver(event(Status.RUNNING))
    channel.deliver(event(Status.COMPLETED))
    patches = [r.body["properties"] for r in patched_http.requests if r.method == "PATCH"]
    assert "Started At" in patches[0]
    assert "Started At" not in patches[1]


def test_progress_leaves_the_status_property_alone(patched_http: FakeOpener) -> None:
    """A progress ping is not a state change; writing Status would flicker the row."""
    patched_http.responses += [notion_query([{"id": "p"}])] + [FakeResponse(200)] * 2
    channel = NotionChannel("tok", "db")
    channel.deliver(event(Status.RUNNING))
    channel.deliver(event(Status.PROGRESS, progress=10))
    patches = [r.body["properties"] for r in patched_http.requests if r.method == "PATCH"]
    assert patches[0]["Status"]["status"]["name"] == "Running"
    assert "Status" not in patches[1]
    assert patches[1]["Progress"]["number"] == 10


def test_oom_maps_to_its_own_status_name(patched_http: FakeOpener) -> None:
    patched_http.responses += [notion_query([{"id": "p"}]), FakeResponse(200)]
    NotionChannel("tok", "db").deliver(event(Status.OOM))
    props = patched_http.requests[-1].body["properties"]
    assert props["Status"]["status"]["name"] == "OOM"


def test_the_log_accumulates_and_keeps_the_most_recent_lines() -> None:
    """Clipped from the front: an overflowing log's interesting end is the tail."""
    channel = NotionChannel("tok", "db", log_limit=80)
    for i in range(50):
        props = channel.properties_for(event(Status.PROGRESS, message=f"line-{i}"))
    text = props["Log"]["rich_text"][0]["text"]["content"]
    assert len(text) <= 80
    assert "line-49" in text
    assert "line-0:" not in text


def test_property_names_are_configurable(patched_http: FakeOpener) -> None:
    patched_http.responses += [notion_query([{"id": "p"}]), FakeResponse(200)]
    channel = NotionChannel("tok", "db", properties={"title": "Run", "message": "Note", "log": ""})
    channel.deliver(event())
    query = patched_http.requests[0].body
    assert query["filter"]["property"] == "Run"
    props = patched_http.requests[-1].body["properties"]
    assert "Note" in props
    assert "Latest Message" not in props
    assert "Log" not in props  # disabled by an empty name


def test_status_names_are_configurable(patched_http: FakeOpener) -> None:
    patched_http.responses += [notion_query([{"id": "p"}]), FakeResponse(200)]
    NotionChannel("tok", "db", status_names={"error": "Failed"}).deliver(event(Status.ERROR))
    props = patched_http.requests[-1].body["properties"]
    assert props["Status"]["status"]["name"] == "Failed"


def test_long_values_are_clipped_to_notions_limit(patched_http: FakeOpener) -> None:
    patched_http.responses += [notion_query([{"id": "p"}]), FakeResponse(200)]
    NotionChannel("tok", "db").deliver(event(message="x" * 5000))
    props = patched_http.requests[-1].body["properties"]
    assert len(props["Latest Message"]["rich_text"][0]["text"]["content"]) == 2000


def test_notion_sends_auth_and_version_headers(patched_http: FakeOpener) -> None:
    patched_http.responses += [notion_query([{"id": "p"}]), FakeResponse(200)]
    NotionChannel("tok", "db", api_version="2022-06-28").deliver(event())
    headers = patched_http.requests[0].headers
    assert headers["authorization"] == "Bearer tok"
    assert headers["notion-version"] == "2022-06-28"


def test_notion_raises_channel_error_on_failure(patched_http: FakeOpener) -> None:
    patched_http.responses.append(http_error(401))
    with pytest.raises(ChannelError, match="notion"):
        NotionChannel("tok", "db", retries=0).deliver(event())
