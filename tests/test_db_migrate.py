"""dronewatch-db-migrate: схема PostgreSQL з нуля й перенесення файлу SQLite (id, хеші журналу, послідовності)."""
import hashlib
import json
import threading
import zlib
from pathlib import Path

import pytest
from tests.conftest import pg_dsn

from dronewatch.service import db_migrate
from dronewatch.service.audit import AuditEntry, AuditLog
from dronewatch.service.pg_lease import PgLeaseStore
from dronewatch.service.schema import ensure_replay_schema, ensure_schema
from dronewatch.service.store import SqliteStore

E = AuditEntry(user="alice", role="admin", action="layout_save", target="sensors=3", result="ok", ip="127.0.0.1")


@pytest.fixture
def dsn(pg_store, monkeypatch):
    monkeypatch.setattr(db_migrate, "check_pg_dsn", lambda value: None)   # тестова БД без TLS і з паролем у DSN
    monkeypatch.delenv("DRONEWATCH_PG_PASSWORD", raising=False)
    value = pg_dsn()
    assert value
    return value


def make_source(tmp_path: Path) -> Path:
    path = tmp_path / "src.db"
    db = SqliteStore(str(path)).connect()
    ensure_schema(db)
    ensure_replay_schema(db)
    for i in range(1, 4):
        db.insert_id("INSERT INTO incidents (ts, track_id, level, zone, x, y, z, speed, sensors, model, side, status, failover) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (1_790_000_000.25 + i, i, "WARNING", "A", 1.5, 2.5, 3.5, 4.5, "radar1,rf1", "geran-2", "enemy",
                      "confirmed" if i == 1 else "new", 1 if i == 3 else None))
    db.insert_id("INSERT INTO incident_reviews (incident_id, status, by, ts, note) VALUES (1,'confirmed','op',1790000001.5,'ок')")
    db.insert_id("INSERT INTO incident_reviews (incident_id, status, by, ts, note) VALUES (2,'dismissed','op',1790000002.5,NULL)")
    blob = zlib.compress(json.dumps({"v": 1, "frames": [[0, []]]}).encode())
    db.execute("INSERT INTO incident_replays (incident_id, ts, complete, data) VALUES (1, 1790000001.25, 1, ?)", (blob,))
    db.execute("INSERT INTO ha_epoch (id, epoch) VALUES (1, 9)")
    db.commit()
    log = AuditLog(db, threading.Lock(), clock=iter(range(1000, 2000)).__next__)
    for _ in range(3):
        log.append(E)
    db.close()
    return path


def pg_rows(pg_store, sql: str):
    db = pg_store.connect()
    try:
        return [tuple(r) for r in db.execute(sql).fetchall()]
    finally:
        db.close()


def sqlite_rows(path: Path, sql: str):
    db = SqliteStore(str(path)).connect(readonly=True)
    try:
        return [tuple(r) for r in db.execute(sql).fetchall()]
    finally:
        db.close()


def test_round_trip_preserves_rows_ids_and_hashes(tmp_path, pg_store, dsn):
    src = make_source(tmp_path)
    assert db_migrate.main(["--dsn", dsn, "--from", str(src)]) == 0
    for table, cols in (("incidents", "id, ts, track_id, level, zone, x, y, z, speed, sensors, model, side, status, failover"),
                        ("incident_reviews", "id, incident_id, status, by, ts, note"),
                        ("incident_replays", "incident_id, ts, complete, data"),
                        ("audit_log", 'id, ts, "user", role, action, target, result, ip, prev_hash, hash')):
        want = sqlite_rows(src, f"SELECT {cols} FROM {table} ORDER BY 1")
        got = pg_rows(pg_store, f"SELECT {cols} FROM {table} ORDER BY 1")
        assert [[bytes(v) if isinstance(v, memoryview) else v for v in r] for r in got] == [list(r) for r in want], table
    db = pg_store.connect()
    assert AuditLog(db, threading.Lock(), create=False).verify().ok
    db.close()


def test_sequences_continue_after_max(tmp_path, pg_store, dsn):
    assert db_migrate.main(["--dsn", dsn, "--from", str(make_source(tmp_path))]) == 0
    db = pg_store.connect()
    new_incident = db.insert_id("INSERT INTO incidents (ts, track_id, level, zone, x, y, z, speed, sensors) "
                                "VALUES (1, 9, 'WARNING', 'A', 0, 0, 0, 1, 'r')")
    new_review = db.insert_id("INSERT INTO incident_reviews (incident_id, status, by, ts) VALUES (1, 'new', 'op', 1)")
    db.commit()
    assert new_incident == 4 and new_review == 3
    log = AuditLog(db, threading.Lock(), create=False)
    assert log.append(E) == 4 and log.verify().ok
    db.close()


def test_refuses_non_empty_target(tmp_path, pg_store, dsn, capsys):
    src = make_source(tmp_path)
    assert db_migrate.main(["--dsn", dsn, "--from", str(src)]) == 0
    before = pg_rows(pg_store, "SELECT count(*) FROM incidents") + pg_rows(pg_store, "SELECT count(*) FROM audit_log")
    assert db_migrate.main(["--dsn", dsn, "--from", str(src)]) == 2
    assert "не порожня" in capsys.readouterr().out
    assert before == pg_rows(pg_store, "SELECT count(*) FROM incidents") + pg_rows(pg_store, "SELECT count(*) FROM audit_log")


def test_source_unchanged(tmp_path, pg_store, dsn):
    src = make_source(tmp_path)
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    assert db_migrate.main(["--dsn", dsn, "--from", str(src)]) == 0
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before


def test_ha_epoch_not_below_source(tmp_path, pg_store, dsn):
    assert db_migrate.main(["--dsn", dsn, "--from", str(make_source(tmp_path))]) == 0
    assert pg_rows(pg_store, "SELECT epoch FROM ha_epoch") == [(9,)]
    assert PgLeaseStore(pg_store).acquire("a", 5000) == 10


def test_broken_source_chain_aborts_and_target_untouched(tmp_path, pg_store, dsn, capsys):
    src = make_source(tmp_path)
    import sqlite3
    con = sqlite3.connect(src)
    con.execute("DROP TRIGGER audit_no_update")
    con.execute("UPDATE audit_log SET ip = 'x' WHERE id = 2")
    con.commit()
    con.close()
    assert db_migrate.main(["--dsn", dsn, "--from", str(src)]) == 1
    assert "id=2" in capsys.readouterr().out
    db = pg_store.connect()
    assert db.columns("incidents") == set()   # до цілі навіть не торкались
    db.close()


def test_init_only_creates_empty_schema_idempotently(pg_store, dsn):
    assert db_migrate.main(["--dsn", dsn, "--init"]) == 0
    assert db_migrate.main(["--dsn", dsn, "--init"]) == 0
    for table in ("incidents", "incident_reviews", "incident_replays", "audit_log", "audit_head", "ha_epoch", "ha_lease"):
        assert pg_rows(pg_store, f"SELECT count(*) FROM {table}")[0][0] == (1 if table == "audit_head" else 0), table


def test_missing_source_file(tmp_path, pg_store, dsn):
    assert db_migrate.main(["--dsn", dsn, "--from", str(tmp_path / "none.db")]) == 2


def test_bad_dsn_rejected_without_patch(tmp_path, capsys):
    assert db_migrate.main(["--dsn", "postgresql://u:pw@h/d", "--init"]) == 2
    assert "pw" not in capsys.readouterr().out


def test_requires_from_or_init(dsn):
    with pytest.raises(SystemExit):
        db_migrate.main(["--dsn", dsn])
