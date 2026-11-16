"""Стан датчиків і відмова радара. Запуск: python test_health.py (або pytest). Час фейковий, тож це секунди, а не 90 с."""
import asyncio
import random
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fusion_proto"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from demo import to_event  # noqa: E402
from engine import Engine, Zone  # noqa: E402
from sim import Target, default_sensors, simulate  # noqa: E402

BASE = 1_000_000.0
# дрон підлітає й зависає за 212 м від центру: у критичній зоні, у зоні покриття радара
HOVER = Target("H", [(0, 600, 500, 60), (25, 150, 150, 60), (200, 150, 150, 60)])
# Без радара позиція лише з пеленгів 5° і 10° на 400+ м: похибка 40-100 м це норма, а не втрата цілі
DEGRADED_GATE = 150.0


class Clock:
    def __init__(self):
        self.now = BASE

    def __call__(self):
        return self.now


def run(silent=None, heartbeats=True, duration=90.0, seed=3):
    """silent: {sensor_id: (від, до)}: у цьому проміжку датчик мовчить цілком (ні детекцій, ні heartbeat).
    Повертає журнал [(t, треків на цілі, стан радара)] і сам Engine."""
    silent = silent or {}
    clock = Clock()
    sensors = default_sensors(realistic=False)
    eng = Engine(sensors, [Zone("crit", 250, "CRITICAL")], scan_dt=0.5, min_age_s=5.0, clock=clock)
    by_name = {s.name: s for s in sensors}
    log = []
    for t, dets, truth in simulate(sensors, [HOVER], duration=duration, seed=seed):
        clock.now = BASE + t
        mute = {sid for sid, (a, b) in silent.items() if a <= t < b}
        events = [to_event(by_name[n], z, clock.now) for n, z in dets if n not in mute]
        if heartbeats:
            events += [{"sensor_id": n, "heartbeat": True} for n in by_name if n not in mute]
        eng.ingest(events)
        eng.tick()
        on_target = [tr for tr in eng.tracker.confirmed() if "H" in truth and np.linalg.norm(tr.x[:3] - truth["H"]) < DEGRADED_GATE]
        log.append((t, len(on_target), eng.sensor_status()["radar1"]["state"]))
    return log, eng


def at(log, t):
    return next(row for row in log if row[0] == t)


def test_radar_failure_keeps_track():
    """Було: через 3 с після відмови радара трек зникав і не повертався. Тепер ціль лишається по RF+акустиці."""
    log, eng = run(silent={"radar1": (30.0, 1e9)})
    assert at(log, 29.5)[1] == 1 and at(log, 29.5)[2] == "OK"
    for t in (33.0, 40.0, 60.0, 89.5):
        assert at(log, t)[1] == 1, f"трек на цілі зник на t={t}"
    ids = {tr.id for tr in eng.tracker.confirmed()}
    assert len(ids) == 1, f"має бути один безперервний трек, а не {ids}"
    assert at(log, 40.0)[2] == "LOST"
    assert eng.degraded() == ["radar1"]


def test_radar_recovers():
    log, eng = run(silent={"radar1": (30.0, 60.0)})
    assert at(log, 45.0)[2] == "LOST"
    assert at(log, 70.0)[2] == "OK"
    assert at(log, 70.0)[1] == 1, "після повернення радара трек має лишитися"
    assert eng.degraded() == []


def test_critical_alert_without_radar():
    """Радара немає від самого початку: лише два пеленги з великим шумом. Трек буде неточним і місцями блимати,
    але тривога в критичній зоні має спрацювати (раніше радарне правило не давало їй зʼявитися взагалі)."""
    for seed in (1, 2, 3, 4):
        _, eng = run(silent={"radar1": (0.0, 1e9)}, seed=seed)
        assert any(a["level"] == "CRITICAL" for a in eng.alerts), f"тривоги немає, seed={seed}"
        assert all("radar" not in a["sensors"] for a in eng.alerts)


def test_status_transitions():
    clock = Clock()
    eng = Engine(default_sensors(), [Zone("z", 100, "WARNING")], sensor_timeout_s=5.0, clock=clock)
    assert eng.sensor_status()["rf1"]["state"] == "WAITING"
    eng.ingest([{"sensor_id": "rf1", "heartbeat": True}])
    assert eng.sensor_status()["rf1"]["state"] == "OK"
    clock.now += 6.0
    assert eng.sensor_status()["rf1"]["state"] == "LOST"
    assert eng.sensor_status()["ac1"]["state"] == "LOST"      # так і не озвався
    eng.ingest([{"sensor_id": "rf1", "ts": clock.now, "azimuth_deg": 10.0}])
    assert eng.sensor_status()["rf1"]["state"] == "OK"        # звичайна детекція теж доводить, що живий


