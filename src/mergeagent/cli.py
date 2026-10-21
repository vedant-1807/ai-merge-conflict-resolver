"""Command line interface: local resolution, offline demo and the evaluation harness."""

from __future__ import annotations

import asyncio
import json
import tempfile
import uuid
from pathlib import Path
from typing import Annotated, Any

import typer

from mergeagent.agents.publisher import LocalPublisher
from mergeagent.config import Settings, get_settings
from mergeagent.llm.budget import InMemoryBudget
from mergeagent.llm.gateway import MemoryRecorder
from mergeagent.llm.providers.base import LLMProvider
from mergeagent.logging import configure_logging
from mergeagent.mcp_servers.context import StaticIssueContext
from mergeagent.rag.store import MemoryVectorStore
from mergeagent.service import (
    ResolutionRequest,
    build_embedder,
    build_provider,
    build_router,
    build_sandbox,
    resolve_workspace,
)
from mergeagent.verification.pipeline import MemoryBaselineCache
from mergeagent.workspace.git_workspace import GitWorkspace

app = typer.Typer(help="MergeAgent: AI merge-conflict resolution with regression-verified fixes.")
eval_app = typer.Typer(
    help="Evaluation harness (resolution rate, routing cost, regression detection)."
)
app.add_typer(eval_app, name="eval")


async def _resolve_local(
    settings: Settings, repo: str, ours: str, theirs: str, out: Path, provider: LLMProvider,
    fixed_model: str | None = None, ctx_ref: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], MemoryRecorder]:  # fmt: skip
    job_id = f"local-{uuid.uuid4().hex[:8]}"
    ws = await GitWorkspace.clone(
        str(Path(repo).resolve()), Path(tempfile.mkdtemp()) / "ws", refs=[ours, theirs]
    )
    try:
        outcome = await ws.start_merge(await ws.resolve(ours), await ws.resolve(theirs))
        recorder = MemoryRecorder()
        req = ResolutionRequest(
            job_id, Path(repo).name, ws, outcome, StaticIssueContext(), LocalPublisher(out)
        )
        final = await resolve_workspace(
            req, settings=settings, provider=provider, router=build_router(settings, fixed_model),
            budget=InMemoryBudget(), recorder=recorder, store=MemoryVectorStore(build_embedder(settings)),
            sandbox=build_sandbox(settings), baseline_cache=MemoryBaselineCache(),
            on_context=(lambda c: ctx_ref.__setitem__("ctx", c)) if ctx_ref is not None else None,
        )  # fmt: skip
        return dict(final), recorder
    finally:
        ws.cleanup()


def _print(final: dict[str, Any], rec: MemoryRecorder) -> None:
    for e in final.get("events", []):
        typer.echo(f"  • {e}")
    typer.secho(f"\nstatus: {final.get('status')}  ({final.get('reason')})", bold=True)
    typer.echo(f"LLM calls: {len(rec.calls)}  cost: ${rec.total_cost:.4f}  models: "
               f"{sorted({c.model for c in rec.calls})}")  # fmt: skip
    if final.get("result_url"):
        typer.echo(f"patch: {final['result_url']}")


@app.command()
def resolve(
    repo: Annotated[str, typer.Option(help="Path to a local git repository")],
    ours: Annotated[str, typer.Option(help="PR/feature branch (merged INTO)")],
    theirs: Annotated[str, typer.Option(help="Target branch to merge in, e.g. main")],
    out: Annotated[Path, typer.Option(help="Where to write the patch")] = Path("mergeagent-out"),
    fixed_model: Annotated[str | None, typer.Option(help="Disable routing; use this model")] = None,
) -> None:
    """Resolve conflicts between two local branches with Claude and verify the result."""
    settings = get_settings()
    configure_logging(settings.log_level, json=False)
    final, rec = asyncio.run(
        _resolve_local(settings, repo, ours, theirs, out, build_provider(settings), fixed_model)
    )
    _print(final, rec)
    raise typer.Exit(0 if final.get("status") == "resolved" else 1)


