"""Per-job LLM budgets with atomic reserve/settle semantics.

Before each call the gateway *reserves* the worst-case cost; after the call it *settles* the
reservation against the actual cost. Reservation happens in a single Lua script so concurrent
agents in the same job can never jointly overspend. Amounts are stored as integer micro-dollars
to avoid float drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis

MICRO = 1_000_000
TTL_SECONDS = 7 * 24 * 3600

# KEYS[1] = budget hash; ARGV = amount, ttl. Returns remaining after reservation or -1.
_RESERVE = """
local limit = tonumber(redis.call('HGET', KEYS[1], 'limit') or '-1')
if limit < 0 then return -2 end
local spent = tonumber(redis.call('HGET', KEYS[1], 'spent') or '0')
local reserved = tonumber(redis.call('HGET', KEYS[1], 'reserved') or '0')
local amount = tonumber(ARGV[1])
if spent + reserved + amount > limit then return -1 end
redis.call('HINCRBY', KEYS[1], 'reserved', amount)
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
return limit - spent - reserved - amount
"""

# KEYS[1] = budget hash; ARGV = reserved amount, actual amount.
_SETTLE = """
redis.call('HINCRBY', KEYS[1], 'reserved', -tonumber(ARGV[1]))
redis.call('HINCRBY', KEYS[1], 'spent', tonumber(ARGV[2]))
local limit = tonumber(redis.call('HGET', KEYS[1], 'limit') or '0')
local spent = tonumber(redis.call('HGET', KEYS[1], 'spent') or '0')
local reserved = tonumber(redis.call('HGET', KEYS[1], 'reserved') or '0')
return limit - spent - reserved
"""


class BudgetExceededError(RuntimeError):
    def __init__(self, job_id: str, needed: float, remaining: float) -> None:
        super().__init__(
            f"job {job_id}: call needs up to ${needed:.4f} but only ${remaining:.4f} remains"
        )
        self.needed = needed
        self.remaining = remaining


@dataclass(frozen=True, slots=True)
class Reservation:
    job_id: str
    micros: int


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    limit_usd: float
    spent_usd: float
    reserved_usd: float

    @property
    def remaining_usd(self) -> float:
        return self.limit_usd - self.spent_usd - self.reserved_usd


class Budget(Protocol):
    async def open(self, job_id: str, limit_usd: float) -> None: ...
    async def reserve(self, job_id: str, amount_usd: float) -> Reservation: ...
    async def settle(self, reservation: Reservation, actual_usd: float) -> float: ...
    async def snapshot(self, job_id: str) -> BudgetSnapshot: ...


def _key(job_id: str) -> str:
    return f"mergeagent:budget:{job_id}"


class RedisBudget:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._reserve = redis.register_script(_RESERVE)
        self._settle = redis.register_script(_SETTLE)

    async def open(self, job_id: str, limit_usd: float) -> None:
        key = _key(job_id)
        # HSETNX keeps an existing budget when a job is retried by the queue.
        await self._redis.hsetnx(key, "limit", str(int(limit_usd * MICRO)))  # type: ignore[misc]
        await self._redis.expire(key, TTL_SECONDS)

    async def reserve(self, job_id: str, amount_usd: float) -> Reservation:
        micros = max(int(amount_usd * MICRO), 1)
        remaining = int(await self._reserve(keys=[_key(job_id)], args=[micros, TTL_SECONDS]))
        if remaining == -2:
            raise RuntimeError(f"budget for job {job_id} was never opened")
        if remaining < 0:
            snap = await self.snapshot(job_id)
            raise BudgetExceededError(job_id, amount_usd, snap.remaining_usd)
        return Reservation(job_id, micros)

    async def settle(self, reservation: Reservation, actual_usd: float) -> float:
        remaining = await self._settle(
            keys=[_key(reservation.job_id)],
            args=[reservation.micros, int(round(actual_usd * MICRO))],
        )
        return int(remaining) / MICRO

    async def snapshot(self, job_id: str) -> BudgetSnapshot:
        raw = await self._redis.hgetall(_key(job_id))  # type: ignore[misc]
        vals = {(k.decode() if isinstance(k, bytes) else k): int(v) for k, v in raw.items()}
        return BudgetSnapshot(
            vals.get("limit", 0) / MICRO,
            vals.get("spent", 0) / MICRO,
            vals.get("reserved", 0) / MICRO,
        )


class InMemoryBudget:
    """Single-process budget for the CLI and eval harness (same semantics as RedisBudget)."""

    def __init__(self) -> None:
        self._state: dict[str, list[int]] = {}  # job -> [limit, spent, reserved]

    async def open(self, job_id: str, limit_usd: float) -> None:
        self._state.setdefault(job_id, [int(limit_usd * MICRO), 0, 0])

    async def reserve(self, job_id: str, amount_usd: float) -> Reservation:
        limit, spent, reserved = self._state[job_id]
        micros = max(int(amount_usd * MICRO), 1)
        if spent + reserved + micros > limit:
            raise BudgetExceededError(job_id, amount_usd, (limit - spent - reserved) / MICRO)
        self._state[job_id][2] += micros
        return Reservation(job_id, micros)

    async def settle(self, reservation: Reservation, actual_usd: float) -> float:
        st = self._state[reservation.job_id]
        st[2] -= reservation.micros
        st[1] += int(round(actual_usd * MICRO))
        return (st[0] - st[1] - st[2]) / MICRO

    async def snapshot(self, job_id: str) -> BudgetSnapshot:
        limit, spent, reserved = self._state[job_id]
        return BudgetSnapshot(limit / MICRO, spent / MICRO, reserved / MICRO)