def test_bad_events_do_not_count_as_alive():
    clock = Clock()
    eng = Engine(default_sensors(), [Zone("z", 100, "WARNING")], clock=clock)
    ok, bad = eng.ingest([{"sensor_id": "ghost", "heartbeat": True},               # невідомий датчик
                          {"sensor_id": "rf1", "ts": clock.now - 60, "azimuth_deg": 1.0},   # застаріла
                          {"sensor_id": "rf1", "ts": clock.now}])                   # без азимута
    assert (ok, bad) == (0, 3)
    assert eng.sensor_status()["rf1"]["state"] == "WAITING"


def test_http_schema_accepts_heartbeat():
    """Регрес: схема POST /api/events вимагала ts і азимут, тож heartbeat отримував 422. Потребує fastapi і paho-mqtt."""
    try:
        from app import Detection
    except ImportError as e:
        print("skip: service deps missing:", e)
        return
    d = Detection(sensor_id="radar1", heartbeat=True)
    clock = Clock()
    eng = Engine(default_sensors(), [Zone("z", 100, "WARNING")], clock=clock)
    assert eng.ingest([d.model_dump()]) == (1, 0)
    assert eng.sensor_status()["radar1"]["state"] == "OK"
    # детекція без ts/азимута, як і раніше, відхиляється рушієм
    assert eng.ingest([Detection(sensor_id="rf1").model_dump()]) == (0, 1)


class _Task:  # заміна asyncio.Task для health()
    def __init__(self, done=False):
        self._done = done

    def done(self):
        return self._done


def _app_with_engine():
    """Підготувати модуль app без запуску сервера. None, якщо немає залежностей сервісу."""
    try:
        import app
    except ImportError as e:
        print("skip: service deps missing:", e)
        return None
    app.engine = Engine(default_sensors(), [Zone("z", 100, "WARNING")])
    app.demo_src = app.mqtt_ingest = None
    app.clients = set()
    app.scan_task = _Task()
    app.scan_health.update(started=time.time(), last_ok=None, errors=0, consecutive=0, last_error=None)
    return app


def test_scan_error_does_not_stop_loop():
    app = _app_with_engine()
    if app is None:
        return
    real_tick, resets = app.engine.tick, []
    app.engine.reset = lambda: resets.append(1)

    def boom():
        raise RuntimeError("зіпсований стан")
    app.engine.tick = boom
    for _ in range(app.SCAN_ERRORS_BEFORE_RESET):
        asyncio.run(app.scan_step())            # виняток не виходить назовні
    sh = app.scan_health
    assert (sh["errors"], sh["consecutive"]) == (5, 5)
    assert "RuntimeError" in sh["last_error"] and app.engine.stats["scan_errors"] == 5
    assert resets == [1], "після серії збоїв трекер має скинутися рівно раз"

    app.engine.tick = real_tick                 # збій минув: скан знову проходить, лічильник серії скидається
    asyncio.run(app.scan_step())
    assert sh["consecutive"] == 0 and sh["errors"] == 5 and sh["last_ok"] is not None
    assert app.health()["ok"] is True


def test_health_reports_dead_scanning():
    app = _app_with_engine()
    if app is None:
        return
    asyncio.run(app.scan_step())
    assert app.health()["ok"] is True
    app.scan_health["last_ok"] = time.time() - 100        # скани давно не йшли
    r = app.health()
    assert r.status_code == 503 and b'"ok":false' in r.body
    app.scan_health["last_ok"] = time.time()
    app.scan_task = _Task(done=True)                      # цикл помер зовсім
    assert app.health().status_code == 503


def _flood(eng, sensor_id, n, rng=None):
    rng = rng or random.Random(1)
    ev = []
    for _ in range(n):
        e = {"sensor_id": sensor_id, "ts": eng.clock(), "azimuth_deg": rng.uniform(0, 360)}
        if sensor_id == "radar1":
            e.update(range_m=rng.uniform(50, 1400), elevation_deg=rng.uniform(0, 30))
        ev.append(e)
    return ev


def test_buffer_is_capped_per_sensor():
    eng = Engine(default_sensors(), [Zone("z", 100, "WARNING")], max_events_per_sensor=50)
    ok, bad = eng.ingest(_flood(eng, "rf1", 500))
    assert (ok, bad) == (50, 450)
    assert len(eng.buffer) == 50 and eng.stats["events_in"] == 50 and eng.stats["events_dropped"] == 450
    assert eng.ingest(_flood(eng, "rf1", 10)) == (0, 10), "буфер вже повний: стеля діє між пакетами, а не лише всередині одного"
    assert eng.ingest(_flood(eng, "ac1", 30)) == (30, 0), "потоп від одного датчика не має відбирати місце в інших"
    eng.tick()
    assert len(eng.buffer) == 0 and eng.ingest(_flood(eng, "rf1", 5)) == (5, 0), "після скану місце звільняється"


