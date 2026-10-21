"""LangGraph wiring of the multi-agent resolution pipeline.

triage ──(nothing pending)──────────────────────────► verify
   │                                                    ▲  │
   └─► analyze ─► resolve ──────────────────────────────┘  ├─pass──► publish
                     ▲                                     ├─retry─► next_attempt ─► resolve
                     └─────────────────────────────────────┘
                                                           └─fail/inconclusive──► give_up
"""

from __future__ import annotations

from functools import partial
from typing import Any

from langgraph.graph import END, START, StateGraph

from mergeagent.agents import nodes
from mergeagent.agents.state import Deps, GraphState
from mergeagent.llm.budget import BudgetExceededError
from mergeagent.llm.providers.base import RefusalError


def _guard(fn: Any, deps: Deps) -> Any:
    """Turn budget exhaustion / model refusal into a clean needs_human outcome."""

    async def node(state: GraphState) -> dict[str, Any]:
        try:
            return await fn(state, deps)  # type: ignore[no-any-return]
        except (BudgetExceededError, RefusalError) as exc:
            return {
                "status": "needs_human",
                "reason": str(exc),
                "events": [f"{fn.__name__}: {exc}"],
            }

    node.__name__ = fn.__name__
    return node


def _after_triage(state: GraphState) -> str:
    if state.get("status"):
        return "end"
    return "analyze" if state.get("pending") else "verify"


def _halted(state: GraphState, nxt: str) -> str:
    return "end" if state.get("status") else nxt


def build_graph(deps: Deps) -> Any:
    g: StateGraph[GraphState] = StateGraph(GraphState)
    g.add_node("triage", _guard(nodes.triage, deps))
    g.add_node("analyze", _guard(nodes.analyze, deps))
    g.add_node("resolve", _guard(nodes.resolve, deps))
    g.add_node("verify", _guard(nodes.verify, deps))
    g.add_node("next_attempt", _guard(nodes.next_attempt, deps))
    g.add_node("publish", _guard(nodes.publish, deps))
    g.add_node("give_up", _guard(nodes.give_up, deps))

    g.add_edge(START, "triage")
    g.add_conditional_edges(
        "triage", _after_triage, {"analyze": "analyze", "verify": "verify", "end": END}
    )
    g.add_conditional_edges(
        "analyze", partial(_halted, nxt="resolve"), {"resolve": "resolve", "end": END}
    )
    g.add_conditional_edges(
        "resolve", partial(_halted, nxt="verify"), {"verify": "verify", "end": END}
    )
    g.add_conditional_edges(
        "verify",
        lambda s: "end" if s.get("status") else nodes.route_after_verify(s, deps),
        {"publish": "publish", "retry": "next_attempt", "give_up": "give_up", "end": END},
    )
    g.add_edge("next_attempt", "resolve")
    g.add_edge("publish", END)
    g.add_edge("give_up", END)
    return g.compile()


async def run_resolution(deps: Deps) -> GraphState:
    graph = build_graph(deps)
    final: GraphState = await graph.ainvoke({"events": []}, config={"recursion_limit": 50})
    return final
