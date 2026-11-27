"""Оренда лідера в PostgreSQL (той самий протокол `LeaseStore`, що й Redis, тож `Elector` не змінюється).

Оренда й запобіжник `ha_epoch` лежать у тій самій БД, що й дані: після перемикання PostgreSQL вони переїжджають разом, тож
epoch не може відкотитися окремо від записів. Захоплення оренди підіймає `ha_epoch` у ТІЙ САМІЙ транзакції: старий лідер
відсікається в момент, коли оренду взято, а не коли новий лідер дійде до `activate`. Запис старого лідера, що вже почав
транзакцію з `FOR SHARE` на `ha_epoch`, блокує це підвищення до свого commit (див. `Engine._fence_locked`).

Термін оренди рахується за годинником PostgreSQL (`clock_timestamp()`), а не VPS: розбіжність годинників між вузлами нічого
не ламає. Лідер, як і раніше, відраховує свій термін за монотонним годинником від початку запиту поновлення (див. ha.Elector)."""
import contextlib
import threading
from collections.abc import Callable
from typing import Any

from dronewatch.service.ha import LeaseStoreError
from dronewatch.service.store import Db, Store, StoreError

_SEED = (
    "INSERT INTO ha_lease (id, owner, epoch, expires_at) VALUES (1, '', 0, to_timestamp(0)) ON CONFLICT (id) DO NOTHING",
    "INSERT INTO ha_epoch (id, epoch) VALUES (1, 0) ON CONFLICT (id) DO NOTHING",
)
_NOW_PLUS = "clock_timestamp() + (? * interval '1 millisecond')"


class PgLeaseStore:
    """Одне з'єднання на копію (Elector викликає з одного потоку); збій обриває його, наступний крок перепідключається.
    Будь-який збій це LeaseStoreError без тексту драйвера (fail-closed, як у Redis-варіанта)."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._db: Db | None = None
        self._lock = threading.Lock()

    def _run(self, fn: Callable[[Db], Any]) -> Any:
        err = ""
        with self._lock:
            try:
                if self._db is None:
                    self._db = self._store.connect()
                return fn(self._db)
            except StoreError as e:
                err = type(e).__name__
                self._drop()
        raise LeaseStoreError(err)

    def _drop(self) -> None:
        if self._db is not None:
            with contextlib.suppress(StoreError):
                self._db.rollback()
            self._db.close()
        self._db = None

    def acquire(self, owner: str, ttl_ms: int) -> int | None:
        def tx(db: Db) -> int | None:
            for sql in _SEED:
                db.execute(sql)
            row = db.execute("SELECT epoch, owner = '' OR expires_at < clock_timestamp() "
                             "FROM ha_lease WHERE id = 1 FOR UPDATE").fetchone()
            if not row[1]:
                db.rollback()
                return None
            stored = db.execute("SELECT epoch FROM ha_epoch WHERE id = 1 FOR UPDATE").fetchone()[0]
            epoch = max(int(row[0]), int(stored)) + 1
            db.execute(f"UPDATE ha_lease SET owner = ?, epoch = ?, expires_at = {_NOW_PLUS} WHERE id = 1",
                       (owner, epoch, ttl_ms))
            db.execute("UPDATE ha_epoch SET epoch = ? WHERE id = 1", (epoch,))
            db.commit()
            return epoch
        result: int | None = self._run(tx)
        return result

    def renew(self, owner: str, epoch: int, ttl_ms: int) -> bool:
        def tx(db: Db) -> bool:
            cur = db.execute(f"UPDATE ha_lease SET expires_at = {_NOW_PLUS} "
                             "WHERE id = 1 AND owner = ? AND epoch = ? AND expires_at > clock_timestamp()",
                             (ttl_ms, owner, epoch))
            ok = cur.rowcount == 1
            db.commit()
            return ok
        return bool(self._run(tx))

    def release(self, owner: str, epoch: int) -> None:
        def tx(db: Db) -> None:
            db.execute("UPDATE ha_lease SET expires_at = to_timestamp(0) WHERE id = 1 AND owner = ? AND epoch = ?",
                       (owner, epoch))
            db.commit()
        self._run(tx)

    def raise_epoch(self, at_least: int) -> None:
        def tx(db: Db) -> None:
            for sql in _SEED:
                db.execute(sql)
            db.execute("UPDATE ha_epoch SET epoch = GREATEST(epoch, ?) WHERE id = 1", (at_least,))
            db.execute("UPDATE ha_lease SET epoch = GREATEST(epoch, ?) WHERE id = 1", (at_least,))
            db.commit()
        self._run(tx)
