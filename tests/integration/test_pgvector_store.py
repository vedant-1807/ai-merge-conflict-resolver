import os

import pytest
from sqlalchemy import delete

from mergeagent.db.models import CodeChunk
from mergeagent.db.session import make_engine, make_sessionmaker
from mergeagent.rag.embeddings import HashingEmbedder
from mergeagent.rag.store import PgVectorStore

pytestmark = pytest.mark.integration

URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://mergeagent:mergeagent@localhost:5432/mergeagent"
)


async def test_index_incrementally_and_hybrid_search() -> None:
    engine = make_engine(URL)
    factory = make_sessionmaker(engine)
    async with factory() as s, s.begin():
        await s.execute(delete(CodeChunk).where(CodeChunk.repo_full_name == "it/repo"))
    store = PgVectorStore(factory, HashingEmbedder(1024))
    files = {
        "calc.py": "def total(prices):\n    return sum(prices)\n\n\ndef tax_rate():\n    return 0.2\n",
        "cart.py": "class Cart:\n    def checkout(self):\n        return total(self.prices)\n",
    }
    assert await store.index_files("it/repo", files) >= 3
    assert await store.index_files("it/repo", files) == 0  # unchanged -> nothing re-embedded
    files["calc.py"] = files["calc.py"].replace("0.2", "0.25")
    assert await store.index_files("it/repo", files) == 1  # only the changed function

    hits = await store.search("it/repo", "tax_rate", k=3)
    assert hits[0].symbol == "tax_rate"
    assert "0.25" in hits[0].content
    await engine.dispose()
