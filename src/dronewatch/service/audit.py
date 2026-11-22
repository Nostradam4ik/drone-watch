"""Журнал дій оператора й адміністратора: лише дописування, кожен рядок звʼязаний хешем із попереднім.

Захист двошаровий: тригери SQLite не дають змінити чи видалити рядок звичайним запитом, а ланцюжок хешів (`verify`)
виявляє зміну, навіть якщо тригери зняли напряму в файлі БД. Керівні символи (категорії Unicode Cc і Cf) вилучаються ще
до запису, щоб імʼя чи коментар не підробили рядок журналу в терміналі чи інтерфейсі."""
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

GENESIS = "0" * 64
MAX_LEN = {"user": 64, "role": 16, "action": 32, "target": 200, "result": 16, "ip": 64}
MAX_BLOCKED_KEYS = 10_000
_COLS = "id, ts, user, role, action, target, result, ip, prev_hash, hash"


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
    s = "".join(ch for ch in str(text) if unicodedata.category(ch) not in ("Cc", "Cf"))
    return s[:limit]


def _digest(prev_hash: str, ts: float, user: str, role: str, action: str, target: str, result: str, ip: str) -> str:
    body = json.dumps([ts, user, role, action, target, result, ip], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256((prev_hash + "\n" + body).encode()).hexdigest()


class AuditLog:
    def __init__(self, db: sqlite3.Connection, lock: threading.Lock, clock: Callable[[], float] = time.time) -> None:
        self._db, self._lock, self._clock = db, lock, clock
        self._blocked: dict[str, float] = {}
        self._blocked_lock = threading.Lock()
        db.execute("""CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, user TEXT, role TEXT, action TEXT, target TEXT,
            result TEXT, ip TEXT, prev_hash TEXT, hash TEXT)""")
        db.execute("CREATE INDEX IF NOT EXISTS audit_log_ts ON audit_log (ts)")
        db.execute("CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log "
                   "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END")
        db.execute("CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log "
                   "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END")
        db.commit()

    def append_locked(self, entry: AuditEntry) -> int:
        """Без замка й без commit: для транзакцій, які вже тримає виклик (він сам комітить або відкочує)."""
        user, role = clean(entry.user, MAX_LEN["user"]), clean(entry.role, MAX_LEN["role"])
        action, target = clean(entry.action, MAX_LEN["action"]), clean(entry.target, MAX_LEN["target"])
        result, ip = clean(entry.result, MAX_LEN["result"]), clean(entry.ip, MAX_LEN["ip"])
        ts = float(self._clock())
        row = self._db.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        prev = row[0] if row else GENESIS
        cur = self._db.execute(
            "INSERT INTO audit_log (ts, user, role, action, target, result, ip, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, user, role, action, target, result, ip, prev, _digest(prev, ts, user, role, action, target, result, ip)))
        return cur.lastrowid or 0

    def append(self, entry: AuditEntry) -> int:
        with self._lock:
            try:
                rid = self.append_locked(entry)
                self._db.commit()
                return rid
            except sqlite3.Error as e:
                with contextlib.suppress(sqlite3.Error):
                    self._db.rollback()
                raise AuditError(f"не вдалося записати в журнал дій: {e}") from e

    def read(self, limit: int = 100) -> list[dict[str, Any]]:
        n = max(1, min(1000, int(limit)))
        with self._lock:
            cur = self._db.execute(f"SELECT {_COLS} FROM audit_log ORDER BY id DESC LIMIT ?", (n,))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    def verify(self) -> VerifyResult:
        """Іде по id, перераховує хеш і звіряє prev_hash; broken_at = id першого рядка, що не зійшовся."""
        rows, prev, broken = 0, GENESIS, None
        with self._lock:
            for rid, ts, user, role, action, target, result, ip, prev_hash, h in self._db.execute(
                    f"SELECT {_COLS} FROM audit_log ORDER BY id"):
                rows += 1
                if broken is None and (prev_hash != prev or h != _digest(prev_hash, ts, user, role, action, target, result, ip)):
                    broken = rid
                prev = h
        return VerifyResult(ok=broken is None, rows=rows, broken_at=broken, head=prev)

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
                    return False
            self._blocked[key] = now
            return True
