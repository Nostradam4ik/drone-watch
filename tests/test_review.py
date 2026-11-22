"""Рішення оператора по тривогах: підтвердити/відхилити, автор, час, коментар, історія, статистика."""
import random
import sqlite3
from unittest.mock import Mock

import pytest

from dronewatch.fusion.sim import default_sensors
from dronewatch.service.audit import AuditEntry, AuditError
from dronewatch.service.engine import Engine, Zone
from test_identification import Clock, events_for, make_engine


@pytest.fixture
def eng(tmp_path):
    return Engine(default_sensors(), [Zone("z", 100, "WARNING")], db_path=str(tmp_path / "i.db"))


def _insert(eng, zone="A", ts=1.0, track=1):
    cur = eng.db.execute("INSERT INTO incidents (ts, track_id, level, zone, x, y, z, speed, sensors) "
                         "VALUES (?,?,'WARNING',?,0,0,0,5,'radar1')", (ts, track, zone))
    eng.db.commit()
    return cur.lastrowid


def _real_alert_engine(tmp_path):
    clock = Clock()
    eng = Engine(make_engine(clock).sensors.values(), [Zone("z", 600, "WARNING")], clock=clock, min_age_s=0.0,
                 db_path=str(tmp_path / "r.db"))
    rng = random.Random(3)
    for i in range(14):
        eng.ingest(events_for(clock, [(-100 + 2.5 * i, 100, 50)], rng, ["geran-2"]))
        eng.tick()
        clock.now += 0.5
    return eng


def test_new_columns_and_table_exist_on_fresh_db(eng):
    cols = {r[1] for r in eng.db.execute("PRAGMA table_info(incidents)")}
    assert {"status", "reviewed_by", "reviewed_at", "review_note"} <= cols
    assert eng.db.execute("SELECT count(*) FROM incident_reviews").fetchone() == (0,)


def test_migrates_legacy_db(tmp_path):
    db = str(tmp_path / "old.db")
    with sqlite3.connect(db) as old:
        old.execute("CREATE TABLE incidents (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, track_id INTEGER, level TEXT, "
                    "zone TEXT, x REAL, y REAL, z REAL, speed REAL, sensors TEXT)")
        old.execute("INSERT INTO incidents (ts, track_id, level, zone, x, y, z, speed, sensors) VALUES (1,1,'WARNING','A',0,0,0,1,'r')")
    e = Engine(default_sensors(), [], db_path=db)
    assert e.db.execute("SELECT status FROM incidents").fetchone() == (None,)
    assert e.incident_stats() == [{"zone": "A", "status": "new", "count": 1}]


def test_review_sets_fields_and_history(eng):
    i = _insert(eng)
    r = eng.review_incident(i, "dismissed", "op", "дрон лелеки")
    assert r["status"] == "dismissed" and r["reviewed_by"] == "op" and r["reviewed_at"] > 0
    assert r["review_note"] == "дрон лелеки"
    r = eng.review_incident(i, "confirmed", "op2")
    assert r["status"] == "confirmed" and r["reviewed_by"] == "op2" and r["review_note"] is None
    rows = eng.db.execute("SELECT status, by FROM incident_reviews WHERE incident_id=? ORDER BY id", (i,)).fetchall()
    assert rows == [("dismissed", "op"), ("confirmed", "op2")]


def test_same_status_twice_adds_history_row(eng):
    i = _insert(eng)
    eng.review_incident(i, "confirmed", "op")
    eng.review_incident(i, "confirmed", "op")
    assert eng.db.execute("SELECT count(*) FROM incident_reviews").fetchone() == (2,)


def test_review_unknown_id_returns_none(eng):
    assert eng.review_incident(999, "confirmed", "op") is None


def test_empty_note_becomes_null_and_long_note_truncated(eng):
    i = _insert(eng)
    assert eng.review_incident(i, "confirmed", "op", "   ")["review_note"] is None
    assert len(eng.review_incident(i, "confirmed", "op", "x" * 900)["review_note"]) == 500


def test_review_updates_alert_in_memory_and_tolerates_missing(eng):
    i = _insert(eng)
    eng.alerts = [{"id": i, "status": "new", "reviewed_by": None, "reviewed_at": None, "review_note": None}]  # type: ignore[list-item]
    eng.review_incident(i, "dismissed", "op", "n")
    assert eng.alerts[0]["status"] == "dismissed" and eng.alerts[0]["reviewed_by"] == "op"
    j = _insert(eng, track=2)
    assert eng.review_incident(j, "confirmed", "op")["status"] == "confirmed"   # j немає в engine.alerts


def test_alert_has_id_and_new_status(tmp_path):
    eng = _real_alert_engine(tmp_path)
    a = eng.alerts[0]
    assert a["status"] == "new" and a["reviewed_by"] is None
    row = eng.db.execute("SELECT id, status FROM incidents WHERE id=?", (a["id"],)).fetchone()
    assert row == (a["id"], "new")


def test_incident_stats_groups_by_zone_and_status(eng):
    a = _insert(eng, "A", ts=100.0)
    b = _insert(eng, "A", ts=200.0, track=2)
    c = _insert(eng, "B", ts=300.0, track=3)
    eng.review_incident(b, "dismissed", "op")
    eng.review_incident(c, "confirmed", "op")
    stats = {(r["zone"], r["status"]): r["count"] for r in eng.incident_stats()}
    assert stats == {("A", "new"): 1, ("A", "dismissed"): 1, ("B", "confirmed"): 1}
    assert {(r["zone"], r["status"]) for r in eng.incident_stats(since_ts=150.0)} == {("A", "dismissed"), ("B", "confirmed")}
    assert a


def test_review_does_not_touch_max_level(tmp_path):
    eng = _real_alert_engine(tmp_path)
    before = dict(eng.max_level)
    eng.review_incident(eng.alerts[0]["id"], "dismissed", "op")
    assert eng.max_level == before and before


def _read_status(eng, iid):
    return eng.db.execute("SELECT status FROM incidents WHERE id=?", (iid,)).fetchone()[0]


def test_review_writes_audit_in_the_same_transaction(eng):
    iid = _insert(eng)
    eng.review_incident(iid, "confirmed", "alice", "ok",
                        audit=AuditEntry("alice", "operator", "review", f"incident={iid} status=confirmed", "ok", "?"))
    assert eng.audit.read()[0]["action"] == "review"
    assert _read_status(eng, iid) == "confirmed"


def test_review_rolls_back_when_audit_fails(eng, monkeypatch):
    iid = _insert(eng)
    monkeypatch.setattr(eng.audit, "append_locked", Mock(side_effect=sqlite3.OperationalError("disk full")))
    with pytest.raises(AuditError):
        eng.review_incident(iid, "confirmed", "alice", audit=AuditEntry("alice", "operator", "review", "x", "ok", "?"))
    assert _read_status(eng, iid) in (None, "new")   # рішення не записалось
    assert eng.db.execute("SELECT count(*) FROM incident_reviews").fetchone() == (0,)
