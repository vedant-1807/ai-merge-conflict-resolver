"""Minimal async GitHub REST client (only the endpoints the app needs)."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

API_VERSION = "2022-11-28"
_ISSUE_REF = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)|(?<![\w/])#(\d+)\b"
)


class GitHubClient:
    def __init__(self, token: str, api_url: str = "https://api.github.com") -> None:
        self._http = httpx.AsyncClient(
            base_url=api_url.rstrip("/"),
            timeout=30,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "mergeagent",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _req(self, method: str, url: str, **kw: Any) -> Any:
        resp = await self._http.request(method, url, **kw)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    async def get_pull(self, repo: str, number: int) -> dict[str, Any]:
        return await self._req("GET", f"/repos/{repo}/pulls/{number}")  # type: ignore[no-any-return]

    async def wait_for_mergeability(
        self, repo: str, number: int, attempts: int = 6, delay_s: float = 2.0
    ) -> dict[str, Any]:
        """GitHub computes `mergeable` asynchronously; poll until it is known."""
        pr = await self.get_pull(repo, number)
        for i in range(attempts):
            if pr.get("mergeable") is not None:
                break
            await asyncio.sleep(delay_s * (i + 1))
            pr = await self.get_pull(repo, number)
        return pr

    async def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        return await self._req("GET", f"/repos/{repo}/issues/{number}")  # type: ignore[no-any-return]

    async def get_file(self, repo: str, path: str, ref: str) -> str | None:
        resp = await self._http.get(
            f"/repos/{repo}/contents/{path}", params={"ref": ref},
            headers={"Accept": "application/vnd.github.raw+json"},
        )  # fmt: skip
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.text

    async def create_pull(
        self, repo: str, *, head: str, base: str, title: str, body: str, draft: bool = True
    ) -> dict[str, Any]:
        return await self._req(  # type: ignore[no-any-return]
            "POST", f"/repos/{repo}/pulls",
            json={"head": head, "base": base, "title": title, "body": body, "draft": draft},
        )  # fmt: skip

    async def comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        return await self._req(  # type: ignore[no-any-return]
            "POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body[:65_000]}
        )

    async def create_check_run(
        self,
        repo: str,
        *,
        head_sha: str,
        conclusion: str,
        title: str,
        summary: str,
        details_url: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": "MergeAgent",
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": title[:255], "summary": summary[:65_000]},
        }
        if details_url:
            payload["details_url"] = details_url
        return await self._req("POST", f"/repos/{repo}/check-runs", json=payload)  # type: ignore[no-any-return]


def referenced_issues(text: str) -> list[int]:
    nums = []
    for m in _ISSUE_REF.finditer(text or ""):
        n = int(m.group(1) or m.group(2))
        if n not in nums:
            nums.append(n)
    return nums[:10]


class GitHubIssueContext:
    """IssueContext backed by the GitHub API (used by the issues MCP server)."""

    def __init__(self, client: GitHubClient, repo: str, pr: dict[str, Any]) -> None:
        self._c = client
        self._repo = repo
        self._pr = pr

    async def pull_request(self) -> dict[str, Any]:
        p = self._pr
        return {
            "number": p.get("number"), "title": p.get("title"), "body": (p.get("body") or "")[:6000],
            "author": (p.get("user") or {}).get("login"),
            "labels": [lbl.get("name") for lbl in p.get("labels", [])],
            "head": p.get("head", {}).get("ref"), "base": p.get("base", {}).get("ref"),
        }  # fmt: skip

    async def linked_issues(self) -> list[dict[str, Any]]:
        out = []
        for n in referenced_issues(f"{self._pr.get('title', '')}\n{self._pr.get('body') or ''}"):
            if n == self._pr.get("number"):
                continue
            try:
                issue = await self._c.get_issue(self._repo, n)
            except httpx.HTTPStatusError:
                continue
            out.append({"number": n, "title": issue.get("title"), "body": (issue.get("body") or "")[:4000],
                        "state": issue.get("state")})  # fmt: skip
        return out
