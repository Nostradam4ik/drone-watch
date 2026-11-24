"""storage.backend: конфіг, перевірка DSN, збірка сервісу з PostgreSQL, читання інцидентів і replay через Store."""
import builtins
import threading

import pytest
from pydantic import ValidationError
from tests.conftest import pg_dsn

from dronewatch.service import app, config, dashboard_redis
from dronewatch.service.audit import AuditEntry, AuditLog
from dronewatch.service.pg_lease import PgLeaseStore
from dronewatch.service.replay import read_replay
from dronewatch.service.schema import ensure_replay_schema, ensure_schema
from dronewatch.service.store import PostgresStore, StoreError, check_pg_dsn
from test_ha_lease import FakeRedis

GOOD = "postgresql://dw_app@a.internal:5432,b.internal:5432/dw?sslmode=verify-full&target_session_attrs=read-write"
LIVE = {
    "mode": "live",
    "sensors": [{"id": "r1", "type": "radar", "position": [0, 0], "max_range": 1000}],
    "zones": [{"name": "z", "radius_m": 500, "level": "CRITICAL", "center": [0, 0]}],
    "auth": {"mode": "disabled"},
    "dashboard": {"auth": "disabled"},
}
HA = {"enabled": True, "redis_url": "rediss://redis.internal"}


def cfg(**kw):
    return config.Config(**LIVE, **kw)


def test_sqlite_default_unchanged():
    c = config.Config()
    assert c.storage.backend == "sqlite" and c.ha.lease_store == "redis"
    assert c.storage.connect_timeout_s == 2 and c.storage.statement_timeout_s == 3


def test_postgres_requires_ha():
    with pytest.raises(ValidationError, match=r"ha\.enabled"):
        cfg(storage={"backend": "postgres", "dsn": GOOD})


def test_postgres_requires_dsn():
    with pytest.raises(ValidationError, match=r"storage\.dsn"):
        cfg(storage={"backend": "postgres"}, ha=HA)


@pytest.mark.parametrize(("dsn", "why"), [
    ("postgresql://u@h/d?target_session_attrs=read-write", "verify-full"),
    ("postgresql://u@h/d?sslmode=require&target_session_attrs=read-write", "verify-full"),
    ("postgresql://u@h/d?sslmode=verify-full", "read-write"),
    ("postgresql://u:pw@h/d?sslmode=verify-full&target_session_attrs=read-write", "пароль"),
    ("postgresql://u@h/d?sslmode=verify-full&target_session_attrs=read-write&password=pw", "пароль"),
    ("host=h dbname=d sslmode=verify-full", "postgresql://"),
])
def test_bad_dsn_rejected(dsn, why):
    with pytest.raises(ValueError, match=why):
        check_pg_dsn(dsn)
    with pytest.raises(ValidationError, match=why):
        cfg(storage={"backend": "postgres", "dsn": dsn}, ha=HA)


def test_good_dsn_and_lease_store_auto_resolves_to_postgres():
    check_pg_dsn(GOOD)
    c = cfg(storage={"backend": "postgres", "dsn": GOOD}, ha=HA)
    assert c.ha.lease_store == "postgres"


def test_lease_store_postgres_needs_postgres_backend():
    with pytest.raises(ValidationError, match="lease_store"):
        cfg(ha={**HA, "lease_store": "postgres"})


def test_postgres_backend_rejects_redis_lease():
    with pytest.raises(ValidationError, match="lease_store"):
        cfg(storage={"backend": "postgres", "dsn": GOOD}, ha={**HA, "lease_store": "redis"})


def test_missing_psycopg_fails_startup(monkeypatch, tmp_path):
    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("psycopg"):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setenv("DB_PATH", str(tmp_path / "i.db"))
    monkeypatch.delenv("MQTT_HOST", raising=False)
    monkeypatch.setattr(dashboard_redis, "connect", lambda url: FakeRedis())
    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(StoreError, match=r"dronewatch\[postgres\]"):
        app.build_runtime(cfg(storage={"backend": "postgres", "dsn": GOOD}, ha=HA))


