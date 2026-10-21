"""Split source files into retrieval units.

Python files are chunked per top-level function/class (methods stay with their class unless the
class is large, then per method). Other files use overlapping line windows.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath

WINDOW = 60
OVERLAP = 10
MAX_CLASS_LINES = 150
INDEXABLE_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".rb", ".c",
    ".h", ".cc", ".cpp", ".cs", ".php", ".scala", ".swift", ".md", ".rst", ".toml", ".cfg",
    ".yaml", ".yml",
}  # fmt: skip


@dataclass(frozen=True, slots=True)
class Chunk:
    path: str
    symbol: str | None
    start_line: int
    end_line: int
    content: str

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(f"{self.path}\0{self.content}".encode()).hexdigest()


def should_index(path: str, size: int, max_bytes: int = 200_000) -> bool:
    p = PurePosixPath(path)
    if size > max_bytes or any(part.startswith(".") for part in p.parts[:-1]):
        return False
    if any(part in {"node_modules", "vendor", "dist", "build", "__pycache__"} for part in p.parts):
        return False
    return p.suffix.lower() in INDEXABLE_SUFFIXES


def _windows(path: str, lines: list[str]) -> list[Chunk]:
    chunks = []
    step = WINDOW - OVERLAP
    for start in range(0, max(len(lines), 1), step):
        body = lines[start : start + WINDOW]
        if not "".join(body).strip():
            continue
        chunks.append(Chunk(path, None, start + 1, start + len(body), "".join(body)))
        if start + WINDOW >= len(lines):
            break
    return chunks


def _python(path: str, text: str, lines: list[str]) -> list[Chunk]:
    tree = ast.parse(text)
    chunks: list[Chunk] = []
    covered: set[int] = set()

    def add(node: ast.AST, symbol: str) -> None:
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])  # type: ignore[attr-defined]
        end = node.end_lineno or start  # type: ignore[attr-defined]
        chunks.append(Chunk(path, symbol, start, end, "".join(lines[start - 1 : end])))
        covered.update(range(start, end + 1))

    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            add(node, node.name)
        elif isinstance(node, ast.ClassDef):
            size = (node.end_lineno or node.lineno) - node.lineno
            methods = [
                n for n in node.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
            ]
            if size > MAX_CLASS_LINES and methods:
                for m in methods:
                    add(m, f"{node.name}.{m.name}")
            else:
                add(node, node.name)
    # Module-level code not inside any definition (constants, imports, scripts).
    rest = [ln if i + 1 not in covered else "\n" for i, ln in enumerate(lines)]
    if "".join(rest).strip():
        chunks += [c for c in _windows(path, rest) if c.content.strip()]
    return chunks


def chunk_file(path: str, text: str) -> list[Chunk]:
    lines = text.splitlines(keepends=True)
    if PurePosixPath(path).suffix == ".py":
        try:
            return _python(path, text, lines)
        except SyntaxError:
            pass
    return _windows(path, lines)
