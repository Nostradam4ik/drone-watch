"""Журнал дій оператора й адміністратора: лише дописування, кожен рядок звʼязаний хешем із попереднім.

Захист двошаровий: тригери SQLite не дають змінити чи видалити рядок звичайним запитом, а ланцюжок хешів (`verify`)
виявляє зміну, навіть якщо тригери зняли напряму в файлі БД. Керівні символи (категорії Unicode Cc, Cf і Cs; Cs це одинокі сурогати, що не
кодуються в UTF-8) вилучаються ще до запису, щоб імʼя чи коментар не підробили рядок журналу в терміналі чи інтерфейсі."""
import contextlib
import hashlib
import json
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dronewatch.service.store import Db, StoreError, as_db

GENESIS = "0" * 64
MAX_LEN = {"user": 64, "role": 16, "action": 32, "target": 200, "result": 16, "ip": 64}
MAX_BLOCKED_KEYS = 10_000
OVERFLOW_KEY = "*overflow*"   # спільний ключ для всіх, кому не вистачило місця в таблиці
_COLS = 'id, ts, "user", role, action, target, result, ip, prev_hash, hash'   # "user" у лапках: у PostgreSQL голе user це поточна роль


class AuditError(Exception):
    """Запис у журнал не вдався: дію, що мала бути записана, виконувати не можна (fail-closed)."""


@dataclass(frozen=True)
class AuditEntry:
    user: str
    role: str
    action: str
    target: str
    result: str
    ip: str


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    rows: int
    broken_at: int | None
    head: str


def clean(text: object, limit: int) -> str:
    """str(), без керівних символів (Cc, Cf), обрізано до `limit`."""
    s = "".join(ch for ch in str(text) if unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))
    return s[:limit]


