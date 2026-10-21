import hashlib
import hmac
import json
from typing import Any

import fakeredis.aioredis
import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from mergeagent.api.main import create_app
from mergeagent.api.webhooks import route_event, verify_signature
from mergeagent.config import GitHubSettings, Settings
from mergeagent.github.auth import InstallationTokenProvider, app_jwt
from mergeagent.github.client import GitHubClient, GitHubIssueContext, referenced_issues
from mergeagent.github.publisher import render_body

SECRET = "whsec"


class FakeArq:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, dict[str, Any]]] = []

    async def enqueue_job(self, fn: str, **kw: Any) -> None:
        self.jobs.append((fn, kw))


def _app() -> tuple[Any, FakeArq]:
    settings = Settings(
        github=GitHubSettings(webhook_secret=SecretStr(SECRET)), api_key=SecretStr("k")
    )
    app = create_app(settings)
    arq = FakeArq()
    app.state.settings = settings
    app.state.redis = fakeredis.aioredis.FakeRedis()
    app.state.arq = arq
    return app, arq


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


PR_PAYLOAD = {
    "action": "synchronize",
    "installation": {"id": 9},
    "repository": {"full_name": "octo/app"},
    "pull_request": {"number": 12},
}


async def test_webhook_signature_dedupe_and_enqueue() -> None:
    app, arq = _app()
    body = json.dumps(PR_PAYLOAD).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        bad = await c.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "d1",
                "X-Hub-Signature-256": "sha256=00",
            },
        )
        assert bad.status_code == 401
        headers = {
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "d1",
            "X-Hub-Signature-256": _sign(body),
        }
        ok = await c.post("/webhooks/github", content=body, headers=headers)
        assert ok.status_code == 202
        assert ok.json() == {"queued": True}
        dup = await c.post("/webhooks/github", content=body, headers=headers)
        assert dup.json()["queued"] is False
    assert arq.jobs == [
        (
            "check_pr",
            {"installation_id": 9, "repo": "octo/app", "number": 12, "trigger": "pull_request"},
        )
    ]


async def test_api_key_protection_and_health() -> None:
    app, _ = _app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/healthz")).json() == {"status": "ok"}
        assert (await c.get("/api/jobs")).status_code == 401
        assert (await c.post("/api/jobs", json={"installation_id": 1, "repo": "a/b", "pr_number": 1},
                             headers={"X-API-Key": "k"})).status_code == 202  # fmt: skip
        assert "mergeagent_" in (await c.get("/metrics")).text


def test_verify_signature_and_routing() -> None:
    assert verify_signature("s", b"x", "sha256=" + hmac.new(b"s", b"x", hashlib.sha256).hexdigest())
    assert not verify_signature("", b"x", "sha256=abc")
    assert not verify_signature("s", b"x", None)
    comment = {
        "action": "created", "installation": {"id": 1}, "repository": {"full_name": "a/b"},
        "issue": {"number": 3, "pull_request": {}},
        "comment": {"body": "/resolve please", "author_association": "MEMBER"},
    }  # fmt: skip
    assert route_event("issue_comment", comment, "/resolve") == (
        "check_pr",
        {"installation_id": 1, "repo": "a/b", "number": 3, "trigger": "comment"},
    )
    comment["comment"]["author_association"] = "NONE"  # drive-by users cannot spend our budget
    assert route_event("issue_comment", comment, "/resolve") is None
    assert route_event("pull_request", {**PR_PAYLOAD, "action": "closed"}, "/resolve") is None
    assert route_event("push", {}, "/resolve") is None


def _key() -> tuple[str, Any]:
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()).decode()  # fmt: skip
    return pem, k.public_key()


@respx.mock
async def test_installation_token_cached() -> None:
    pem, pub = _key()
    decoded = jwt.decode(app_jwt(42, pem, now=1_000_000), pub, algorithms=["RS256"],
                         options={"verify_exp": False, "verify_iat": False})  # fmt: skip
    assert decoded["iss"] == "42"
    assert decoded["exp"] - decoded["iat"] <= 600
    route = respx.post("https://api.github.com/app/installations/7/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_x"})
    )
    tp = InstallationTokenProvider(42, pem, fakeredis.aioredis.FakeRedis())
    assert await tp.token(7) == "ghs_x"
    assert await tp.token(7) == "ghs_x"
    assert route.call_count == 1


@respx.mock
async def test_client_mergeability_and_issue_context() -> None:
    pulls = respx.get("https://api.github.com/repos/a/b/pulls/5").mock(
        side_effect=[
            httpx.Response(200, json={"mergeable": None}),
            httpx.Response(
                200, json={"number": 5, "mergeable": False, "title": "Fix #3", "body": "closes #4"}
            ),
        ]
    )
    respx.get("https://api.github.com/repos/a/b/issues/3").mock(
        return_value=httpx.Response(200, json={"title": "Bug"})
    )
    respx.get("https://api.github.com/repos/a/b/issues/4").mock(return_value=httpx.Response(404))
    gh = GitHubClient("t")
    pr = await gh.wait_for_mergeability("a/b", 5, delay_s=0)
    assert pr["mergeable"] is False
    assert pulls.call_count == 2
    issues = await GitHubIssueContext(gh, "a/b", pr).linked_issues()
    assert [i["number"] for i in issues] == [3]
    await gh.aclose()


def test_referenced_issues_and_body() -> None:
    assert referenced_issues("Fixes #12, see #3 and owner/repo#9; closes #12") == [12, 3]
    body = render_body({"verification": {"verdict": "pass", "gates": {"tests": True}, "reasons": ["ok"]},
                        "attempts": 2, "hunks": [{"hunk_id": "a.py#0", "category": "semantic",
                                                  "complexity": 0.4, "strategy": None}]},
                       verified=True, cost=0.0123)  # fmt: skip
    assert "passed regression verification" in body
    assert "$0.0123" in body
    assert "`a.py#0`" in body


@pytest.mark.parametrize("missing", ["installation", "repository"])
def test_route_requires_installation_and_repo(missing: str) -> None:
    payload = {k: v for k, v in PR_PAYLOAD.items() if k != missing}
    assert route_event("pull_request", payload, "/resolve") is None
