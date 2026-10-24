from __future__ import annotations

import base64
import json
import os
import socket
from pathlib import Path

import httpx
import pytest
import respx

from conftest import REPO, git
from ticket2pr.errors import ConfigurationError, PolicyViolationError, SCMError
from ticket2pr.scm import GitAuth, GitHubClient, GitRepo, LocalHost, RepoHost
from ticket2pr.scm.git import sync_tree

API = "https://api.github.com"


def test_git_auth_uses_env_config_not_argv() -> None:
    env = GitAuth(token="s3cret").env()
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    encoded = env["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")
    assert base64.b64decode(encoded).decode() == "x-access-token:s3cret"


async def test_clone_branch_stage_commit_push(repos_dir: Path, tmp_path: Path) -> None:
    origin = repos_dir / REPO
    repo = await GitRepo.clone(origin.as_uri(), tmp_path / "ws", branch="main")
    await repo.checkout_new_branch("feature/x")
    (repo.path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo.path / "new.bin").write_bytes(b"\x00\x01")

    changes = await repo.stage_all()
    assert sorted(changes.files) == ["calc.py", "new.bin"]
    assert (changes.insertions, changes.deletions) == (1, 1)  # binary counts are skipped

    await repo.unstage(["new.bin"])
    assert (await repo.staged_changes()).files == ("calc.py",)
    await repo.unstage([])

    sha = await repo.commit("msg", author_name="Bot", author_email="bot@x.test")
    assert len(sha) == 40
    git(origin, "config", "receive.denyCurrentBranch", "ignore")
    await repo.push(origin.as_uri(), "feature/x", expected_sha="")
    assert git(origin, "log", "-1", "--format=%an", "feature/x").strip() == "Bot"


async def test_clone_failure_raises_scm_error(tmp_path: Path) -> None:
    with pytest.raises(SCMError, match="git clone failed"):
        await GitRepo.clone((tmp_path / "nope").as_uri(), tmp_path / "ws", branch="main")


async def test_non_ascii_paths_are_reported_verbatim(repos_dir: Path, tmp_path: Path) -> None:
    repo = await GitRepo.clone((repos_dir / REPO).as_uri(), tmp_path / "ws", branch="main")
    (repo.path / "clé été.txt").write_text("x\n")
    (repo.path / "tab\tname.txt").write_text("y\n")
    changes = await repo.stage_all()
    assert sorted(changes.files) == ["clé été.txt", "tab\tname.txt"]
    await repo.unstage(["clé été.txt"])
    assert (await repo.staged_changes()).files == ("tab\tname.txt",)


async def test_staged_diff_includes_binary_content(repos_dir: Path, tmp_path: Path) -> None:
    repo = await GitRepo.clone((repos_dir / REPO).as_uri(), tmp_path / "ws", branch="main")
    (repo.path / "blob.bin").write_bytes(b"\x00\x01needle\x02")
    await repo.stage_all()
    assert b"needle" in await repo.staged_diff(max_bytes=1_000_000)
    with pytest.raises(PolicyViolationError, match="exceeds 10 bytes"):
        await repo.staged_diff(max_bytes=10)


async def test_remote_head_tolerates_an_empty_email(repos_dir: Path, tmp_path: Path) -> None:
    origin = repos_dir / REPO
    git(origin, "checkout", "-q", "-b", "ticket2pr/x-1")
    git(origin, "-c", "user.email=", "commit", "-q", "--allow-empty", "-m", "no email")
    git(origin, "checkout", "-q", "main")
    repo = await GitRepo.clone(origin.as_uri(), tmp_path / "ws", branch="main")
    head = await repo.remote_head(origin.as_uri(), "ticket2pr/x-1")
    assert head is not None
    assert head[1:] == ("", "")