def _digest(prev_hash: str, ts: float, user: str, role: str, action: str, target: str, result: str, ip: str) -> str:
    body = json.dumps([ts, user, role, action, target, result, ip], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256((prev_hash + "\n" + body).encode()).hexdigest()


class AuditLog:
    def __init__(self, db: "Db | sqlite3.Connection", lock: threading.Lock, clock: Callable[[], float] = time.time,
                 create: bool = True) -> None:
        """create=False: лише читання готової БД (CLI перевірки), нічого не створює й не пише."""
        db = as_db(db)
        self._db, self._lock, self._clock = db, lock, clock
        self._blocked: dict[str, float] = {}
        self._blocked_lock = threading.Lock()
        self._rates: dict[str, tuple[float, int]] = {}
        if not create:
            return
        if db.dialect == "postgres":
            self._create_postgres(db)
            return
        db.execute("""CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, user TEXT, role TEXT, action TEXT, target TEXT,
            result TEXT, ip TEXT, prev_hash TEXT, hash TEXT)""")
        db.execute("CREATE INDEX IF NOT EXISTS audit_log_ts ON audit_log (ts)")
        db.execute("CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log "
                   "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END")
        db.execute("CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log "
                   "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END")
        db.commit()

    @staticmethod
    def _create_postgres(db: Db) -> None:
        if db.columns("audit_log") and db.columns("audit_head"):
            return   # робоча роль dw_app не має права DDL (CREATE/DROP TRIGGER): таблиці й тригери вже створив власник
        """id призначає код (не IDENTITY): послідовність PostgreSQL пропускає значення при відкаті, а ланцюжок вимагає id підряд.
        `audit_head` тримає останній id і хеш; рядок блокується на час вставки, тож додавання йдуть по черзі."""
        db.execute("""CREATE TABLE IF NOT EXISTS audit_log (
            id BIGINT PRIMARY KEY, ts DOUBLE PRECISION, "user" TEXT, role TEXT, action TEXT, target TEXT,
            result TEXT, ip TEXT, prev_hash TEXT, hash TEXT)""")
        db.execute("CREATE INDEX IF NOT EXISTS audit_log_ts ON audit_log (ts)")
        db.execute("CREATE TABLE IF NOT EXISTS audit_head (id INTEGER PRIMARY KEY CHECK (id = 1), "
                   "last_id BIGINT NOT NULL, head_hash TEXT NOT NULL)")
        db.execute("INSERT INTO audit_head (id, last_id, head_hash) VALUES (1, 0, ?) ON CONFLICT (id) DO NOTHING", (GENESIS,))
        db.execute("CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger AS $$ BEGIN "
                   "RAISE EXCEPTION 'audit_log is append-only'; END $$ LANGUAGE plpgsql")
        db.execute("DROP TRIGGER IF EXISTS audit_log_immutable ON audit_log")
        db.execute("CREATE TRIGGER audit_log_immutable BEFORE UPDATE OR DELETE ON audit_log "
                   "FOR EACH ROW EXECUTE FUNCTION audit_log_immutable()")
        db.execute("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log")
        db.execute("CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log "
                   "FOR EACH STATEMENT EXECUTE FUNCTION audit_log_immutable()")
        db.commit()

    def append_locked(self, entry: AuditEntry) -> int:
        """Без замка й без commit: для транзакцій, які вже тримає виклик (він сам комітить або відкочує)."""
        user, role = clean(entry.user, MAX_LEN["user"]), clean(entry.role, MAX_LEN["role"])
        action, target = clean(entry.action, MAX_LEN["action"]), clean(entry.target, MAX_LEN["target"])
        result, ip = clean(entry.result, MAX_LEN["result"]), clean(entry.ip, MAX_LEN["ip"])
        ts = float(self._clock())
        if self._db.dialect == "postgres":
            head = self._db.execute("SELECT last_id, head_hash FROM audit_head WHERE id = 1 FOR UPDATE").fetchone()
            if head is None:
                raise AuditError("audit_head не ініціалізовано")
            new_id, prev = int(head[0]) + 1, str(head[1])
            digest = _digest(prev, ts, user, role, action, target, result, ip)
            self._db.execute(
                'INSERT INTO audit_log (id, ts, "user", role, action, target, result, ip, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (new_id, ts, user, role, action, target, result, ip, prev, digest))
            self._db.execute("UPDATE audit_head SET last_id = ?, head_hash = ? WHERE id = 1", (new_id, digest))
            return new_id
        row = self._db.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        prev = row[0] if row else GENESIS
        return self._db.insert_id(
            'INSERT INTO audit_log (ts, "user", role, action, target, result, ip, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?)',
            (ts, user, role, action, target, result, ip, prev, _digest(prev, ts, user, role, action, target, result, ip)))

    def append(self, entry: AuditEntry) -> int:
        with self._lock:
            try:
                rid = self.append_locked(entry)
                self._db.commit()
                return rid
            except Exception as e:   # будь-який збій (не лише sqlite) = відкіт і AuditError, щоб виклик не лишив напівтранзакцію
                with contextlib.suppress(sqlite3.Error, StoreError):
                    self._db.rollback()
                raise AuditError(f"не вдалося записати в журнал дій: {e}") from e

    def read(self, limit: int = 100) -> list[dict[str, Any]]:
        n = max(1, min(1000, int(limit)))
        with self._lock:
            cur = self._db.execute(f"SELECT {_COLS} FROM audit_log ORDER BY id DESC LIMIT ?", (n,))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    def verify(self) -> VerifyResult:
        """Іде по id, перераховує хеш і звіряє prev_hash; id мусять іти підряд від 1, а останній дорівнювати лічильнику
        AUTOINCREMENT (sqlite_sequence): так видно й видалення рядків з кінця чи повне очищення. broken_at = id першого
        рядка, що не зійшовся (або першого відсутнього)."""
        rows, prev, broken, expected = 0, GENESIS, None, 1
        with self._lock:
            for rid, ts, user, role, action, target, result, ip, prev_hash, h in self._db.execute(
                    f"SELECT {_COLS} FROM audit_log ORDER BY id"):
                rows += 1
                if broken is None:
                    if rid != expected:
                        broken = expected
                    elif prev_hash != prev or h != _digest(prev_hash, ts, user, role, action, target, result, ip):
                        broken = rid
                prev, expected = h, rid + 1
            if self._db.dialect == "postgres":
                head = self._db.execute("SELECT last_id, head_hash FROM audit_head WHERE id = 1").fetchone()
                seq, head_hash = (int(head[0]), str(head[1])) if head else (0, GENESIS)
                if broken is None and seq == expected - 1 and head_hash != prev:   # хвіст підмінено разом із ланцюжком
                    broken = max(1, expected - 1)
            else:
                seq_row = self._db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'audit_log'").fetchone()
                seq = seq_row[0] if seq_row else 0
        if broken is None and seq != expected - 1:   # лічильник пам'ятає більше (кінець видалено) або менше, ніж є в таблиці
            broken = min(seq, expected - 1) + 1
        return VerifyResult(ok=broken is None, rows=rows, broken_at=broken, head=prev)

    def take(self, key: str, limit: int, window_s: float) -> bool:
        """True, поки за вікно (фіксоване, від першого виклику) дано менше `limit` дозволів на ключ: спільна стеля на потік
        рядків, щоб флуд з багатьох адрес не заповнив диск (адресний ліміт цього не гарантує)."""
        with self._blocked_lock:
            now = self._clock()
            start, count = self._rates.get(key, (now, 0))
            if now - start >= window_s:
                start, count = now, 0
            if count >= limit:
                return False
            self._rates[key] = (start, count + 1)
            return True

    def allow_blocked_record(self, key: str, window_s: float) -> bool:
        """True не частіше одного разу за вікно на ключ (щоб флуд 429 не роздував журнал)."""
        with self._blocked_lock:
            now = self._clock()
            last = self._blocked.get(key)
            if last is not None and now - last < window_s:
                return False
            if key not in self._blocked and len(self._blocked) >= MAX_BLOCKED_KEYS:   # стеля памʼяті: прибираємо ключі з минулими вікнами
                self._blocked = {k: t for k, t in self._blocked.items() if now - t < window_s}
                if len(self._blocked) >= MAX_BLOCKED_KEYS:
                    # Таблиця повна свіжими ключами: не росте далі, але й не мовчить. Усі нові ключі ділять один спільний запис на вікно.
                    last = self._blocked.get(OVERFLOW_KEY)
                    if last is not None and now - last < window_s:
                        return False
                    self._blocked[OVERFLOW_KEY] = now
                    return True
            self._blocked[key] = now
            return True
