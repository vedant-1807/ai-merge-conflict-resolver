import fakeredis.aioredis
import pytest

from mergeagent.llm.budget import BudgetExceededError, InMemoryBudget, RedisBudget
from mergeagent.llm.gateway import LLMGateway, MemoryRecorder
from mergeagent.llm.pricing import Usage, cost_usd
from mergeagent.llm.providers.anthropic import AnthropicProvider
from mergeagent.llm.providers.base import LLMRequest, ToolSpec
from mergeagent.llm.providers.fake import FakeProvider, FakeTurn
from mergeagent.llm.redaction import Redactor, shannon_entropy
from mergeagent.llm.router import ModelRouter, RouteContext, Task, Tier

GH_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def router(**kw: object) -> ModelRouter:
    return ModelRouter("fake-small", "fake-medium", "fake-large", **kw)  # type: ignore[arg-type]


def test_redaction_known_formats_and_stability() -> None:
    r = Redactor()
    text = (
        f"token={GH_TOKEN}\nagain {GH_TOKEN}\n"
        "AWS AKIAABCDEFGHIJKLMNOP\n"
        "url = postgres://admin:hunter2pass@db:5432/x\n"
        "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----\n"
    )
    out = r.redact(text)
    assert GH_TOKEN not in out
    assert "AKIAABCDEFGHIJKLMNOP" not in out
    assert "hunter2pass" not in out
    assert "MIIabc" not in out
    placeholder = r.placeholders[GH_TOKEN]
    assert out.count(placeholder) == 2  # same secret, same placeholder
    assert "postgres://admin:[REDACTED_CONNECTION_STRING_PASSWORD" in out
    assert r.counts["github_token"] == 2


def test_redaction_entropy_and_false_positives() -> None:
    r = Redactor()
    assert "Zx9QpL2mW8rT4vK7nB3c" not in r.redact('API_KEY = "Zx9QpL2mW8rT4vK7nB3c"')
    benign = "password_hint = aaaaaaaaaaaaaaaaaaaa\ndef total(prices):\n    return sum(prices)\n"
    assert r.redact(benign) == benign
    assert shannon_entropy("aaaa") == 0.0


def test_router_tiers_escalation_budget_and_fixed() -> None:
    rt = router()
    assert rt.choose(RouteContext(Task.TRIAGE)).tier is Tier.SMALL
    assert rt.choose(RouteContext(Task.RESOLVE, complexity=0.1)).tier is Tier.SMALL
    assert rt.choose(RouteContext(Task.RESOLVE, complexity=0.4)).tier is Tier.MEDIUM
    assert rt.choose(RouteContext(Task.RESOLVE, complexity=0.9)).tier is Tier.LARGE
    esc = rt.choose(RouteContext(Task.RESOLVE, complexity=0.4, attempt=2))
    assert esc.tier is Tier.LARGE
    assert "escalated" in esc.reason
    poor = rt.choose(RouteContext(Task.RESOLVE, complexity=0.9, budget_remaining_usd=0.05))
    assert poor.tier is Tier.SMALL
    assert "downgraded" in poor.reason
    fixed = router(fixed_model="fake-large").choose(RouteContext(Task.TRIAGE))
    assert fixed.model == "fake-large"
    assert fixed.tier is None


def test_pricing() -> None:
    assert cost_usd("claude-opus-5-5", Usage(1_000_000, 1_000_000)) == pytest.approx(24.0)
    assert cost_usd("claude-haiku-5-5", Usage(cache_read_tokens=1_000_000)) == pytest.approx(0.01)
    with pytest.raises(KeyError):
        cost_usd("unknown", Usage())


async def test_redis_budget_reserve_settle_and_reject() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    b = RedisBudget(redis)
    await b.open("j1", 1.0)
    await b.open("j1", 99.0)  # idempotent: does not reset the limit
    res = await b.reserve("j1", 0.6)
    with pytest.raises(BudgetExceededError):
        await b.reserve("j1", 0.5)
    remaining = await b.settle(res, 0.1)
    assert remaining == pytest.approx(0.9)
    snap = await b.snapshot("j1")
    assert snap.spent_usd == pytest.approx(0.1)
    assert snap.reserved_usd == 0
    with pytest.raises(RuntimeError, match="never opened"):
        await b.reserve("nope", 0.1)


def _echo(req: LLMRequest) -> FakeTurn:
    return FakeTurn(text="seen: " + str(req.messages[-1]["content"]))


async def test_gateway_redacts_routes_and_records() -> None:
    provider = FakeProvider(_echo)
    budget = InMemoryBudget()
    await budget.open("job", 1.0)
    rec = MemoryRecorder()
    gw = LLMGateway(provider, router(), budget, rec)
    resp, choice = await gw.complete(
        job_id="job", agent="analyzer", task=Task.ANALYZE, system="sys",
        messages=[{"role": "user", "content": f"key {GH_TOKEN}"}], complexity=0.1,
    )  # fmt: skip
    assert GH_TOKEN not in str(provider.requests[0].messages)
    assert choice.model == "fake-small"
    assert rec.calls[0].redactions == 1
    assert rec.calls[0].cost_usd > 0
    snap = await budget.snapshot("job")
    assert snap.spent_usd == pytest.approx(rec.total_cost, abs=1e-6)
    assert snap.reserved_usd == 0
    assert resp.text.startswith("seen:")


async def test_gateway_budget_exhaustion_and_provider_error() -> None:
    budget = InMemoryBudget()
    await budget.open("job", 0.000001)
    gw = LLMGateway(FakeProvider(_echo), router(), budget, MemoryRecorder())
    with pytest.raises(BudgetExceededError):
        await gw.complete(job_id="job", agent="a", task=Task.TRIAGE, system="s",
                          messages=[{"role": "user", "content": "x"}])  # fmt: skip

    def boom(_: LLMRequest) -> FakeTurn:
        raise RuntimeError("provider down")

    budget2 = InMemoryBudget()
    await budget2.open("j", 1.0)
    rec = MemoryRecorder()
    gw2 = LLMGateway(FakeProvider(boom), router(), budget2, rec)
    with pytest.raises(RuntimeError):
        await gw2.complete(job_id="j", agent="a", task=Task.TRIAGE, system="s",
                           messages=[{"role": "user", "content": "x"}])  # fmt: skip
    assert rec.calls[0].status == "error"
    assert (await budget2.snapshot("j")).reserved_usd == 0  # reservation released


def test_anthropic_params_use_caching_and_auto_tool_choice() -> None:
    req = LLMRequest(
        model="claude-sonnet-5-5", system="sys", messages=[{"role": "user", "content": "hi"}],
        tools=[ToolSpec("a", "d", {"type": "object"}), ToolSpec("b", "d", {"type": "object"})],
        effort="medium",
    )  # fmt: skip
    p = AnthropicProvider.build_params(req)
    assert p["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" in p["tools"][-1]
    assert "cache_control" not in p["tools"][0]
    assert p["tool_choice"] == {"type": "auto"}
    assert p["output_config"] == {"effort": "medium"}