async def test_per_user_ignore_file_is_ignored(
    repos_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A planted ~/.config/git/ignore must not hide new files from ticket2pr's commit."""
    home = tmp_path / "home"
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".config" / "git" / "ignore").write_text("*.py\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    repo = await GitRepo.clone((repos_dir / REPO).as_uri(), tmp_path / "ws", branch="main")
    (repo.path / "new_module.py").write_text("x = 1\n")
    assert (await repo.stage_all()).files == ("new_module.py",)


async def test_undecodable_names_round_trip(repos_dir: Path, tmp_path: Path) -> None:
    repo = await GitRepo.clone((repos_dir / REPO).as_uri(), tmp_path / "ws", branch="main")
    name = os.fsdecode(b"x\xff.pem")
    (repo.path / name).write_text("key\n")
    assert (await repo.stage_all()).files == (name,)
    await repo.unstage([name])
    assert (await repo.staged_changes()).empty


def test_sync_tree_drops_special_files(tmp_path: Path) -> None:
    source, dest = tmp_path / "src", tmp_path / "dest"
    source.mkdir()
    dest.mkdir()
    (source / "a.txt").write_text("a\n")
    os.mkfifo(source / "fifo")
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(source / "test.sock"))
        sync_tree(source, dest)
    assert sorted(path.name for path in dest.iterdir()) == ["a.txt"]


