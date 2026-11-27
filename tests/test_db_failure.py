"""Збій сховища під час роботи лідера: тривоги в pending, вимикач, фонове перепідключення, без дублів після повернення."""
import contextlib
import logging
import sqlite3
import threading
import time

import pytest

from dronewatch.service.engine import Engine, Zone
from dronewatch.service.store import Db, GuardedDb, SqliteStore, StoreError
from test_ha_engine import run_until_alert
from test_identification import Clock, make_engine


class Ctl:
    """Керування «мережею»: down (усе падає), lose_ack (commit дійшов, відповідь загубилась), fail_on (збій на запиті), gate (connect чекає)."""

    def __init__(self) -> None:
        self.down = False
        self.lose_ack = False
        self.fail_on: str | None = None
        self.calls = 0
        self.connects = 0
        self.gate: threading.Event | None = None


class FlakyDb:
    def __init__(self, real: Db, ctl: Ctl) -> None:
        self.real, self.ctl, self.dialect = real, ctl, real.dialect

    def _gate(self, sql: str = "") -> None:
        self.ctl.calls += 1
        if self.ctl.down or (self.ctl.fail_on and sql.startswith(self.ctl.fail_on)):
            raise StoreError("OperationalError")

    def execute(self, sql, params=()):
        self._gate(sql)
        return self.real.execute(sql, params)

    def insert_id(self, sql, params=()):
        self._gate(sql)
        return self.real.insert_id(sql, params)

    def columns(self, table):
        self._gate()
        return self.real.columns(table)

    def commit(self):
        self._gate()
        self.real.commit()
        if self.ctl.lose_ack:
            raise StoreError("OperationalError")

    def rollback(self):
        self.real.rollback()

    def close(self):
        self.real.close()


class FlakyStore:
    kind = "postgres"

    def __init__(self, path: str, ctl: Ctl) -> None:
        self.path, self.ctl = path, ctl

    def connect(self, *, readonly: bool = False):
        self.ctl.connects += 1
        if self.ctl.gate is not None:
            self.ctl.gate.wait(5)
        if self.ctl.down:
            raise StoreError("OperationalError")
        return FlakyDb(SqliteStore(self.path).connect(), self.ctl)


def make(tmp_path):
    clock, ctl = Clock(), Ctl()
    path = str(tmp_path / "f.db")
    eng = Engine(make_engine(clock).sensors.values(), [Zone("z", 600, "WARNING")], clock=clock, min_age_s=0.0, db_path=path)
    eng.db = GuardedDb(FlakyStore(path, ctl), FlakyDb(eng.db, ctl), clock=clock)
    return eng, ctl, path


def rows(path: str, sql: str):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_guard_skips_inner_during_breaker_and_reconnects_in_background(tmp_path):
    clock, ctl = Clock(), Ctl()
    path = str(tmp_path / "g.db")
    guard = GuardedDb(FlakyStore(path, ctl), FlakyDb(SqliteStore(path).connect(), ctl), breaker_s=5.0, clock=clock)
    guard.execute("CREATE TABLE t (x INTEGER)")
    ctl.down = True
    with pytest.raises(StoreError):
        guard.execute("SELECT 1")
    calls = ctl.calls
    for _ in range(20):   # вимикач: мережі не торкаємось
        with pytest.raises(StoreError):
            guard.execute("SELECT 1")
    assert ctl.calls == calls and ctl.connects == 0 and guard.healthy is False
    ctl.down = False
    clock.now += 5.1
    with pytest.raises(StoreError):   # цей виклик лише запускає перепідключення у фоні й не чекає його
        guard.execute("SELECT 1")
    guard.wait_reconnect(2)
    assert ctl.connects == 1 and guard.healthy is True
    assert guard.execute("SELECT count(*) FROM t").fetchone()[0] == 0


def test_sql_error_is_not_an_outage_but_rolls_back(tmp_path):
    clock, ctl = Clock(), Ctl()
    path = str(tmp_path / "s.db")
    guard = GuardedDb(FlakyStore(path, ctl), FlakyDb(SqliteStore(path).connect(), ctl), clock=clock)
    guard.execute("CREATE TABLE t (x INTEGER)")
    guard.commit()

    class Boom(StoreError):
        pass

    real_execute = guard._inner.execute

    def sql_error(sql, params=()):
        raise StoreError("IntegrityError", sqlstate="23505")

    guard._inner.execute = sql_error
    with pytest.raises(StoreError) as e:
        guard.execute("INSERT INTO t VALUES (1)")
    assert e.value.sqlstate == "23505" and guard.healthy is True   # з'єднання лишилось
    guard._inner.execute = real_execute


def test_alert_goes_to_pending_when_db_fails(tmp_path):
    eng, ctl, path = make(tmp_path)
    ctl.down = True
    run_until_alert(eng, 20)
    assert eng.alerts and eng.pending
    assert rows(path, "SELECT count(*) FROM incidents") == [(0,)]
    assert eng.db_healthy is False


def test_pending_flushed_once_after_recovery(tmp_path):
    eng, ctl, path = make(tmp_path)
    ctl.down = True
    run_until_alert(eng, 20)
    shown = len(eng.alerts)
    assert shown >= 1 and len(eng.pending) == shown
    eng.db.wait_reconnect(2)   # спроба з часів збою має провалитись до зсуву годинника, інакше вона відсуне вимикач ще на breaker_s
    ctl.down = False
    eng.clock.now += 6
    for _ in range(3):
        eng.tick()
        eng.clock.now += 0.5
        eng.db.wait_reconnect(2)
    assert not eng.pending
    assert rows(path, "SELECT count(*) FROM incidents") == [(shown,)]
    assert all(a["id"] for a in eng.alerts)


