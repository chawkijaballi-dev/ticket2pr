"""The forge clients other than GitHub (tests/test_scm.py): GitLab, Bitbucket, Azure, Gitea."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
import respx

from ticket2pr.config import Settings
from ticket2pr.errors import ConfigurationError, SCMError
from ticket2pr.runtime import build_host
from ticket2pr.scm import (
    AzureDevOpsClient,
    BitbucketCloudClient,
    BitbucketServerClient,
    GitAuth,
    GiteaClient,
    GitHubClient,
    GitLabClient,
    RepoHost,
)

PR: dict[str, Any] = {"base": "main", "title": "T", "body": "B"}


def sent(route: respx.Route) -> Any:
    return json.loads(route.calls.last.request.content)


# ── GitLab ──────────────────────────────────────────────────────────────

GITLAB = "https://gitlab.com/api/v4"
MRS = f"{GITLAB}/projects/acme%2Fsub%2Fapi/merge_requests"


@respx.mock
async def test_gitlab_opens_a_draft_merge_request() -> None:
    lookup = respx.get(MRS).respond(json=[])
    create = respx.post(MRS).respond(
        201, json={"iid": 3, "web_url": "https://gitlab.com/x/-/merge_requests/3", "draft": True}
    )
    client = GitLabClient(token="glpat")
    pr = await client.open_or_update_pr(
        "acme/sub/api", source_repo="acme/sub/api", branch="b", draft=True, **PR
    )
    assert (pr.number, pr.draft) == (3, True)
    assert lookup.calls.last.request.url.params["source_branch"] == "b"
    assert sent(create) == {
        "source_branch": "b",
        "target_branch": "main",
        "title": "Draft: T",
        "description": "B",
    }
    assert create.calls.last.request.headers["Authorization"] == "Bearer glpat"
    assert client.clone_url("acme/sub/api") == "https://gitlab.com/acme/sub/api.git"
    assert client.git_auth() == GitAuth(token="glpat", username="oauth2")
    await client.aclose()


@respx.mock
async def test_gitlab_updates_the_open_merge_request() -> None:
    respx.get(MRS).respond(
        json=[
            {"iid": 8, "source_project_id": 66, "target_project_id": 1},  # someone's fork
            {"iid": 9, "source_project_id": 1, "target_project_id": 1},
        ]
    )
    update = respx.put(f"{MRS}/9").respond(
        json={"iid": 9, "web_url": "https://gitlab.com/x/-/merge_requests/9", "title": "T"}
    )
    pr = await GitLabClient(token="t").open_or_update_pr(
        "acme/sub/api", source_repo="acme/sub/api", branch="b", draft=False, **PR
    )
    assert (pr.number, pr.draft) == (9, False)
    assert sent(update) == {"title": "T", "description": "B"}


@respx.mock
async def test_gitlab_fork_merge_request_is_created_on_the_fork() -> None:
    respx.get(f"{GITLAB}/projects/bot%2Fapi").respond(json={"id": 77})
    respx.get(f"{GITLAB}/projects/acme%2Fsub%2Fapi").respond(json={"id": 11})
    respx.get(MRS).respond(json=[{"iid": 1, "source_project_id": 5}])  # another fork's
    create = respx.post(f"{GITLAB}/projects/77/merge_requests").respond(
        201, json={"iid": 4, "web_url": "https://gitlab.com/x/-/merge_requests/4"}
    )
    pr = await GitLabClient(token="t").open_or_update_pr(
        "acme/sub/api", source_repo="bot/api", branch="b", draft=False, **PR
    )
    assert pr.number == 4
    assert sent(create)["target_project_id"] == 11


@respx.mock
async def test_gitlab_never_takes_over_a_fork_merge_request() -> None:
    respx.get(MRS).respond(json=[{"iid": 8, "source_project_id": 66, "target_project_id": 1}])
    create = respx.post(MRS).respond(
        201, json={"iid": 10, "web_url": "https://gitlab.com/x/-/merge_requests/10"}
    )
    pr = await GitLabClient(token="t").open_or_update_pr(
        "acme/sub/api", source_repo="acme/sub/api", branch="b", draft=False, **PR
    )
    assert pr.number == 10
    assert create.called


def test_gitlab_self_managed_clone_url() -> None:
    client = GitLabClient(token="t", api_url="https://git.acme.test/api/v4")
    assert client.clone_url("a/b") == "https://git.acme.test/a/b.git"


# ── Bitbucket Cloud ─────────────────────────────────────────────────────

BB = "https://api.bitbucket.org/2.0/repositories/acme/api/pullrequests"


def bb_pr(number: int, draft: bool = False) -> dict[str, Any]:
    url = f"https://bitbucket.org/acme/api/pull-requests/{number}"
    return {"id": number, "links": {"html": {"href": url}}, "draft": draft}


@respx.mock
async def test_bitbucket_cloud_opens_a_pull_request_from_a_fork() -> None:
    lookup = respx.get(BB).respond(json={"values": []})
    create = respx.post(BB).respond(201, json=bb_pr(2, draft=True))
    client = BitbucketCloudClient(token="atok")
    pr = await client.open_or_update_pr(
        "Acme/API", source_repo="Bot/api", branch="b", draft=True, **PR
    )
    assert (pr.number, pr.draft) == (2, True)
    assert 'source.repository.full_name="bot/api"' in lookup.calls.last.request.url.params["q"]
    body = sent(create)
    assert body["source"] == {"branch": {"name": "b"}, "repository": {"full_name": "bot/api"}}
    assert (body["destination"], body["draft"]) == ({"branch": {"name": "main"}}, True)
    assert client.clone_url("acme/api") == "https://bitbucket.org/acme/api.git"
    assert client.git_auth() == GitAuth(token="atok", username="x-token-auth")


@respx.mock
async def test_bitbucket_cloud_updates_and_falls_back_when_drafts_are_refused() -> None:
    respx.get(BB).respond(json={"values": [{"id": 6}]})
    update = respx.put(f"{BB}/6")
    update.side_effect = [
        httpx.Response(400, json={"error": "draft"}),
        httpx.Response(200, json=bb_pr(6)),
    ]
    pr = await BitbucketCloudClient(token="t").open_or_update_pr(
        "acme/api", source_repo="acme/api", branch="b", draft=True, **PR
    )
    assert (pr.number, pr.draft) == (6, False)
    assert sent(update) == {"title": "T", "description": "B"}


# ── Bitbucket Data Center ───────────────────────────────────────────────

BBS = "https://bb.acme.test/rest/api/1.0"


def bbs_pr(number: int, target: str = "PROJ") -> dict[str, Any]:
    return {
        "id": number,
        "version": 3,
        "reviewers": [{"user": {"name": "alice"}, "approved": False}],
        "toRef": {
            "id": "refs/heads/main",
            "repository": {"slug": "api", "project": {"key": target}},
        },
        "links": {"self": [{"href": f"https://bb.acme.test/pr/{number}"}]},
    }


@respx.mock
async def test_bitbucket_server_updates_with_the_version() -> None:
    lookup = respx.get(f"{BBS}/projects/~BOT/repos/api/pull-requests").respond(
        json={"values": [bbs_pr(1, target="OTHER"), bbs_pr(8)]}
    )
    update = respx.put(f"{BBS}/projects/PROJ/repos/api/pull-requests/8").respond(json=bbs_pr(8))
    client = BitbucketServerClient(token="htok", api_url=BBS)
    pr = await client.open_or_update_pr(
        "PROJ/api", source_repo="~BOT/api", branch="b", draft=False, **PR
    )
    assert pr.number == 8
    assert lookup.calls.last.request.url.params["at"] == "refs/heads/b"
    assert sent(update) == {
        "title": "T",
        "description": "B",
        "draft": False,
        "version": 3,
        "reviewers": [{"user": {"name": "alice"}}],
    }
    assert client.clone_url("PROJ/api") == "https://bb.acme.test/scm/PROJ/api.git"
    assert client.git_auth() == GitAuth(token="htok", scheme="Bearer")


@respx.mock
async def test_bitbucket_server_matches_keys_and_slugs_whatever_their_case() -> None:
    respx.get(f"{BBS}/projects/proj/repos/api/pull-requests").respond(json={"values": [bbs_pr(4)]})
    update = respx.put(f"{BBS}/projects/proj/repos/api/pull-requests/4").respond(json=bbs_pr(4))
    await BitbucketServerClient(token="t", api_url=BBS).open_or_update_pr(
        "proj/api", source_repo="proj/api", branch="b", draft=False, **PR
    )
    assert update.called


@respx.mock
async def test_bitbucket_server_creates_with_refs() -> None:
    respx.get(f"{BBS}/projects/PROJ/repos/api/pull-requests").respond(json={"values": []})
    create = respx.post(f"{BBS}/projects/PROJ/repos/api/pull-requests").respond(201, json=bbs_pr(2))
    await BitbucketServerClient(token="t", api_url=BBS).open_or_update_pr(
        "PROJ/api", source_repo="PROJ/api", branch="b", draft=False, **PR
    )
    body = sent(create)
    assert body["fromRef"] == {
        "id": "refs/heads/b",
        "repository": {"slug": "api", "project": {"key": "PROJ"}},
    }
    assert body["toRef"]["id"] == "refs/heads/main"


@respx.mock
async def test_bitbucket_server_pull_request_without_link_is_an_error() -> None:
    respx.get(f"{BBS}/projects/PROJ/repos/api/pull-requests").respond(json={"values": []})
    respx.post(f"{BBS}/projects/PROJ/repos/api/pull-requests").respond(201, json={"id": 1})
    with pytest.raises(SCMError, match="without a link"):
        await BitbucketServerClient(token="t", api_url=BBS).open_or_update_pr(
            "PROJ/api", source_repo="PROJ/api", branch="b", draft=False, **PR
        )


# ── Azure DevOps ────────────────────────────────────────────────────────

AZ = "https://dev.azure.com/acme/My%20Project/_apis/git/repositories/api/pullrequests"


@respx.mock
async def test_azure_creates_a_draft_and_truncates_long_descriptions() -> None:
    lookup = respx.get(AZ).respond(json={"value": []})
    create = respx.post(AZ).respond(201, json={"pullRequestId": 5, "isDraft": True})
    client = AzureDevOpsClient(token="pat")
    pr = await client.open_or_update_pr(
        "acme/My Project/api",
        source_repo="acme/My Project/api",
        branch="b",
        draft=True,
        base="main",
        title="T",
        body="😀" * 3_000,  # two UTF-16 units each, as Azure counts
    )
    assert pr.url == "https://dev.azure.com/acme/My%20Project/_git/api/pullrequest/5"
    assert pr.draft is True
    params = lookup.calls.last.request.url.params
    assert params["searchCriteria.sourceRefName"] == "refs/heads/b"
    assert params["api-version"] == "6.0"
    body = sent(create)
    assert (body["isDraft"], body["targetRefName"]) == (True, "refs/heads/main")
    assert len(body["description"].encode("utf-16-le")) // 2 <= 4_000
    expected = base64.b64encode(b":pat").decode()
    assert create.calls.last.request.headers["Authorization"] == f"Basic {expected}"
    assert (
        client.clone_url("acme/My Project/api")
        == "https://dev.azure.com/acme/My%20Project/_git/api"
    )
    assert client.git_auth() == GitAuth(token="pat", username="")


@respx.mock
async def test_azure_updates_the_active_pull_request() -> None:
    respx.get(AZ).respond(
        json={
            "value": [{"pullRequestId": 6, "forkSource": {"repository": {}}}, {"pullRequestId": 7}]
        }
    )
    update = respx.patch(f"{AZ}/7").respond(json={"pullRequestId": 7, "isDraft": False})
    pr = await AzureDevOpsClient(token="t").open_or_update_pr(
        "acme/My Project/api", source_repo="acme/My Project/api", branch="b", draft=False, **PR
    )
    assert pr.number == 7
    assert sent(update) == {"title": "T", "description": "B", "isDraft": False}


def test_azure_devops_server_urls() -> None:
    client = AzureDevOpsClient(token="t", api_url="https://tfs.acme.test/tfs")
    assert client.clone_url("DefaultCollection/My Project/My Project") == (
        "https://tfs.acme.test/tfs/DefaultCollection/My%20Project/_git/My%20Project"
    )


# ── Gitea / Forgejo ─────────────────────────────────────────────────────

GITEA = "https://codeberg.org/api/v1"
PULLS = f"{GITEA}/repos/acme/api/pulls"


def gitea_pr(number: int, ref: str, owner: str = "acme", title: str = "T") -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "html_url": f"https://codeberg.org/acme/api/pulls/{number}",
        "head": {"ref": ref, "repo": {"full_name": f"{owner}/api"}},
        "base": {"ref": "main"},
    }


@respx.mock
async def test_gitea_pages_through_open_pulls_and_updates_the_match() -> None:
    page = respx.get(PULLS)
    page.side_effect = [
        httpx.Response(200, json=[gitea_pr(n, "other") for n in range(50)]),
        httpx.Response(200, json=[gitea_pr(51, "b", owner="bot"), gitea_pr(52, "b")]),
    ]
    update = respx.patch(f"{PULLS}/52").respond(json=gitea_pr(52, "b", title="WIP: T"))
    client = GiteaClient(token="gtok", api_url=GITEA)
    pr = await client.open_or_update_pr(
        "acme/api", source_repo="acme/api", branch="b", draft=True, **PR
    )
    assert (pr.number, pr.draft) == (52, True)
    assert sent(update) == {"title": "WIP: T", "body": "B"}
    assert update.calls.last.request.headers["Authorization"] == "token gtok"
    assert client.clone_url("acme/api") == "https://codeberg.org/acme/api.git"


@respx.mock
async def test_gitea_gives_up_after_the_page_cap() -> None:
    respx.get(PULLS).respond(json=[gitea_pr(n, "other") for n in range(50)])
    with pytest.raises(SCMError, match="more than 1000 open pull requests"):
        await GiteaClient(token="t", api_url=GITEA).open_or_update_pr(
            "acme/api", source_repo="acme/api", branch="b", draft=False, **PR
        )


@respx.mock
async def test_gitea_creates_from_a_fork() -> None:
    respx.get(PULLS).respond(json=[])
    create = respx.post(PULLS).respond(201, json=gitea_pr(3, "b", owner="bot"))
    pr = await GiteaClient(token="t", api_url=GITEA).open_or_update_pr(
        "acme/api", source_repo="bot/api", branch="b", draft=False, **PR
    )
    assert (pr.number, pr.draft) == (3, False)
    assert sent(create) == {"head": "bot:b", "base": "main", "title": "T", "body": "B"}


# ── shared ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("provider", "kind"),
    [
        ("github", GitHubClient),
        ("gitlab", GitLabClient),
        ("bitbucket", BitbucketCloudClient),
        ("bitbucket-server", BitbucketServerClient),
        ("azure", AzureDevOpsClient),
        ("gitea", GiteaClient),
    ],
)
async def test_build_host_picks_the_provider(provider: str, kind: type) -> None:
    settings = Settings(
        scm_provider=provider, scm_token="t", scm_api_url="https://forge.test/api", _env_file=None
    )
    host = build_host(settings)
    assert isinstance(host, kind)
    assert isinstance(host, RepoHost)
    await host.aclose()


def test_self_hosted_forges_need_an_api_url() -> None:
    with pytest.raises(ConfigurationError, match="T2P_SCM_API_URL"):
        build_host(Settings(scm_provider="gitea", scm_token="t", _env_file=None))
    with pytest.raises(ConfigurationError, match="T2P_SCM_TOKEN"):
        build_host(Settings(scm_provider="gitlab", _env_file=None))


def test_bearer_git_auth_sends_the_token_as_is() -> None:
    env = GitAuth(token="htok", scheme="Bearer").env()
    assert env["GIT_CONFIG_VALUE_0"] == "Authorization: Bearer htok"