def test_unreachable_postgres_fails_startup(monkeypatch, tmp_path):
    pytest.importorskip("psycopg")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "i.db"))
    monkeypatch.delenv("MQTT_HOST", raising=False)
    monkeypatch.setattr(dashboard_redis, "connect", lambda url: FakeRedis())
    dsn = "postgresql://dw_app@127.0.0.1:1/dw?sslmode=verify-full&target_session_attrs=read-write"
    with pytest.raises(StoreError) as e:
        app.build_runtime(cfg(storage={"backend": "postgres", "dsn": dsn}, ha=HA))
    assert "127.0.0.1" not in str(e.value)


def test_build_runtime_with_postgres_becomes_leader(monkeypatch, tmp_path, pg_store):
    db = pg_store.connect()
    ensure_schema(db)
    db.close()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "i.db"))
    monkeypatch.delenv("MQTT_HOST", raising=False)
    monkeypatch.setattr(dashboard_redis, "connect", lambda url: FakeRedis())
    monkeypatch.setattr(app, "open_store", lambda storage_cfg, sqlite_path: pg_store)   # тестова БД без TLS
    rt = app.build_runtime(cfg(storage={"backend": "postgres", "dsn": GOOD}, ha={**HA, "instance_id": "n1"}))
    assert rt.ha is not None and isinstance(rt.ha.elector.store, PgLeaseStore)
    rt.ha.elector.step()
    try:
        assert rt.engine.active and rt.engine.db.dialect == "postgres" and rt.engine.epoch == 1
    finally:
        rt.engine.db.close()


def test_read_incidents_through_store(db_backend):
    db = db_backend.connect()
    ensure_schema(db)
    db.insert_id("INSERT INTO incidents (ts, track_id, level, zone, x, y, z, speed, sensors) VALUES (1,2,'WARNING','A',0,0,0,5,'r')")
    db.commit()
    db.close()
    rows = app.read_incidents(db_backend, limit=10)
    assert len(rows) == 1 and rows[0]["zone"] == "A" and rows[0]["track_id"] == 2


def test_read_replay_through_store(db_backend):
    db = db_backend.connect()
    ensure_schema(db)
    ensure_replay_schema(db)
    import json
    import zlib
    blob = zlib.compress(json.dumps({"v": 1, "frames": []}).encode())
    db.execute("INSERT INTO incident_replays (incident_id, ts, complete, data) VALUES (?,?,?,?)", (5, 1.0, 1, blob))
    db.commit()
    db.close()
    found = read_replay(db_backend, 5)
    assert found is not None and found["incident_id"] == 5 and found["complete"] is True
    assert read_replay(db_backend, 6) is None


def test_incidents_api_503_when_store_down():
    from fastapi.testclient import TestClient

    from dronewatch.service.dashboard_web import DashboardAccess

    class Down:
        kind = "postgres"

        def connect(self, *, readonly=False):
            raise StoreError("OperationalError")

    app.app.state.runtime = app.Runtime(engine=app.Engine([], []), store=Down())
    app.app.state.access = DashboardAccess(sessions=None)
    r = TestClient(app.app, raise_server_exceptions=False).get("/api/incidents")
    assert r.status_code == 503 and "OperationalError" not in r.text


def test_audit_cli_verifies_postgres(pg_store, monkeypatch, capsys):
    from dronewatch.service import audit_cli
    log = AuditLog(pg_store.connect(), threading.Lock())
    log.append(AuditEntry("alice", "admin", "login", "", "ok", "1.2.3.4"))
    monkeypatch.setattr(audit_cli, "check_pg_dsn", lambda dsn: None)   # тестова БД без TLS
    monkeypatch.delenv("DRONEWATCH_PG_PASSWORD", raising=False)
    assert audit_cli.main(["--verify", "--dsn", pg_dsn() or ""]) == 0
    assert "OK: 1" in capsys.readouterr().out
    assert PostgresStore  # імпорт використано