async def test_global_config_is_ignored(
    repos_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text(f"#!/bin/sh\ntouch {tmp_path}/ran\n")
    (hooks / "pre-commit").chmod(0o755)
    poisoned = tmp_path / "poisoned.gitconfig"
    poisoned.write_text(f"[core]\n\thooksPath = {hooks}\n\tfsmonitor = touch {tmp_path}/ran\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(poisoned))
    repo = await GitRepo.clone((repos_dir / REPO).as_uri(), tmp_path / "ws", branch="main")
    (repo.path / "x.txt").write_text("x")
    await repo.stage_all()
    await repo.commit("m", author_name="a", author_email="a@b.c")
    assert not (tmp_path / "ran").exists()


def test_sync_tree_mirrors_the_tree_but_never_git_metadata(tmp_path: Path) -> None:
    source, dest = tmp_path / "src", tmp_path / "dest"
    (source / ".git").mkdir(parents=True)
    (source / ".git" / "config").write_text("evil")
    (source / "pkg" / ".git").mkdir(parents=True)  # nested repo metadata
    (source / "pkg" / "mod.py").write_text("new")
    (source / "link").symlink_to("/etc/passwd")
    (dest / ".git").mkdir(parents=True)
    (dest / ".git" / "config").write_text("pristine")
    (dest / "deleted.py").write_text("old")
    (dest / "olddir").mkdir()
    (dest / "olddir" / "f").write_text("old")
    (dest / "oldlink").symlink_to("/etc/hosts")

    sync_tree(source, dest)

    assert (dest / ".git" / "config").read_text() == "pristine"
    assert (dest / "pkg" / "mod.py").read_text() == "new"
    assert not (dest / "pkg" / ".git").exists()
    assert (dest / "link").is_symlink()
    assert not (dest / "deleted.py").exists()
    assert not (dest / "olddir").exists()
    assert not (dest / "oldlink").exists()


async def test_exclude_keeps_files_out_of_staging(repos_dir: Path, tmp_path: Path) -> None:
    repo = await GitRepo.clone((repos_dir / REPO).as_uri(), tmp_path / "ws", branch="main")
    repo.exclude("/build/")
    (repo.path / "build").mkdir()
    (repo.path / "build" / "out.txt").write_text("x")
    assert (await repo.stage_all()).empty


@respx.mock
async def test_github_creates_pr_when_none_is_open() -> None:
    respx.get(f"{API}/repos/acme/api/pulls").respond(json=[])
    create = respx.post(f"{API}/repos/acme/api/pulls").respond(
        201, json={"number": 12, "html_url": "https://github.com/acme/api/pull/12", "draft": True}
    )
    client = GitHubClient(token="t")
    pr = await client.open_or_update_pr(
        "acme/api",
        source_repo="acme/api",
        branch="ticket2pr/x",
        base="main",
        title="T",
        body="B",
        draft=True,
    )
    assert (pr.number, pr.draft) == (12, True)
    sent = json.loads(create.calls.last.request.content)
    assert sent == {"title": "T", "head": "ticket2pr/x", "base": "main", "body": "B", "draft": True}
    assert create.calls.last.request.headers["Authorization"] == "Bearer t"
    await client.aclose()


@respx.mock
async def test_github_updates_open_pr() -> None:
    lookup = respx.get(f"{API}/repos/acme/api/pulls").respond(
        json=[{"number": 5, "node_id": "PR_5"}]
    )
    patch = respx.patch(f"{API}/repos/acme/api/pulls/5").respond(
        json={"number": 5, "html_url": "https://github.com/acme/api/pull/5", "draft": False}
    )
    pr = await GitHubClient(token="t").open_or_update_pr(
        "acme/api",
        source_repo="acme/api",
        branch="b",
        base="main",
        title="T",
        body="B",
        draft=False,
    )
    assert pr.number == 5
    assert lookup.calls.last.request.url.params["head"] == "acme:b"
    assert json.loads(patch.calls.last.request.content) == {"title": "T", "body": "B"}


@respx.mock
async def test_github_errors() -> None:
    respx.get(f"{API}/repos/acme/api/pulls").respond(403, text="forbidden")
    with pytest.raises(SCMError, match="403"):
        await GitHubClient(token="t").open_or_update_pr(
            "acme/api",
            source_repo="acme/api",
            branch="b",
            base="main",
            title="T",
            body="B",
            draft=False,
        )
    respx.get(f"{API}/repos/acme/web/pulls").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(SCMError, match="slow"):
        await GitHubClient(token="t").open_or_update_pr(
            "acme/web",
            source_repo="acme/web",
            branch="b",
            base="main",
            title="T",
            body="B",
            draft=False,
        )


def test_clone_url_for_github_and_enterprise() -> None:
    assert GitHubClient(token="t").clone_url("a/b") == "https://github.com/a/b.git"
    ghes = GitHubClient(token="t", api_url="https://git.acme.test/api/v3/")
    assert ghes.clone_url("a/b") == "https://git.acme.test/a/b.git"


@pytest.mark.parametrize(
    ("was_draft", "want_draft", "mutation"),
    [(True, False, "markPullRequestReadyForReview"), (False, True, "convertPullRequestToDraft")],
)
@respx.mock
async def test_github_syncs_draft_state_on_update(
    was_draft: bool, want_draft: bool, mutation: str
) -> None:
    respx.get(f"{API}/repos/acme/api/pulls").respond(json=[{"number": 5, "node_id": "PR_5"}])
    respx.patch(f"{API}/repos/acme/api/pulls/5").respond(
        json={"number": 5, "html_url": "https://github.com/acme/api/pull/5", "draft": was_draft}
    )
    graphql = respx.post(f"{API}/graphql").respond(json={"data": {}})
    pr = await GitHubClient(token="t").open_or_update_pr(
        "acme/api",
        source_repo="acme/api",
        branch="b",
        base="main",
        title="T",
        body="B",
        draft=want_draft,
    )
    assert pr.draft is want_draft
    sent = json.loads(graphql.calls.last.request.content)
    assert mutation in sent["query"]
    assert sent["variables"] == {"id": "PR_5"}


@respx.mock
async def test_github_falls_back_to_a_regular_pr_when_drafts_are_refused() -> None:
    respx.get(f"{API}/repos/acme/api/pulls").respond(json=[])
    create = respx.post(f"{API}/repos/acme/api/pulls")
    create.side_effect = [
        httpx.Response(422, json={"message": "Draft pull requests are not supported"}),
        httpx.Response(201, json={"number": 7, "html_url": "https://github.com/acme/api/pull/7"}),
    ]
    pr = await GitHubClient(token="t").open_or_update_pr(
        "acme/api", source_repo="acme/api", branch="b", base="main", title="T", body="B", draft=True
    )
    assert (pr.number, pr.draft) == (7, False)
    assert "draft" not in json.loads(create.calls.last.request.content)


@respx.mock
async def test_github_create_errors_other_than_draft_refusal_propagate() -> None:
    respx.get(f"{API}/repos/acme/api/pulls").respond(json=[])
    respx.post(f"{API}/repos/acme/api/pulls").respond(422, json={"message": "No commits"})
    with pytest.raises(SCMError, match="422"):
        await GitHubClient(token="t").open_or_update_pr(
            "acme/api",
            source_repo="acme/api",
            branch="b",
            base="main",
            title="T",
            body="B",
            draft=False,
        )


@respx.mock
async def test_github_reports_the_real_state_when_draft_change_fails() -> None:
    respx.get(f"{API}/repos/acme/api/pulls").respond(json=[{"number": 5, "node_id": "PR_5"}])
    respx.patch(f"{API}/repos/acme/api/pulls/5").respond(
        json={"number": 5, "html_url": "https://github.com/acme/api/pull/5", "draft": False}
    )
    respx.post(f"{API}/graphql").respond(json={"errors": [{"message": "Draft PRs unavailable"}]})
    pr = await GitHubClient(token="t").open_or_update_pr(
        "acme/api", source_repo="acme/api", branch="b", base="main", title="T", body="B", draft=True
    )
    assert pr.draft is False


@respx.mock
async def test_github_enterprise_graphql_endpoint() -> None:
    api = "https://git.acme.test/api/v3"
    respx.get(f"{api}/repos/acme/api/pulls").respond(json=[{"number": 5, "node_id": "PR_5"}])
    respx.patch(f"{api}/repos/acme/api/pulls/5").respond(
        json={"number": 5, "html_url": "https://git.acme.test/acme/api/pull/5", "draft": True}
    )
    graphql = respx.post("https://git.acme.test/api/graphql").respond(json={"data": {}})
    await GitHubClient(token="t", api_url=api).open_or_update_pr(
        "acme/api",
        source_repo="acme/api",
        branch="b",
        base="main",
        title="T",
        body="B",
        draft=False,
    )
    assert graphql.called


def test_github_requires_token() -> None:
    with pytest.raises(ConfigurationError):
        GitHubClient(token="")


@respx.mock
async def test_github_client_is_a_repo_host() -> None:
    respx.get(f"{API}/repos/acme/api/pulls").respond(json=[])
    respx.post(f"{API}/repos/acme/api/pulls").respond(
        json={"number": 1, "html_url": "https://github.com/acme/api/pull/1"}
    )
    host = GitHubClient(token="t")
    assert isinstance(host, RepoHost)
    assert host.clone_url("acme/api") == "https://github.com/acme/api.git"
    assert host.git_auth() == GitAuth(token="t")
    pr = await host.open_or_update_pr(
        "acme/api",
        source_repo="acme/api",
        branch="b",
        base="main",
        title="T",
        body="B",
        draft=False,
    )
    assert pr.url.endswith("/pull/1")
    await host.aclose()


async def test_local_host(repos_dir: Path) -> None:
    host = LocalHost(repos_dir)
    assert host.clone_url(REPO).startswith("file://")
    assert host.git_auth() is None
    with pytest.raises(SCMError):
        host.clone_url("../../etc")
    with pytest.raises(SCMError):
        host.clone_url("acme/missing")
    first = await host.open_or_update_pr(
        REPO, source_repo=REPO, branch="b1", base="main", title="T", body="B", draft=False
    )
    again = await host.open_or_update_pr(
        REPO, source_repo=REPO, branch="b1", base="main", title="T2", body="B", draft=True
    )
    assert first.url == again.url
    assert json.loads(Path(again.url.removeprefix("file://")).read_text())["title"] == "T2"
    await host.aclose()
