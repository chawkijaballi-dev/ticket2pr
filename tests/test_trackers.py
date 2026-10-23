from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from ticket2pr.errors import ConfigurationError, TicketNotFoundError, TrackerError
from ticket2pr.trackers import JiraTracker, LocalTracker, Tracker

BASE = "https://acme.atlassian.net"


@pytest.fixture
def jira() -> JiraTracker:
    return JiraTracker(base_url=BASE + "/", email="bot@acme.test", api_token="tok")


def test_jira_requires_credentials() -> None:
    with pytest.raises(ConfigurationError):
        JiraTracker(base_url=BASE, email="", api_token="tok")


def test_implementations_satisfy_the_protocol(jira: JiraTracker, tmp_path: Path) -> None:
    assert isinstance(jira, Tracker)
    assert isinstance(LocalTracker(tmp_path), Tracker)


@respx.mock
async def test_jira_get_ticket(jira: JiraTracker) -> None:
    route = respx.get(f"{BASE}/rest/api/3/issue/PROJ-7").respond(
        json={
            "key": "PROJ-7",
            "fields": {
                "summary": "Crash on empty cart",
                "description": {
                    "type": "doc",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "Boom"}]}
                    ],
                },
                "labels": ["ticket2pr", "backend"],
                "issuetype": {"name": "Bug"},
            },
        }
    )
    ticket = await jira.get_ticket("PROJ-7")
    assert ticket.title == "Crash on empty cart"
    assert ticket.description == "Boom"
    assert ticket.labels == ("ticket2pr", "backend")
    assert ticket.issue_type == "Bug"
    assert ticket.url == f"{BASE}/browse/PROJ-7"
    request = route.calls.last.request
    assert request.headers["Authorization"].startswith("Basic ")
    assert request.url.params["fields"] == "summary,description,labels,issuetype"
    await jira.aclose()


@respx.mock
async def test_jira_ticket_with_empty_fields(jira: JiraTracker) -> None:
    respx.get(f"{BASE}/rest/api/3/issue/PROJ-8").respond(json={"key": "PROJ-8", "fields": {}})
    ticket = await jira.get_ticket("PROJ-8")
    assert (ticket.title, ticket.description, ticket.labels, ticket.issue_type) == (
        "",
        "",
        (),
        "Task",
    )


@respx.mock
async def test_jira_not_found(jira: JiraTracker) -> None:
    respx.get(f"{BASE}/rest/api/3/issue/PROJ-9").respond(404, json={})
    with pytest.raises(TicketNotFoundError):
        await jira.get_ticket("PROJ-9")


@respx.mock
async def test_jira_server_error(jira: JiraTracker) -> None:
    respx.get(f"{BASE}/rest/api/3/issue/PROJ-9").respond(500, text="down")
    with pytest.raises(TrackerError, match="500"):
        await jira.get_ticket("PROJ-9")


@respx.mock
async def test_jira_network_error(jira: JiraTracker) -> None:
    respx.get(f"{BASE}/rest/api/3/issue/PROJ-9").mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(TrackerError, match="no route"):
        await jira.get_ticket("PROJ-9")


@respx.mock
async def test_jira_comment_posts_adf(jira: JiraTracker) -> None:
    route = respx.post(f"{BASE}/rest/api/3/issue/PROJ-7/comment").respond(201, json={"id": "1"})
    await jira.comment("PROJ-7", "Opened [PR](https://x.test/1)")
    body = json.loads(route.calls.last.request.content)
    assert body["body"]["type"] == "doc"


async def test_local_tracker_reads_yaml(tmp_path: Path) -> None:
    (tmp_path / "ABC-1.yaml").write_text(
        "title: Hello\ndescription: World\nlabels: [a, 2]\ntype: Bug\n"
    )
    ticket = await LocalTracker(tmp_path).get_ticket("ABC-1")
    assert (ticket.title, ticket.description, ticket.labels, ticket.issue_type) == (
        "Hello",
        "World",
        ("a", "2"),
        "Bug",
    )


@pytest.mark.parametrize(
    ("content", "message"),
    [("title: [unclosed", "invalid YAML"), ("- a list", "expected a mapping")],
)
async def test_local_tracker_rejects_bad_files(tmp_path: Path, content: str, message: str) -> None:
    (tmp_path / "ABC-1.yaml").write_text(content)
    with pytest.raises(TrackerError, match=message):
        await LocalTracker(tmp_path).get_ticket("ABC-1")


async def test_local_tracker_treats_empty_fields_as_empty(tmp_path: Path) -> None:
    (tmp_path / "ABC-1.yaml").write_text("title:\ndescription:\n")
    ticket = await LocalTracker(tmp_path).get_ticket("ABC-1")
    assert (ticket.title, ticket.description) == ("", "")


async def test_local_tracker_empty_file_gives_empty_ticket(tmp_path: Path) -> None:
    (tmp_path / "ABC-1.yaml").write_text("")
    assert (await LocalTracker(tmp_path).get_ticket("ABC-1")).title == ""


@pytest.mark.parametrize("key", ["ABC-404", "../etc/passwd", "abc-1"])
async def test_local_tracker_missing_or_invalid_key(tmp_path: Path, key: str) -> None:
    with pytest.raises(TicketNotFoundError):
        await LocalTracker(tmp_path).get_ticket(key)


async def test_local_tracker_appends_comments(tmp_path: Path) -> None:
    tracker = LocalTracker(tmp_path)
    await tracker.comment("ABC-1", "first")
    await tracker.comment("ABC-1", "second")
    await tracker.aclose()
    text = (tmp_path / "ABC-1.comments.md").read_text()
    assert text.index("first") < text.index("second")
