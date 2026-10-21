# Evaluation methodology

All numbers come from `mergeagent eval …`. Raw per-scenario JSONL lands in `eval-results/`, and the aggregated table goes in `docs/evaluation-results.md`.

## 1. Scenario mining (`make eval-mine`)

For each repository in `src/mergeagent/eval/datasets/repos.yaml` (pallets/flask, pallets/click, pallets/werkzeug, pallets/jinja, pytest-dev/pytest):

1. Walk merge commits on the default branch (two parents only).
2. Re-merge the parents in a scratch workspace: `ours` = second parent (the merged branch), `theirs` = first parent (the target).
3. Keep the merge if it produces supported text conflicts (no binary or rename/rename) with at most `max_hunks` hunks.
4. Stop at `target_per_repo` (20), giving **100 scenarios**.

The human merge commit is kept as a reference. It is used for similarity only, never as an input to the agents.

## 2. Resolution rate (`make eval-run`)

Every scenario runs the exact production pipeline (`service.resolve_workspace`): triage → analyze → resolve → sandbox verification → review, with retries and escalation.

A scenario counts as **test-verified** only if the final verdict is `pass`. Every gate must pass: build, no new typecheck diagnostics, no test regressions or lost tests against *either* parent, and acceptance commands. Verification runs in the Docker sandbox with the repository's own test suite.

Also recorded: attempts, LLM calls, cost, models used, similarity to the human resolution, and exact match.

## 3. Routing cost (`make eval-cost`)

The same scenarios run again with `--fixed-model claude-opus-5-5`, which bypasses the router. The report compares **total LLM cost ÷ total attempts** between the two modes, next to their verified rates, so savings are never reported without the quality they bought.

## 4. Regression detection (`make eval-mutate`)

Starting from the **human** merge commit (assumed correct), one mutation operator is applied to one line of a conflicted Python file:

`negate_eq`, `negate_neq`, `boundary_lt`, `boundary_gt`, `and_to_or`, `or_to_and`, `true_to_false`, `false_to_true`, `off_by_one`, `drop_not`, `return_none`

Mutants that no longer parse are discarded, because a syntax check catches those trivially; only behavioral regressions are scored. Each mutant goes through the parent-vs-candidate pipeline. **Detected** means the verdict is `fail`, so a draft PR would not have been opened. Detection is bounded by each project's test coverage. Equivalent mutants are counted as misses, so the reported rate is conservative.

## Reproducing

```bash
export LLM__ANTHROPIC_API_KEY=...
make sandbox-images
make eval-mine eval-run eval-cost eval-mutate eval-report
```

Runs are resumable: finished scenarios are skipped on restart.