def test_lost_ack_does_not_duplicate(tmp_path):
    """commit дійшов до БД, а відповідь загубилась: тривога і в БД, і в pending; після повернення дедуплікація лишає один рядок."""
    eng, ctl, path = make(tmp_path)
    ctl.lose_ack = True
    run_until_alert(eng, 20)
    ctl.lose_ack = False
    eng.clock.now += 6
    for _ in range(3):
        eng.tick()
        eng.clock.now += 0.5
        eng.db.wait_reconnect(2)
    assert not eng.pending
    stored = rows(path, "SELECT count(*) FROM incidents")[0][0]
    assert stored == len(eng.alerts) >= 1   # по одному рядку на тривогу, дублів нема


def test_tick_does_not_wait_for_reconnect(tmp_path):
    """Замок рушія не тримається, поки йде перепідключення: tick повертається одразу, навіть якщо connect завис."""
    eng, ctl, _ = make(tmp_path)
    ctl.down = True
    run_until_alert(eng, 3)
    ctl.down = False
    ctl.gate = threading.Event()   # connect висить
    eng.clock.now += 6
    started = time.monotonic()
    for _ in range(5):
        eng.tick()
    assert time.monotonic() - started < 1.0
    ctl.gate.set()
    eng.db.wait_reconnect(2)


def test_review_during_outage_raises_and_changes_nothing(tmp_path):
    eng, ctl, path = make(tmp_path)
    run_until_alert(eng, 20)
    iid = rows(path, "SELECT id FROM incidents")[0][0]
    ctl.down = True
    with pytest.raises(StoreError):
        eng.review_incident(iid, "confirmed", "op")
    assert all(a.get("status") in (None, "new") for a in eng.alerts)
    ctl.down = False
    eng.clock.now += 6
    with pytest.raises(StoreError):
        eng.review_incident(iid, "confirmed", "op")   # перше звернення після вимикача лише запускає перепідключення
    eng.db.wait_reconnect(2)
    assert eng.review_incident(iid, "confirmed", "op")["status"] == "confirmed"


def test_failed_transaction_rolled_back(tmp_path):
    eng, ctl, path = make(tmp_path)
    run_until_alert(eng, 20)
    iid = rows(path, "SELECT id FROM incidents")[0][0]
    ctl.fail_on = "UPDATE incidents SET status"   # INSERT у incident_reviews пройшов, UPDATE ні
    with pytest.raises(StoreError):
        eng.review_incident(iid, "confirmed", "op")
    ctl.fail_on = None
    eng.clock.now += 6
    with pytest.raises(StoreError):
        eng.db.execute("SELECT 1")
    eng.db.wait_reconnect(2)
    assert rows(path, "SELECT count(*) FROM incident_reviews") == [(0,)]   # напівтранзакції немає


def test_outage_logged_once_not_per_scan(tmp_path, caplog):
    eng, ctl, _ = make(tmp_path)
    ctl.down = True
    with caplog.at_level(logging.INFO, logger="dronewatch"):
        run_until_alert(eng, 20)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR and "сховище" in r.getMessage().lower()]
    assert len(errors) == 1, [r.getMessage() for r in errors]
    assert "OperationalError" in errors[0].getMessage() and not errors[0].exc_info


def test_review_api_503_without_driver_text(tmp_path):
    from test_review_api import _make
    client, sessions = _make(tmp_path, login=True)
    client.cookies.set("dw_session", sessions.create("operator", "operator"))
    from dronewatch.service import app
    eng = app.app.state.runtime.engine
    ctl = Ctl()
    eng.db = GuardedDb(FlakyStore(str(tmp_path / "i.db"), ctl), FlakyDb(eng.db, ctl))
    ctl.down = True
    r = client.post("/api/incidents/1/review", json={"status": "confirmed"})
    assert r.status_code == 503 and "OperationalError" not in r.text


def test_health_reports_storage(tmp_path):
    from test_review_api import _make
    client, sessions = _make(tmp_path, login=True)
    client.cookies.set("dw_session", sessions.create("operator", "operator"))
    from dronewatch.service import app
    rt = app.app.state.runtime
    assert client.get("/api/health").json()["storage"] == {"ok": True, "backend": "sqlite"}
    ctl = Ctl()
    rt.engine.db = GuardedDb(FlakyStore(str(tmp_path / "i.db"), ctl), FlakyDb(rt.engine.db, ctl))
    ctl.down = True
    with contextlib.suppress(StoreError):
        rt.engine.db.execute("SELECT 1")
    assert client.get("/api/health").json()["storage"]["ok"] is False


def test_guard_recovers_after_postgres_backend_killed(pg_store):
    """Справжній PostgreSQL: з'єднання вбито ззовні (як при перемиканні master), охоронець перепідключається сам."""
    guard = GuardedDb(pg_store, pg_store.connect(), breaker_s=0.2)
    pid = guard.execute("SELECT pg_backend_pid()").fetchone()[0]
    admin = pg_store.connect()
    admin.execute("SELECT pg_terminate_backend(?)", (pid,))
    admin.commit()
    with pytest.raises(StoreError):
        guard.execute("SELECT 1")
    assert guard.healthy is False
    time.sleep(0.3)
    with pytest.raises(StoreError):   # перший виклик після вимикача лише запускає перепідключення
        guard.execute("SELECT 1")
    guard.wait_reconnect(5)
    assert guard.execute("SELECT 1").fetchone()[0] == 1
