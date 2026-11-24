"""Журнал дій на PostgreSQL: ланцюжок, audit_head, незмінність, права ролі dw_app (і спільні сценарії на обох бекендах)."""
import threading
from pathlib import Path

import pytest
from tests.conftest import pg_dsn

from dronewatch.service.audit import GENESIS, AuditEntry, AuditLog, VerifyResult
from dronewatch.service.store import PostgresStore, StoreError

E = AuditEntry(user="alice", role="admin", action="layout_save", target="sensors=3", result="started", ip="127.0.0.1")
ROLES_SQL = Path(__file__).resolve().parents[1] / "deploy" / "ha-cluster" / "postgres-roles.sql"


def _log(store, **kw):
    return AuditLog(store.connect(), threading.Lock(), clock=iter(range(1000, 9000)).__next__, **kw)


def test_chain_and_verify_on_both_backends(db_backend):
    log = _log(db_backend)
    log.append(E)
    log.append(AuditEntry("bob", "operator", "login", "", "ok", "1.2.3.4"))
    rows = log.read()
    assert [r["user"] for r in rows] == ["bob", "alice"]   # саме імена, а не current_user
    assert rows[1]["prev_hash"] == GENESIS and rows[0]["prev_hash"] == rows[1]["hash"]
    assert log.verify() == VerifyResult(ok=True, rows=2, broken_at=None, head=rows[0]["hash"])


def test_empty_log_verifies(db_backend):
    assert _log(db_backend).verify() == VerifyResult(ok=True, rows=0, broken_at=None, head=GENESIS)


def test_append_assigns_gapless_ids_after_rollback(pg_store):
    log = _log(pg_store)
    log.append(E)
    with pytest.raises(StoreError):   # збій посеред вставки: id не лишає діри
        log._db.execute("SELECT 1/0")
    log._db.rollback()
    log.append(E)
    assert [r["id"] for r in log.read()] == [2, 1]
    assert log.verify().ok


def test_update_delete_truncate_rejected(pg_store):
    log = _log(pg_store)
    log.append(E)
    for sql in ("UPDATE audit_log SET \"user\"='mallory'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
        with pytest.raises(StoreError) as e:
            log._db.execute(sql)
        assert e.value.sqlstate == "P0001", sql
        log._db.rollback()


def test_truncated_tail_detected(pg_store):
    log = _log(pg_store)
    for _ in range(3):
        log.append(E)
    db = pg_store.connect()
    db.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_immutable")
    db.execute("DELETE FROM audit_log WHERE id = 3")
    db.commit()
    res = _log(pg_store).verify()
    assert res.ok is False and res.broken_at == 3


def test_replaced_head_detected(pg_store):
    log = _log(pg_store)
    for _ in range(2):
        log.append(E)
    db = pg_store.connect()
    db.execute("UPDATE audit_head SET head_hash = ?", ("f" * 64,))
    db.commit()
    assert _log(pg_store).verify().ok is False


def test_concurrent_appends_keep_chain(pg_store):
    _log(pg_store)   # створює таблиці
    errors: list[Exception] = []

    def work() -> None:
        try:
            log = AuditLog(pg_store.connect(), threading.Lock(), create=False)
            for _ in range(25):
                log.append(E)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    res = _log(pg_store).verify()
    assert res.ok and res.rows == 200


def test_app_role_can_append_but_not_modify(pg_store):
    _log(pg_store)
    admin = pg_store.connect()
    admin.execute(ROLES_SQL.read_text(encoding="utf-8"))
    admin.execute("ALTER ROLE dw_app PASSWORD 'apppw'")
    admin.commit()
    dsn = pg_dsn()
    assert dsn
    app = PostgresStore(dsn.replace("postgres:x@", "dw_app@"), "apppw", 2, 3).connect()
    log = AuditLog(app, threading.Lock(), create=False)
    log.append(E)
    assert log.verify().ok
    for sql in ("UPDATE audit_log SET ip = 'x'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
        with pytest.raises(StoreError) as e:
            app.execute(sql)
        assert e.value.sqlstate == "42501", sql   # insufficient_privilege
        app.rollback()
