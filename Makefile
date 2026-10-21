.PHONY: install lint format typecheck test test-integration cov up down migrate sandbox-images eval-mine eval-run eval-cost eval-mutate eval-report

install:
	uv sync

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

format:
	uv run ruff format src tests
	uv run ruff check --fix src tests

typecheck:
	uv run mypy

test:
	uv run pytest

test-integration:
	uv run pytest -m integration

cov:
	uv run pytest --cov --cov-report=term-missing

up:
	docker compose up -d --build

down:
	docker compose down

migrate:
	uv run alembic upgrade head

sandbox-images:
	docker build -f docker/sandbox/python.Dockerfile -t mergeagent-sandbox-python:latest docker/sandbox
	docker build -f docker/sandbox/node.Dockerfile -t mergeagent-sandbox-node:latest docker/sandbox

# ---- Evaluation harness (needs LLM__ANTHROPIC_API_KEY; spends real money) ----
EVAL_DIR ?= eval-results

eval-mine:
	uv run mergeagent eval mine --dataset src/mergeagent/eval/datasets/repos.yaml --out $(EVAL_DIR)/scenarios.jsonl

eval-run:
	uv run mergeagent eval run --scenarios $(EVAL_DIR)/scenarios.jsonl --out $(EVAL_DIR)/routed.jsonl

eval-cost:
	uv run mergeagent eval run --scenarios $(EVAL_DIR)/scenarios.jsonl --out $(EVAL_DIR)/fixed.jsonl --fixed-model claude-opus-5-5

eval-mutate:
	uv run mergeagent eval mutate --scenarios $(EVAL_DIR)/scenarios.jsonl --results $(EVAL_DIR)/routed.jsonl --out $(EVAL_DIR)/mutations.jsonl

eval-report:
	uv run mergeagent eval report --dir $(EVAL_DIR) --out docs/evaluation-results.md
