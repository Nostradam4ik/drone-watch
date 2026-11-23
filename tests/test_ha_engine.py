"""Рушій у режимі резерву: нічого не пише в справжній файл, підвищення з запобіжником epoch, тривоги, підняті в паузі."""
import random
import sqlite3

import pytest

from dronewatch.service import engine as engine_mod
from dronewatch.service.engine import Engine, Zone
from dronewatch.service.ha import FencedError, NotActiveError
from test_identification import Clock, events_for, make_engine


def make(tmp_path, *, standby: bool, name: str = "ha.db") -> Engine:
    clock = Clock()
    return Engine(make_engine(clock).sensors.values(), [Zone("z", 600, "WARNING")], clock=clock, min_age_s=0.0,
                  db_path=str(tmp_path / name), standby=standby)


def run_until_alert(eng: Engine, scans: int = 14) -> None:
    rng = random.Random(3)
    for i in range(scans):
        eng.ingest(events_for(eng.clock, [(-100 + 2.5 * i, 100, 50)], rng, ["geran-2"]))
        eng.tick()
        eng.clock.now += 0.5


def rows(path, sql):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_standby_engine_never_touches_real_file(tmp_path):
    eng = make(tmp_path, standby=True)
    run_until_alert(eng)
    assert not (tmp_path / "ha.db").exists()
    assert eng.active is False and eng.alerts


def test_activate_creates_schema_and_claims_epoch(tmp_path):
    eng = make(tmp_path, standby=True)
    eng.activate(str(tmp_path / "ha.db"), 3)
    assert eng.active is True and eng.epoch == 3
    assert rows(tmp_path / "ha.db", "SELECT epoch FROM ha_epoch") == [(3,)]
    assert rows(tmp_path / "ha.db", "SELECT name FROM sqlite_master WHERE name IN ('incidents','audit_log')")


def test_activate_with_lower_epoch_raises_fenced_with_stored(tmp_path):
    first = make(tmp_path, standby=True)
    first.activate(str(tmp_path / "ha.db"), 5)
    late = make(tmp_path, standby=True)
    with pytest.raises(FencedError) as e:
        late.activate(str(tmp_path / "ha.db"), 2)
    assert e.value.stored == 5 and late.active is False


def test_activate_same_epoch_again_is_allowed(tmp_path):
    eng = make(tmp_path, standby=True)
    eng.activate(str(tmp_path / "ha.db"), 4)
    eng.deactivate()
    eng.activate(str(tmp_path / "ha.db"), 4)   # той самий лідер повернувся
    assert eng.active is True


def test_fence_detects_newer_leader_and_deactivates(tmp_path):
    eng = make(tmp_path, standby=True)
    eng.activate(str(tmp_path / "ha.db"), 3)
    con = sqlite3.connect(tmp_path / "ha.db")
    con.execute("UPDATE ha_epoch SET epoch = 4")
    con.commit()
    con.close()
    with pytest.raises(FencedError):
        eng.fence()
    assert eng.active is False


def test_alerts_while_inactive_go_to_pending_not_db(tmp_path):
    eng = make(tmp_path, standby=True)
    run_until_alert(eng)
    assert eng.pending and all(a["id"] == 0 for a in eng.pending)
    assert eng.alerts[0] is eng.pending[-1] or eng.alerts[0] in eng.pending


def test_activate_flushes_pending_with_failover_flag(tmp_path):
    eng = make(tmp_path, standby=True)
    run_until_alert(eng)
    n = len(eng.pending)
    shown = eng.alerts[0]
    eng.activate(str(tmp_path / "ha.db"), 1)
    got = rows(tmp_path / "ha.db", "SELECT id, failover FROM incidents")
    assert len(got) == n and all(f == 1 for _, f in got)
    assert eng.pending == [] and shown["id"] > 0


def test_pending_capped_keeps_newest(tmp_path):
    eng = make(tmp_path, standby=True)
    eng._stash_pending([{"track_id": i} for i in range(engine_mod.MAX_PENDING + 5)])
    assert len(eng.pending) == engine_mod.MAX_PENDING
    assert eng.pending[-1]["track_id"] == engine_mod.MAX_PENDING + 4


def test_review_inactive_raises_not_active(tmp_path):
    eng = make(tmp_path, standby=True)
    with pytest.raises(NotActiveError):
        eng.review_incident(1, "confirmed", "op")


def test_deactivate_returns_to_memory_and_blocks_writes(tmp_path):
    eng = make(tmp_path, standby=True)
    run_until_alert(eng)
    eng.activate(str(tmp_path / "ha.db"), 1)
    before = rows(tmp_path / "ha.db", "SELECT count(*) FROM incidents")
    eng.deactivate()
    eng.deactivate()   # повторний виклик нешкідливий
    assert eng.active is False and eng.epoch is None
    assert eng.db.execute("PRAGMA database_list").fetchone()[2] == ""   # знову :memory:
    with pytest.raises(NotActiveError):
        eng.review_incident(1, "confirmed", "op")
    assert rows(tmp_path / "ha.db", "SELECT count(*) FROM incidents") == before


def test_non_ha_engine_unchanged(tmp_path):
    eng = make(tmp_path, standby=False)
    assert eng.active is True and eng.epoch is None and eng.fence() is None
    run_until_alert(eng)
    assert rows(tmp_path / "ha.db", "SELECT count(*) FROM incidents")[0][0] >= 1
