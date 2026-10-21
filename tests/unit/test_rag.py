from mergeagent.rag.chunker import chunk_file, should_index
from mergeagent.rag.embeddings import HashingEmbedder, code_tokens
from mergeagent.rag.store import MemoryVectorStore, cosine, rrf

SRC = """import os

LIMIT = 3


def parse_price(text):
    return float(text.strip("$"))


class Cart:
    def add(self, item):
        self.items.append(item)
"""


def test_python_chunking_by_symbol() -> None:
    chunks = chunk_file("shop/cart.py", SRC)
    symbols = {c.symbol for c in chunks}
    assert {"parse_price", "Cart"} <= symbols
    pp = next(c for c in chunks if c.symbol == "parse_price")
    assert pp.start_line == 6
    assert "float" in pp.content
    module = [c for c in chunks if c.symbol is None]
    assert module
    assert "LIMIT" in module[0].content


def test_window_chunking_and_filters() -> None:
    text = "".join(f"line {i}\n" for i in range(130))
    chunks = chunk_file("notes.md", text)
    assert [c.start_line for c in chunks] == [1, 51, 101]
    assert chunk_file("bad.py", "def (:\n")  # falls back to windows on syntax errors
    assert should_index("src/a.py", 10)
    assert not should_index("node_modules/x.js", 10)
    assert not should_index(".github/x.yml", 10)
    assert not should_index("a.png", 10)


def test_code_tokens_split_identifiers() -> None:
    assert {"parsePrice".lower(), "parse", "price"} <= set(code_tokens("parsePrice"))
    assert {"total_price", "total", "price"} <= set(code_tokens("total_price"))


async def test_hashing_embedder_similarity() -> None:
    e = HashingEmbedder(256)
    a, b, c = await e.embed(["def parse_price(text)", "parse price from text", "class Cart"])
    assert len(a) == 256
    assert cosine(a, b) > cosine(a, c)


def test_rrf() -> None:
    scores = rrf([[1, 2, 3], [3, 1]])
    assert max(scores, key=lambda k: scores[k]) == 1


async def test_memory_store_hybrid_search() -> None:
    store = MemoryVectorStore(HashingEmbedder(256))
    n = await store.index_files("o/r", {"shop/cart.py": SRC, "README.md": "# Shop\nA cart app\n"})
    assert n >= 3
    hits = await store.search("o/r", "parse_price", k=3)
    assert hits[0].symbol == "parse_price"
    assert await store.search("other/repo", "x") == []
