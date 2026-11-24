"""Адаптер сховища: SQLite і PostgreSQL за одним інтерфейсом."""
import builtins
import sqlite3

import pytest
from tests.conftest import pg_dsn

from dronewatch.service.config import StorageCfg
from dronewatch.service.store import PostgresStore, SqliteStore, StoreError, open_store


def _table(db):
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if db.dialect == "sqlite" else "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY"
    db.execute(f"CREATE TABLE t (id {pk}, name TEXT, v DOUBLE PRECISION)")
    db.commit()


def test_insert_id_and_columns(db_backend):
    db = db_backend.connect()
    _table(db)
    a = db.insert_id("INSERT INTO t (name, v) VALUES (?, ?)", ("a", 1.5))
    b = db.insert_id("INSERT INTO t (name, v) VALUES (?, ?)", ("b", 2.5))
    db.commit()
    assert b == a + 1
    assert db.columns("t") == {"id", "name", "v"}
    assert db.execute("SELECT name FROM t WHERE id = ?", (a,)).fetchone()[0] == "a"
    db.close()


def test_rollback_discards(db_backend):
    db = db_backend.connect()
    _table(db)
    db.execute("INSERT INTO t (name, v) VALUES (?, ?)", ("x", 0.0))
    db.rollback()
    assert db.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    db.close()


def test_readonly_rejects_writes(db_backend):
    db = db_backend.connect()
    _table(db)
    db.close()
    ro = db_backend.connect(readonly=True)
    with pytest.raises((sqlite3.OperationalError, StoreError)):
        ro.execute("INSERT INTO t (name, v) VALUES (?, ?)", ("x", 0.0))
    ro.close()


def test_open_store_sqlite_default(tmp_path):
    s = open_store(StorageCfg(), str(tmp_path / "x.db"))
    assert s.kind == "sqlite"


def test_postgres_requires_psycopg(monkeypatch):
    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("psycopg"):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(StoreError, match=r"dronewatch\[postgres\]"):
        PostgresStore("postgresql://u@h/d", None, 2, 3).connect()


def test_error_text_has_no_dsn_or_password():
    pytest.importorskip("psycopg")
    store = PostgresStore("postgresql://u@127.0.0.1:1/secretdb", "s3cretpw", 1, 1)
    with pytest.raises(StoreError) as e:
        store.connect()
    text = str(e.value) + repr(e.value) + repr(e.value.__cause__) + repr(e.value.__context__)
    assert "s3cretpw" not in text and "127.0.0.1" not in text and "secretdb" not in text


def test_pg_timeouts_applied():
    dsn = pg_dsn()
    if not dsn:
        pytest.skip("DRONEWATCH_TEST_PG_DSN не задано")
    db = PostgresStore(dsn, None, 2, 3).connect()
    assert db.execute("SHOW statement_timeout").fetchone()[0] == "3s"
    db.close()


def test_sqlite_store_kind(tmp_path):
    assert SqliteStore(str(tmp_path / "a.db")).kind == "sqlite"


def test_connect_sets_client_side_dead_peer_detection(monkeypatch):
    """statement_timeout працює на сервері й не рятує, коли сервер зник з мережі (TCP мовчить): клієнт має сам обривати з'єднання."""
    psycopg = pytest.importorskip("psycopg")
    seen: dict = {}

    class Boom(Exception):
        pass

    def fake_connect(dsn, **kw):
        seen.update(kw)
        raise psycopg.OperationalError("stop")

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    with pytest.raises(StoreError):
        PostgresStore("postgresql://u@h/d", None, 2, 3).connect()
    assert seen["tcp_user_timeout"] == 3000   # = statement_timeout: мовчання сервера довше за це обриває з'єднання
    assert seen["keepalives"] == 1 and seen["keepalives_idle"] <= 10 and seen["keepalives_interval"] <= 5 and seen["keepalives_count"] <= 3