def test_track_and_triangulation_caps():
    eng = Engine(default_sensors(), [Zone("z", 100, "WARNING")], max_events_per_sensor=200, max_tracks=5)
    for _ in range(4):
        eng.ingest(_flood(eng, "radar1", 100) + _flood(eng, "rf1", 100) + _flood(eng, "ac1", 100))
        eng.tick()
        assert len(eng.tracker.tracks) <= 5
    assert eng.stats["tracks_capped"] > 0
    eng2 = Engine(default_sensors(), [Zone("z", 100, "WARNING")], max_events_per_sensor=200, max_tracks=500)
    eng2.ingest(_flood(eng2, "rf1", 100) + _flood(eng2, "ac1", 100))
    t0 = time.perf_counter()
    eng2.tick()
    assert eng2.stats["bearings_truncated"] == 140, eng2.stats      # 2 датчики x (100 - 30)
    assert time.perf_counter() - t0 < 2.0, "тріангуляція 100x100 пеленгів не вкладається в бюджет"


def test_readers_wait_for_lock():
    """Детерміновано: поки tick() (чи будь-хто) тримає замок, читачі стану чекають, а не читають напівоновлене."""
    eng = Engine(default_sensors(), [Zone("z", 100, "WARNING")])
    done = {}

    def call(name, fn):
        fn()
        done[name] = True
    readers = {"state": eng.state, "sensor_status": eng.sensor_status, "degraded": eng.degraded, "count": lambda: eng.count("scans"),
               "ingest": lambda: eng.ingest([{"sensor_id": "rf1", "heartbeat": True}])}
    with eng.lock:
        threads = [threading.Thread(target=call, args=(n, f), daemon=True) for n, f in readers.items()]
        for t in threads:
            t.start()
        time.sleep(0.3)
        assert done == {}, f"читачі не чекали замка: {sorted(done)}"
    for t in threads:
        t.join(2)
    assert set(done) == set(readers)


def test_concurrent_ingest_tick_state_is_consistent():
    """Стрес: кілька потоків ingest + потік MQTT-лічильників + tick + state. Жодних винятків, жодного втраченого приросту."""
    eng = Engine(default_sensors(), [Zone("z", 100, "WARNING")], max_events_per_sensor=20)
    errors, stop = [], threading.Event()
    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)   # потоки перемикаються на кожному кроці: без цього гонки майже не відтворюються
    sent, per_thread = [0] * 4, 300
    start = threading.Barrier(8)   # усі виробники й лічильники стартують разом, інакше вони не перетинаються в часі

    def producer(i):
        try:
            start.wait()
            rng = random.Random(i)
            for _ in range(per_thread):
                batch = _flood(eng, ("rf1", "ac1")[i % 2], 5, rng)
                sent[i] += len(batch)
                eng.ingest(batch)
        except Exception as e:
            errors.append(e)

    def counter():
        try:
            start.wait()
            for _ in range(20000):
                eng.count("events_rejected")
        except Exception as e:
            errors.append(e)

    def ticker_and_reader():
        try:
            while not stop.is_set():
                eng.tick()
                eng.state()
                eng.degraded()
        except Exception as e:
            errors.append(e)

    work = [threading.Thread(target=producer, args=(i,)) for i in range(4)] + [threading.Thread(target=counter) for _ in range(4)]
    bg = threading.Thread(target=ticker_and_reader)
    bg.start()
    for t in work:
        t.start()
    for t in work:
        t.join(60)
    stop.set()
    bg.join(60)
    sys.setswitchinterval(old_interval)
    eng.tick()
    assert not errors, errors
    st = eng.stats
    assert st["events_in"] + st["events_dropped"] + st["events_rejected"] - 4 * 20000 == sum(sent), st
    assert st["events_rejected"] >= 4 * 20000


def test_http_batch_limit_and_incidents_clamp():
    app = _app_with_engine()
    if app is None:
        return
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print("skip: no httpx:", e)
        return
    client = TestClient(app.app)     # без with: lifespan (конфіг, цикл сканів) не запускається, engine підставлено вручну
    beat = {"sensor_id": "rf1", "heartbeat": True}
    r = client.post("/api/events", json=[beat] * app.MAX_HTTP_BATCH)
    assert r.status_code == 200 and r.json()["accepted"] == app.MAX_HTTP_BATCH
    assert client.post("/api/events", json=[beat] * (app.MAX_HTTP_BATCH + 1)).status_code == 422

    with tempfile.TemporaryDirectory() as d:
        app.DB_PATH = str(Path(d) / "inc.db")
        db = sqlite3.connect(app.DB_PATH)
        db.execute("CREATE TABLE incidents (id INTEGER PRIMARY KEY, ts REAL)")
        db.executemany("INSERT INTO incidents (ts) VALUES (?)", [(i,) for i in range(5)])
        db.commit()
        db.close()
        assert len(client.get("/api/incidents?limit=3").json()) == 3
        assert len(client.get("/api/incidents?limit=-1").json()) == 1, "limit=-1 раніше віддавав усю таблицю"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
