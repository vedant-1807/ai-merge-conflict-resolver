"""Cheap static checks run on every candidate before the (expensive) sandbox verification."""

from __future__ import annotations

import ast
import json
import tomllib
from pathlib import PurePosixPath

import yaml

from mergeagent.conflicts.parser import has_conflict_markers


def syntax_error(path: str, text: str) -> str | None:
    """Return a human readable syntax error, or ``None`` if the file parses (or is unknown)."""
    suffix = PurePosixPath(path).suffix.lower()
    try:
        if suffix in {".py", ".pyi"}:
            ast.parse(text, filename=path)
        elif suffix == ".json":
            json.loads(text)
        elif suffix in {".yaml", ".yml"}:
            list(yaml.safe_load_all(text))
        elif suffix == ".toml":
            tomllib.loads(text)
    except SyntaxError as exc:
        return f"{path}:{exc.lineno}: {exc.msg}"
    except (json.JSONDecodeError, yaml.YAMLError, tomllib.TOMLDecodeError) as exc:
        return f"{path}: {exc}"
    return None


def static_problems(files: dict[str, str | None]) -> list[str]:
    """Validate resolved file contents (``None`` means the file is deleted)."""
    problems: list[str] = []
    for path, text in files.items():
        if text is None:
            continue
        if has_conflict_markers(text):
            problems.append(f"{path}: conflict markers remain")
        err = syntax_error(path, text)
        if err:
            problems.append(err)
    return problems