@app.command()
def demo(out: Annotated[Path, typer.Option()] = Path("mergeagent-out")) -> None:
    """Offline end-to-end demo (no API key): scripted agents, real graph, real MCP tools, real tests."""
    from mergeagent.demo import make_conflict_repo, scripted_responder
    from mergeagent.llm.providers.fake import FakeProvider

    settings = get_settings().model_copy(deep=True)
    settings.sandbox.backend = "local"
    settings.llm.model_small, settings.llm.model_medium, settings.llm.model_large = (
        "fake-small",
        "fake-medium",
        "fake-large",
    )
    configure_logging("WARNING", json=False)
    root = Path(tempfile.mkdtemp(prefix="mergeagent-demo-"))
    repo = make_conflict_repo(root)
    ctx_ref: dict[str, Any] = {}
    typer.secho(f"Demo repo with conflicting branches: {repo}", fg="cyan")
    final, rec = asyncio.run(_resolve_local(
        settings, str(repo), "feature", "main", out, FakeProvider(scripted_responder(ctx_ref)), ctx_ref=ctx_ref
    ))  # fmt: skip
    _print(final, rec)


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8000) -> None:  # noqa: S104
    """Run the API server (webhooks + jobs API)."""
    import uvicorn

    uvicorn.run("mergeagent.api.main:app", host=host, port=port, proxy_headers=True)


# ----------------------------------------------------------------------------- eval
@eval_app.command("mine")
def eval_mine(
    dataset: Annotated[Path, typer.Option()],
    out: Annotated[Path, typer.Option()],
    cache_dir: Annotated[Path, typer.Option()] = Path(".eval-cache"),
) -> None:
    """Find historical merge commits that genuinely conflict (re-merging their parents)."""
    from mergeagent.eval.mining import mine_dataset

    n = asyncio.run(mine_dataset(dataset, out, cache_dir))
    typer.echo(f"wrote {n} scenarios to {out}")


@eval_app.command("run")
def eval_run(
    scenarios: Annotated[Path, typer.Option()],
    out: Annotated[Path, typer.Option()],
    fixed_model: Annotated[str | None, typer.Option()] = None,
    limit: Annotated[int | None, typer.Option()] = None,
    cache_dir: Annotated[Path, typer.Option()] = Path(".eval-cache"),
) -> None:
    """Run the full pipeline on every scenario and record outcome, cost and attempts."""
    from mergeagent.eval.benchmark import run_benchmark

    settings = get_settings()
    configure_logging("WARNING", json=False)
    rows = asyncio.run(
        run_benchmark(settings, scenarios, out, cache_dir, fixed_model=fixed_model, limit=limit)
    )
    typer.echo(json.dumps({"scenarios": rows}, indent=1))


@eval_app.command("mutate")
def eval_mutate(
    scenarios: Annotated[Path, typer.Option()],
    out: Annotated[Path, typer.Option()],
    per_scenario: Annotated[int, typer.Option()] = 5,
    cache_dir: Annotated[Path, typer.Option()] = Path(".eval-cache"),
) -> None:
    """Inject behavioural regressions into known-good resolutions and measure detection."""
    from mergeagent.eval.mutation import run_mutations

    settings = get_settings()
    configure_logging("WARNING", json=False)
    n = asyncio.run(run_mutations(settings, scenarios, out, cache_dir, per_scenario))
    typer.echo(f"evaluated {n} mutants -> {out}")


@eval_app.command("report")
def eval_report(
    dir: Annotated[Path, typer.Option()] = Path("eval-results"),
    out: Annotated[Path, typer.Option()] = Path("docs/evaluation-results.md"),
) -> None:
    """Aggregate eval JSONL files into a Markdown results table."""
    from mergeagent.eval.report import write_report

    text = write_report(dir, out)
    typer.echo(text)


if __name__ == "__main__":
    app()
