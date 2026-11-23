"""Запис і відтворення інцидентів: знімок до тривоги, дописування після неї, межі, збій запису не ламає трекінг, API."""
import sqlite3
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from dronewatch.fusion.sim import default_sensors
from dronewatch.service import app, config
from dronewatch.service.engine import Engine, Zone
from dronewatch.service.replay import ReplayOptions, ReplayRecorder, read_replay
from helpers_auth import login_as
from test_dashboard import FAST  # noqa: F401  (щоб fixture-імпорти збігались з іншими API-тестами)
from test_review import _real_alert_engine

OPTS = ReplayOptions(before_s=5.0, after_s=3.0, step_s=1.0, retention_days=30)


def alert(i=1, ts=10.0):
    return {"id": i, "ts": ts}


def rec(tmp_path, opts=OPTS):
    db = sqlite3.connect(str(tmp_path / "r.db"))
    db.execute("CREATE TABLE incidents (id INTEGER PRIMARY KEY)")
    return ReplayRecorder(db, opts), str(tmp_path / "r.db")


def frame(n):
    return lambda: [[n, 1.0, 2.0, 3.0, 0.5, 0.5, 1, "e", 0]]


SETUP = lambda: {"sensors": [], "zones": [], "origin": {"lat": 1, "lon": 2}}  # noqa: E731


def test_nothing_is_written_without_an_alert(tmp_path):
    r, _ = rec(tmp_path)
    for t in range(30):
        r.on_scan(float(t), frame(1), [], SETUP)
    assert r.db.execute("SELECT count(*) FROM incident_replays").fetchone() == (0,)
    assert len(r._ring) <= 6   # кільце не росте безмежно: лише before_s


def test_snapshot_has_pre_window_and_is_completed_after(tmp_path):
    r, path = rec(tmp_path)
    for t in range(10):
        r.on_scan(float(t), frame(t), [], SETUP)
    r.on_scan(10.0, frame(10), [alert(1, 10.0)], SETUP)
    early = read_replay(path, 1)
    assert early["complete"] is False
    assert [f[0] for f in early["frames"]] == [-5.0, -4.0, -3.0, -2.0, -1.0, 0.0]   # лише before_s, з кадром самої тривоги
    for t in (11, 12, 13, 14):
        r.on_scan(float(t), frame(t), [], SETUP)
    done = read_replay(path, 1)
    assert done["complete"] is True
    assert [f[0] for f in done["frames"]][-1] == 3.0   # after_s = 3 с, далі не пишемо
    assert done["frames"][0][1][0][0] == 5   # кадр t=5: перший у вікні


def test_alert_between_steps_still_gets_its_own_frame(tmp_path):
    r, path = rec(tmp_path)
    r.on_scan(0.0, frame(0), [], SETUP)
    r.on_scan(0.4, frame(7), [alert(2, 0.4)], SETUP)   # крок ще не минув
    f = read_replay(path, 2)["frames"]
    assert f[-1] == [0.0, [[7, 1.0, 2.0, 3.0, 0.5, 0.5, 1, "e", 0]]]


def test_two_alerts_are_independent_and_flush_completes_pending(tmp_path):
    r, path = rec(tmp_path)
    r.on_scan(1.0, frame(1), [alert(1, 1.0), alert(2, 1.0)], SETUP)
    r.flush()
    assert read_replay(path, 1)["complete"] and read_replay(path, 2)["complete"]
    assert r._pending == {} and not r._ring


def test_frame_cap_and_retention(tmp_path):
    r, path = rec(tmp_path, ReplayOptions(before_s=1.0, after_s=1.0, step_s=1.0, retention_days=1))
    r.on_scan(0.0, frame(1), [alert(1, 0.0)], SETUP)
    r.on_scan(10.0 * 86400, frame(1), [alert(2, 10.0 * 86400)], SETUP)   # через 10 діб: перший запис старший за retention
    assert read_replay(path, 1) is None and read_replay(path, 2) is not None


def test_unknown_incident_has_no_replay(tmp_path):
    _, path = rec(tmp_path)
    assert read_replay(path, 99) is None


def test_engine_records_real_alert_and_survives_recorder_failure(tmp_path, caplog):
    eng = Engine(default_sensors(), [Zone("z", 100, "WARNING")], db_path=str(tmp_path / "e.db"), replay=OPTS)
    eng.replay.on_scan = Mock(side_effect=RuntimeError("диск"))
    eng.tick()   # збій запису не піднімається з tick()
    assert "запис для відтворення" in caplog.text


def test_engine_without_replay_options_records_nothing(tmp_path):
    eng = _real_alert_engine(tmp_path)
    assert eng.replay is None
    assert eng.db.execute("SELECT count(name) FROM sqlite_master WHERE name='incident_replays'").fetchone() == (0,)


# ---------- API ----------
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    rt = app.build_runtime(config.Config(mode="demo"))
    rt.engine.db.execute("INSERT INTO incidents (id, ts, track_id, level, zone, x, y, z, speed, sensors) "
                         "VALUES (1, 1, 1, 'WARNING', 'A', 0, 0, 0, 5, 'radar1')")
    rt.engine.db.commit()
    rt.engine.replay.on_scan(1.0, frame(1), [alert(1, 1.0)], SETUP)
    rt.engine.replay.flush()
    app.app.state.runtime = rt
    c = TestClient(app.app, raise_server_exceptions=False, follow_redirects=False)
    login_as(c, "viewer")
    return c


def test_viewer_can_read_replay(client):
    r = client.get("/api/incidents/1/replay")
    assert r.status_code == 200
    body = r.json()
    assert body["incident_id"] == 1 and body["complete"] is True and body["frames"][0][1][0][7] == "e"


def test_replay_404_and_no_login_401(client):
    assert client.get("/api/incidents/2/replay").status_code == 404
    client.cookies.clear()
    assert client.get("/api/incidents/1/replay").status_code == 401


def test_corrupt_replay_is_500_not_partial(client):
    client.app.state.runtime.engine.db.execute("UPDATE incident_replays SET data=? WHERE incident_id=1", (b"not zlib",))
    client.app.state.runtime.engine.db.commit()
    assert client.get("/api/incidents/1/replay").status_code == 500


def test_config_can_disable_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    rt = app.build_runtime(config.Config(mode="demo", replay=config.ReplayCfg(enabled=False)))
    assert rt.engine.replay is None


def test_real_alert_produces_a_replay_with_the_track(tmp_path):
    import random

    from test_identification import Clock, events_for, make_engine
    clock = Clock()
    eng = Engine(make_engine(clock).sensors.values(), [Zone("z", 600, "WARNING")], clock=clock, min_age_s=0.0,
                 db_path=str(tmp_path / "x.db"), replay=ReplayOptions(before_s=30, after_s=5, step_s=0.5))
    rng = random.Random(3)
    ids = []
    for i in range(24):
        eng.ingest(events_for(clock, [(-100 + 2.5 * i, 100, 50)], rng, ["geran-2"]))
        ids += [a["id"] for a in eng.tick()]
        clock.now += 0.5
    assert ids, "тривога мала спрацювати"
    rp = read_replay(str(tmp_path / "x.db"), ids[0])
    assert rp["complete"] and rp["setup"]["sensors"] and rp["setup"]["zones"][0]["name"] == "z"
    assert any(f[1] for f in rp["frames"]) and rp["frames"][0][0] < 0 <= rp["frames"][-1][0]
