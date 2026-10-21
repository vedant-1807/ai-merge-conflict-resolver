"""GitHub App authentication: App JWT -> short-lived installation tokens (cached in Redis)."""

from __future__ import annotations

import time

import httpx
import jwt
from redis.asyncio import Redis

TOKEN_TTL_MARGIN_S = 300  # refresh 5 minutes before GitHub's 60-minute expiry


def app_jwt(app_id: int, private_key_pem: str, now: int | None = None) -> str:
    now = int(now or time.time())
    # iat backdated 60s to tolerate clock skew; GitHub caps exp at 10 minutes.
    payload = {"iat": now - 60, "exp": now + 540, "iss": str(app_id)}
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


class InstallationTokenProvider:
    def __init__(
        self,
        app_id: int,
        private_key_pem: str,
        redis: Redis,
        api_url: str = "https://api.github.com",
    ) -> None:
        self._app_id = app_id
        self._key = private_key_pem
        self._redis = redis
        self._api = api_url.rstrip("/")

    async def token(self, installation_id: int) -> str:
        cache_key = f"mergeagent:ghtoken:{installation_id}"
        cached = await self._redis.get(cache_key)
        if cached:
            return cached.decode() if isinstance(cached, bytes) else str(cached)
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{self._api}/app/installations/{installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {app_jwt(self._app_id, self._key)}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            resp.raise_for_status()
            token = str(resp.json()["token"])
        await self._redis.set(cache_key, token, ex=3600 - TOKEN_TTL_MARGIN_S)
        return token
