"""Publish results back to GitHub: draft PR + check run, or an explanatory comment."""

from __future__ import annotations

from typing import Any

from mergeagent.agents.publisher import PublishResult
from mergeagent.github.client import GitHubClient
from mergeagent.mcp_servers.context import JobContext


def _table(summary: dict[str, Any]) -> str:
    rows = ["| Hunk | Category | Complexity | Strategy |", "|---|---|---|---|"]
    for h in summary.get("hunks", []):
        rows.append(
            f"| `{h['hunk_id']}` | {h['category']} | {h['complexity']:.2f} | {h['strategy'] or 'llm'} |"
        )
    return "\n".join(rows)


def render_body(summary: dict[str, Any], *, verified: bool, cost: float | None) -> str:
    v = summary.get("verification") or {}
    gates = (
        " · ".join(f"{k}: {'✅' if ok else '❌'}" for k, ok in (v.get("gates") or {}).items())
        or "n/a"
    )
    review = summary.get("review") or {}
    lines = [
        "## 🤖 MergeAgent conflict resolution",
        "",
        ("This resolution **passed regression verification** against both parent branches."
         if verified else "⚠️ No verified resolution was found. The best candidate is attached for a human to finish."),
        "",
        f"**Verification:** {v.get('verdict', 'n/a')} — {gates}",
        f"**Attempts:** {summary.get('attempts', 1)}" + (f" · **LLM cost:** ${cost:.4f}" if cost is not None else ""),
    ]  # fmt: skip
    for reason in v.get("reasons", []):
        lines.append(f"- {reason}")
    if review.get("concerns"):
        lines += ["", "**Reviewer notes:**"] + [f"- {c}" for c in review["concerns"]]
    lines += ["", "<details><summary>Hunks</summary>", "", _table(summary), "", "</details>"]
    return "\n".join(lines)


class GitHubPublisher:
    def __init__(
        self, client: GitHubClient, pr: dict[str, Any], branch_prefix: str, cost: Any = None
    ) -> None:
        self._c = client
        self._pr = pr
        self._prefix = branch_prefix
        self._cost = cost  # callable returning the job's spend so far

    async def _spent(self) -> float | None:
        return float(await self._cost()) if self._cost else None

    async def publish(self, ctx: JobContext, summary: dict[str, Any], patch: str) -> PublishResult:
        pr = self._pr
        number = pr["number"]
        head_ref = pr["head"]["ref"]
        same_repo = pr["head"]["repo"]["full_name"] == ctx.repo
        body = render_body(summary, verified=True, cost=await self._spent())
        sha = await ctx.ws.commit_merge(
            f"Merge {pr['base']['ref']} into {head_ref}\n\nResolved by MergeAgent (verified)."
        )
        if not same_repo:
            # Cannot push to a fork: hand the verified patch to the author instead.
            await self._c.comment(ctx.repo, number, body + f"\n\n```diff\n{patch[:50_000]}\n```")
            await self._check(ctx, "success", "Verified resolution posted as a comment", body)
            return PublishResult(None, "fork PR: verified patch posted as comment")
        branch = f"{self._prefix}pr-{number}-{sha[:7]}"
        await ctx.ws.push(branch)
        draft = await self._c.create_pull(
            ctx.repo, head=branch, base=head_ref, draft=True,
            title=f"Resolve merge conflicts in #{number}", body=body + f"\n\nResolves conflicts in #{number}.",
        )  # fmt: skip
        await self._c.comment(
            ctx.repo, number, f"MergeAgent opened {draft['html_url']} with a verified resolution."
        )
        await self._check(ctx, "success", "Verified resolution available", body, draft["html_url"])
        return PublishResult(draft["html_url"], f"draft PR {draft['html_url']}")

    async def report_failure(
        self, ctx: JobContext, summary: dict[str, Any], patch: str
    ) -> PublishResult:
        body = render_body(summary, verified=False, cost=await self._spent())
        if patch:
            body += f"\n\n<details><summary>Best candidate (unverified)</summary>\n\n```diff\n{patch[:50_000]}\n```\n</details>"
        comment = await self._c.comment(ctx.repo, self._pr["number"], body)
        await self._check(ctx, "neutral", "Needs a human", body)
        return PublishResult(comment.get("html_url"), "posted analysis comment")

    async def _check(
        self, ctx: JobContext, conclusion: str, title: str, body: str, url: str | None = None
    ) -> None:
        await self._c.create_check_run(
            ctx.repo, head_sha=self._pr["head"]["sha"], conclusion=conclusion, title=title,
            summary=body, details_url=url,
        )  # fmt: skip
