"""Сховище рушія: один інтерфейс над SQLite (типово) і PostgreSQL (режим HA на двох вузлах).

SQL у коді пишеться з плейсхолдерами `?`; для PostgreSQL вони перетворюються на `%s`. Різницю діалектів (автоінкремент, id
вставленого рядка, перелік колонок) ховають `insert_id` і `columns`. Помилки PostgreSQL це `StoreError` БЕЗ тексту драйвера:
у ньому можуть бути адреса сервера, імʼя БД чи пароль. Пароль передається окремо від DSN й у DSN його бути не повинно."""
import contextlib
import math
import sqlite3
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from dronewatch.service.config import StorageCfg


class StoreError(Exception):
    """Збій сховища (обрив, таймаут, відмова). `sqlstate` це код PostgreSQL (безпечний для логу), якщо він був."""

    def __init__(self, message: str, *, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class Db(Protocol):
    dialect: Literal["sqlite", "postgres"]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any: ...
    def insert_id(self, sql: str, params: Sequence[Any] = ()) -> int: ...
    def columns(self, table: str) -> set[str]: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


class Store(Protocol):
    kind: Literal["sqlite", "postgres"]

    def connect(self, *, readonly: bool = False) -> Db: ...


class SqliteDb:
    dialect: Literal["sqlite", "postgres"] = "sqlite"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._c = conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self._c.execute(sql, tuple(params))

    def insert_id(self, sql: str, params: Sequence[Any] = ()) -> int:
        return self._c.execute(sql, tuple(params)).lastrowid or 0

    def columns(self, table: str) -> set[str]:
        return {r[1] for r in self._c.execute(f"PRAGMA table_info({table})")}

    def commit(self) -> None:
        self._c.commit()

    def rollback(self) -> None:
        self._c.rollback()

    def close(self) -> None:
        self._c.close()

    @property
    def total_changes(self) -> int:
        return self._c.total_changes


def as_db(conn: "Db | sqlite3.Connection") -> Db:
    """Сире з'єднання SQLite (старі виклики й тести) загортається в SqliteDb; Db лишається як є."""
    return SqliteDb(conn) if isinstance(conn, sqlite3.Connection) else conn


class SqliteStore:
    kind: Literal["sqlite", "postgres"] = "sqlite"

    def __init__(self, path: str) -> None:
        self.path = path

    def connect(self, *, readonly: bool = False) -> SqliteDb:
        if readonly and self.path != ":memory:":
            from pathlib import Path
            conn = sqlite3.connect(f"{Path(self.path).resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
        else:
            conn = sqlite3.connect(self.path, check_same_thread=False)
        return SqliteDb(conn)


def _translate(sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s")


class PostgresDb:
    dialect: Literal["sqlite", "postgres"] = "postgres"

    def __init__(self, conn: Any, errors: type[Exception]) -> None:
        self._c, self._errors = conn, errors

    def _run(self, fn: Any, *args: Any) -> Any:
        # raise поза except: інакше в __context__ лишився б виняток драйвера з адресою й іменем БД
        name, state = "", None
        try:
            return fn(*args)
        except self._errors as e:
            name, state = type(e).__name__, getattr(e, "sqlstate", None)
        raise StoreError(name, sqlstate=state)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        return self._run(self._c.execute, _translate(sql), tuple(params))

    def insert_id(self, sql: str, params: Sequence[Any] = ()) -> int:
        return int(self._run(self._c.execute, _translate(sql) + " RETURNING id", tuple(params)).fetchone()[0])

    def columns(self, table: str) -> set[str]:
        cur = self._run(self._c.execute, "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = %s", (table,))
        return {r[0] for r in cur.fetchall()}

    def commit(self) -> None:
        self._run(self._c.commit)

    def rollback(self) -> None:
        self._run(self._c.rollback)

    def close(self) -> None:
        with contextlib.suppress(self._errors):
            self._c.close()


class PostgresStore:
    kind: Literal["sqlite", "postgres"] = "postgres"

    def __init__(self, dsn: str, password: str | None, connect_timeout_s: float, statement_timeout_s: float) -> None:
        self._dsn, self._password = dsn, password
        self._connect_timeout, self._statement_ms = max(1, math.ceil(connect_timeout_s)), int(statement_timeout_s * 1000)

    def connect(self, *, readonly: bool = False) -> PostgresDb:
        try:
            import psycopg
        except ImportError:
            raise StoreError("потрібен пакет psycopg: pip install 'dronewatch[postgres]'") from None
        name, state = "", None
        try:
            conn = psycopg.connect(self._dsn, password=self._password, connect_timeout=self._connect_timeout, autocommit=False,
                                   options=f"-c statement_timeout={self._statement_ms}")
            if readonly:
                conn.read_only = True
            return PostgresDb(conn, psycopg.Error)
        except psycopg.Error as e:
            name, state = type(e).__name__, getattr(e, "sqlstate", None)
        raise StoreError(name, sqlstate=state)


def open_store(cfg: "StorageCfg", sqlite_path: str) -> Store:
    if cfg.backend == "postgres":
        import os
        assert cfg.dsn   # config._check вимагає dsn при backend=postgres
        return PostgresStore(cfg.dsn, os.environ.get("DRONEWATCH_PG_PASSWORD"), cfg.connect_timeout_s, cfg.statement_timeout_s)
    return SqliteStore(sqlite_path)


def check_pg_dsn(dsn: str) -> None:
    """Політика DSN (fail-closed): лише URL-формат, TLS із перевіркою імені сервера, перехід на поточний master, без пароля в DSN.
    Тексту DSN у повідомленнях немає: у ньому може бути пароль."""
    from urllib.parse import parse_qs, urlsplit
    if not dsn.startswith(("postgresql://", "postgres://")):
        raise ValueError("storage.dsn: потрібен формат postgresql://user@host:port[,host:port]/db?...")
    parts = urlsplit(dsn)
    query = parse_qs(parts.query)
    if parts.password or "password" in query:
        raise ValueError("storage.dsn: пароль не пишемо в DSN, він береться зі змінної DRONEWATCH_PG_PASSWORD")
    if query.get("sslmode") != ["verify-full"]:
        raise ValueError("storage.dsn: потрібен sslmode=verify-full (TLS із перевіркою імені сервера)")
    if query.get("target_session_attrs") != ["read-write"]:
        raise ValueError("storage.dsn: потрібен target_session_attrs=read-write (клієнт сам знаходить поточний master)")


def probe(store: Store) -> None:
    """Старт у режимі HA: недоступне сховище це відмова запуску, а не тихий відкат на пам'ять (fail-closed)."""
    db = store.connect()
    try:
        db.execute("SELECT 1").fetchone()
    finally:
        db.close()
