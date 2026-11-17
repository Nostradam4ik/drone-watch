"""RAG-індекс минулих інцидентів поверх Qdrant: лінива колекція, індексація, семантичний пошук."""
import pytest
from qdrant_client import QdrantClient

from dronewatch.service.ai.errors import AiUnavailable
from dronewatch.service.ai.rag import IncidentIndex


def _fake_embed(dim: int):
    def embed(texts: list[str]) -> list[list[float]]:
        return [[float(hash(t) % 1000) / 1000] * dim for t in texts]
    return embed


def test_index_then_search_finds_it():
    idx = IncidentIndex("unused", _fake_embed(4), client=QdrantClient(location=":memory:"))
    idx.index("1", {"track_id": 7, "zone": "A", "level": "CRITICAL", "speed": 45.0, "sensors": "radar1,rf1", "ts": 1.0})
    hits = idx.search("трек 7 зона A", k=3)
    assert hits and "трек 7" in hits[0]


def test_search_on_empty_index_returns_empty_list():
    idx = IncidentIndex("unused", _fake_embed(4), client=QdrantClient(location=":memory:"))
    assert idx.search("щось") == []


def test_index_propagates_ai_unavailable_from_embed():
    def broken_embed(texts):
        raise AiUnavailable("down")
    idx = IncidentIndex("unused", broken_embed, client=QdrantClient(location=":memory:"))
    with pytest.raises(AiUnavailable):
        idx.index("1", {"track_id": 1, "zone": "A", "level": "WARNING", "speed": 1.0, "sensors": "radar1", "ts": 1.0})


class _BrokenQdrantClient:
    """Імітує непередбачену помилку самого клієнта Qdrant (мережа, протокол) — не через embed."""
    def collection_exists(self, name):
        raise ConnectionError("qdrant unreachable")


def test_index_wraps_raw_qdrant_errors_as_ai_unavailable():
    idx = IncidentIndex("unused", _fake_embed(4), client=_BrokenQdrantClient())
    with pytest.raises(AiUnavailable):
        idx.index("1", {"track_id": 1, "zone": "A", "level": "WARNING", "speed": 1.0, "sensors": "radar1", "ts": 1.0})


def test_search_wraps_raw_qdrant_errors_as_ai_unavailable():
    idx = IncidentIndex("unused", _fake_embed(4), client=_BrokenQdrantClient())
    with pytest.raises(AiUnavailable):
        idx.search("щось")
