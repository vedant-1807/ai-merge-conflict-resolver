"""Prometheus metrics shared by the API and the worker (scraped at ``/metrics``)."""

from __future__ import annotations

from prometheus_client import Counter, Histogram

WEBHOOKS = Counter("mergeagent_webhooks_total", "Webhook deliveries", ["event", "outcome"])
JOBS = Counter("mergeagent_jobs_total", "Finished resolution jobs", ["status"])
HUNKS = Counter("mergeagent_hunks_total", "Conflict hunks by triage category", ["category"])

LLM_CALLS = Counter("mergeagent_llm_calls_total", "LLM calls", ["model", "agent", "status"])
LLM_COST = Counter("mergeagent_llm_cost_usd_total", "LLM spend in USD", ["model", "agent"])
LLM_TOKENS = Counter("mergeagent_llm_tokens_total", "LLM tokens", ["model", "kind"])
LLM_LATENCY = Histogram(
    "mergeagent_llm_latency_seconds",
    "LLM call latency",
    ["model"],
    buckets=(0.5, 1, 2, 5, 10, 20, 40, 80, 160),
)
REDACTIONS = Counter("mergeagent_redactions_total", "Secrets redacted from prompts", ["rule"])
BUDGET_REJECTIONS = Counter("mergeagent_budget_rejections_total", "Calls refused by job budget")

TOOL_CALLS = Counter(
    "mergeagent_mcp_tool_calls_total", "MCP tool calls", ["agent", "tool", "outcome"]
)
SANDBOX_RUNS = Histogram(
    "mergeagent_sandbox_run_seconds",
    "Sandbox command duration",
    ["phase"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 900),
)
VERIFICATIONS = Counter(
    "mergeagent_verifications_total", "Verification pipeline verdicts", ["verdict"]
)
