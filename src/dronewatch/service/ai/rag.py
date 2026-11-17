"""Qdrant-індекс минулих інцидентів для семантичного пошуку («чи був такий випадок раніше?»). Індексація
відв'язана від LLM-наративу (Фаза 2): інцидент потрапляє сюди одразу при появі тривоги, незалежно від того,
чи вдасться потім написати наратив — інакше збій Mistral назавжди вилучав би інцидент з майбутнього пошуку.

Колекція створюється лениво (перший реальний index()/search()), ніколи при старті сервісу: розмір вектора не
знаємо заздалегідь (залежить від моделі ембедингів), а чіпати мережу під час lifespan суперечить принципу
«AI недоступний не повинен заважати старту сервісу»."""
import threading
import uuid
from collections.abc import Callable
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from dronewatch.service.ai.errors import AiUnavailable


def incident_text(row: dict[str, Any]) -> str:
    return (f"Інцидент: трек {row['track_id']}, зона {row['zone']}, рівень {row['level']}, "
            f"швидкість {row['speed']} м/с, підтверджено: {row['sensors']}, час {row['ts']}")


class IncidentIndex:
    def __init__(self, qdrant_url: str, embed: Callable[[list[str]], list[list[float]]], *,
                 collection: str = "incidents", client: QdrantClient | None = None) -> None:
        # check_compatibility=False: за замовчуванням конструктор QdrantClient сам ходить у мережу перевірити
        # версію сервера — для нас це мережевий виклик у build_ai()/lifespan, саме те, чого AI-шар обіцяє не
        # робити при старті сервісу. Лінива колекція (нижче) все одно перевірить реальну доступність при першому
        # справжньому index()/search().
        self._client = client or QdrantClient(url=qdrant_url, check_compatibility=False)
        self._embed = embed
        self._collection = collection
        self._ensured = False
        # check collection_exists() потім create_collection() — дві окремі мережеві дії без цього замка: кілька
        # паралельних index() на щойно піднятому стенді (саме тоді, коли це найбільше шкодить) усі бачать
        # «колекції нема» й намагаються створити, і всі, крім першого, падали з «already exists» — інцидент
        # губився з майбутнього RAG-пошуку, хоча embed() для нього вже відбувся.
        self._ensure_lock = threading.Lock()

    def _ensure_collection(self) -> None:
        if self._ensured:
            return
        with self._ensure_lock:
            if self._ensured:
                return
            try:
                if not self._client.collection_exists(self._collection):
                    dim = len(self._embed(["."])[0])
                    try:
                        self._client.create_collection(self._collection,
                                                        vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
                    except Exception:
                        if not self._client.collection_exists(self._collection):
                            raise   # справді не вдалося, а не просто «хтось інший устиг» — тут замок уже не допоможе
            except AiUnavailable:
                raise
            except Exception as e:   # непередбачена помилка самого клієнта Qdrant (мережа, протокол) — не лише embed
                raise AiUnavailable(f"Qdrant: не вдалося підготувати колекцію «{self._collection}»: {e}") from e
            self._ensured = True

    def index(self, point_id: str, row: dict[str, Any]) -> None:
        text = incident_text(row)
        vector = self._embed([text])[0]   # може підняти AiUnavailable — пропускаємо далі як є
        self._ensure_collection()
        # Qdrant приймає лише unsigned int чи UUID як id точки, а point_id (ключ з викличного боку — рядок SQLite
        # id, uuid4 тощо) довільний рядок; детерміновано мапимо його в UUID5, щоб той самий point_id завжди бив
        # в ту саму точку (повторний index() того самого інциденту оновлює запис, а не дублює).
        qid = str(uuid.uuid5(uuid.NAMESPACE_OID, point_id))
        try:
            self._client.upsert(self._collection, points=[PointStruct(id=qid, vector=vector, payload={"text": text, **row})])
        except Exception as e:
            raise AiUnavailable(f"Qdrant: не вдалося записати інцидент: {e}") from e

    def search(self, query: str, k: int = 3) -> list[str]:
        try:
            if not self._client.collection_exists(self._collection):
                return []
            vector = self._embed([query])[0]
            hits = self._client.query_points(self._collection, query=vector, limit=k).points
        except AiUnavailable:
            raise
        except Exception as e:
            raise AiUnavailable(f"Qdrant: пошук не вдався: {e}") from e
        return [h.payload["text"] for h in hits if h.payload]
