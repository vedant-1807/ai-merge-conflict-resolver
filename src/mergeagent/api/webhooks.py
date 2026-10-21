"""GitHub webhook receiver: verify, de-duplicate, route, enqueue. Must answer in < 10 s, so all
real work happens in the worker."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status

from mergeagent.logging import get_logger
from mergeagent.observability.metrics import WEBHOOKS

log = get_logger(__name__)
router = APIRouter()

PR_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def route_event(
    event: str, payload: dict[str, Any], command: str
) -> tuple[str, dict[str, Any]] | None:
    """Map a webhook to a ``check_pr`` invocation, or None to ignore it."""
    inst = (payload.get("installation") or {}).get("id")
    repo = (payload.get("repository") or {}).get("full_name")
    if not inst or not repo:
        return None
    if event == "pull_request" and payload.get("action") in PR_ACTIONS:
        return "check_pr", {"installation_id": inst, "repo": repo,
                            "number": payload["pull_request"]["number"], "trigger": "pull_request"}  # fmt: skip
    if event == "issue_comment" and payload.get("action") == "created":
        issue, comment = payload.get("issue", {}), payload.get("comment", {})
        if (
            "pull_request" in issue
            and comment.get("body", "").strip().startswith(command)
            and comment.get("author_association") in TRUSTED_ASSOCIATIONS
        ):
            return "check_pr", {"installation_id": inst, "repo": repo, "number": issue["number"],
                                "trigger": "comment"}  # fmt: skip
    if event == "check_run" and payload.get("action") == "rerequested":
        prs = payload.get("check_run", {}).get("pull_requests", [])
        if prs:
            return "check_pr", {"installation_id": inst, "repo": repo, "number": prs[0]["number"],
                                "trigger": "comment"}  # fmt: skip
    return None


@router.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    x_github_event: str = Header(...),
    x_github_delivery: str = Header(...),
    x_hub_signature_256: str | None = Header(None),
) -> dict[str, Any]:
    state = request.app.state
    body = await request.body()
    if not verify_signature(
        state.settings.github.webhook_secret.get_secret_value(), body, x_hub_signature_256
    ):
        WEBHOOKS.labels(x_github_event, "bad_signature").inc()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")

    # At-least-once delivery from GitHub: drop redeliveries we have already accepted.
    if not await state.redis.set(f"mergeagent:delivery:{x_github_delivery}", 1, nx=True, ex=86_400):
        WEBHOOKS.labels(x_github_event, "duplicate").inc()
        return {"queued": False, "reason": "duplicate delivery"}

    routed = route_event(x_github_event, await request.json(), state.settings.github.command)
    if routed is None:
        WEBHOOKS.labels(x_github_event, "ignored").inc()
        return {"queued": False}
    fn, kwargs = routed
    await state.arq.enqueue_job(fn, **kwargs)
    WEBHOOKS.labels(x_github_event, "queued").inc()
    log.info("webhook.queued", github_event=x_github_event, delivery=x_github_delivery, **kwargs)
    return {"queued": True}
