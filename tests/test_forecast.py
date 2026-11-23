"""Прогноз руху цілі і розбір зникнення: де ціль буде далі та куди могла подітись, коли її раптом перестали бачити."""
import math
import random

import numpy as np

from dronewatch.fusion.models import Sensor
from dronewatch.service import forecast as fc
from dronewatch.service.engine import Engine, Zone
from test_identification import Clock, bearing, make_engine


def state_x(x, y, z, vx, vy, vz):
    return np.array([x, y, z, vx, vy, vz], dtype=float)


def test_forecast_follows_the_current_velocity_and_the_circle_grows_with_time():
    P = np.diag([4.0, 4.0, 9.0, 1.0, 1.0, 1.0])
    pts = fc.forecast(state_x(100, 200, 60, 10, -5, 0), P)
    assert [p["t"] for p in pts] == list(fc.FORECAST_HORIZONS)
    for p in pts:   # за прямою: положення + швидкість * час
        assert abs(p["x"] - (100 + 10 * p["t"])) < 0.06 and abs(p["y"] - (200 - 5 * p["t"])) < 0.06
    assert pts[0]["r"] < pts[1]["r"] < pts[2]["r"]
    # без похибки оцінювання розкид це лише маневр: ~ a t^2 / 2 помножене на sqrt(chi2 68 %)
    exact = fc.forecast(state_x(0, 0, 0, 0, 0, 0), np.zeros((6, 6)), horizons=(10.0,), accel=2.0)[0]
    assert abs(exact["r"] - 2.0 * 10 ** 2 / 2 * math.sqrt(fc.CHI2_68_2D)) < 0.1


def test_search_area_moves_along_the_velocity_and_is_capped():
    snap = {"x": 0.0, "y": 0.0, "vx": 10.0, "vy": 0.0, "err": 6.0}
    a5, a30 = fc.search_area(snap, 5.0), fc.search_area(snap, 30.0)
    assert a5["fx"] == 50.0 and a5["fy"] == 0.0
    assert a30["fx"] == 300.0 and a30["r"] > a5["r"] >= 6.0
    far = fc.search_area(snap, 1000.0)
    assert far["fx"] == 10.0 * fc.SEARCH_COAST_S and far["r"] == fc.SEARCH_MAX_M   # довго не екстраполюємо, радіус обмежений


def test_loss_reason_prefers_the_most_specific_explanation():
    base = {"diving": False, "radar_miss": 0, "radar_miss_limit": 6, "misses": 5, "max_misses": 6, "alt_known": True, "z": 120.0}
    assert fc.loss_reason({**base, "diving": True}, sensors_down=False, edge=False) == "dive"
    assert fc.loss_reason({**base, "radar_miss": 5, "misses": 0}, sensors_down=False, edge=False) == "radar_silent"
    assert fc.loss_reason(base, sensors_down=True, edge=True) == "sensor_down"
    assert fc.loss_reason({**base, "z": 10.0}, sensors_down=False, edge=True) == "low"
    assert fc.loss_reason({**base, "alt_known": False, "z": 10.0}, sensors_down=False, edge=True) == "edge"   # невиміряна висота «низько» не доводить
    assert fc.loss_reason(base, sensors_down=False, edge=False) == "unknown"


def fly(eng, clock, steps, z0=120.0, vz=0.0, start=(-300.0, 500.0), v=(15.0, 0.0), seed=4):
    rng = random.Random(seed)
    for i in range(steps):
        t = i * 0.5
        pos = (start[0] + v[0] * t, start[1] + v[1] * t, max(z0 + vz * t, 1.0))
        r = math.sqrt(pos[0] ** 2 + pos[1] ** 2 + pos[2] ** 2)
        eng.ingest([
            {"sensor_id": "radar1", "ts": clock.now, "range_m": r + rng.gauss(0, 3), "azimuth_deg": bearing((0, 0), pos) + rng.gauss(0, 0.4),
             "elevation_deg": math.degrees(math.atan2(pos[2], math.hypot(pos[0], pos[1])))},
            {"sensor_id": "rf1", "ts": clock.now, "azimuth_deg": bearing((400, -300), pos) + rng.gauss(0, 1.0)},
        ])
        eng.tick()
        clock.now += 0.5


def silence(eng, clock, scans):
    """Датчики живі (шлють heartbeat), але нічого не бачать: ціль зникла з неба, а не відмовила апаратура."""
    for _ in range(scans):
        eng.ingest([{"sensor_id": sid, "heartbeat": True} for sid in ("radar1", "rf1", "ac1")])
        eng.tick()
        clock.now += 0.5


def test_state_carries_a_forecast_for_every_track():
    clock = Clock()
    eng = make_engine(clock)
    fly(eng, clock, 24)
    (t,) = eng.state()["tracks"]
    f = t["forecast"]
    assert [p["t"] for p in f] == list(fc.FORECAST_HORIZONS)
    assert abs(f[1]["x"] - (t["x"] + t["vx"] * 10)) < 1.0   # округлення x і vx до 0,1 дає до ~1 м   # прогноз за поточною швидкістю
    assert f[0]["r"] < f[2]["r"]


def test_vanished_target_is_reported_with_where_it_was_and_where_to_look():
    clock = Clock()
    eng = make_engine(clock)
    fly(eng, clock, 24)
    (t,) = eng.state()["tracks"]
    assert eng.state()["lost"] == []
    silence(eng, clock, 14)   # трек доживає MAX_MISSES сканів без детекцій і зникає
    st = eng.state()
    assert st["tracks"] == [] and len(st["lost"]) == 1
    rec = st["lost"][0]
    assert rec["id"] == t["id"] and rec["reason"] == "unknown" and rec["age"] > 0
    assert abs(rec["x"] - t["x"]) < 60 and abs(rec["vx"] - t["vx"]) < 3   # де бачили востаннє і з якою швидкістю
    s = rec["search"]
    assert s["fx"] > rec["x"] and s["r"] >= rec["err"]   # шукати далі по курсу, радіус не менший за похибку позиції


def test_diving_target_that_vanishes_is_reported_as_a_probable_impact_at_the_predicted_spot():
    clock = Clock()
    eng = make_engine(clock)
    fly(eng, clock, 34, z0=250.0, vz=-8.0)
    (t,) = eng.state()["tracks"]
    assert t["diving"] is True
    silence(eng, clock, 14)
    (rec,) = eng.state()["lost"]
    assert rec["reason"] == "dive"
    ttg = rec["z"] / 8.0
    assert abs(rec["search"]["fx"] - (rec["x"] + rec["vx"] * ttg)) < 40   # шукаємо в точці, де вона мала впасти, а не далі по прямій


def test_lost_entries_expire_and_a_reset_does_not_fabricate_losses():
    clock = Clock()
    eng = make_engine(clock)
    fly(eng, clock, 24)
    silence(eng, clock, 14)
    assert len(eng.state()["lost"]) == 1
    clock.now += fc.LOST_KEEP_S + 5
    eng.tick()
    assert eng.state()["lost"] == []   # старий запис зник сам
    clock2 = Clock()
    eng2 = make_engine(clock2)
    fly(eng2, clock2, 24)
    eng2.reset()
    eng2.tick()
    assert eng2.state()["lost"] == []   # скинутий сеанс це не «зникла ціль»


def test_merged_duplicate_track_is_not_a_lost_target():
    """Трек, що зник, бо злився з сусіднім (поруч той самий дрон), не «втрачений»: його просто більше нема окремо."""
    clock = Clock()
    radar = Sensor("radar1", "radar", (0, 0, 0), (5.0, 0.012, 0.026), 0.9, 1500, 0.0, "radar")
    eng = Engine([radar], [Zone("z", 600, "WARNING")], clock=clock, min_age_s=0.0)
    eng.snaps[999] = {"id": 999, "t": 0.0, "x": 100.0, "y": 100.0, "z": 50.0, "vx": 1.0, "vy": 0.0, "vz": 0.0, "err": 5.0,
                      "diving": False, "alt_known": True, "misses": 0, "radar_miss": 0, "radar_miss_limit": 6, "max_misses": 6,
                      "age": 30.0, "model_name": None, "side": "unknown", "level": "NONE"}
    # поруч є живий підтверджений трек: імітуємо його у трекері
    from dronewatch.fusion.tracker import Track
    tr = Track(np.array([110.0, 100.0, 50.0, 1.0, 0.0, 0.0]), np.eye(6), 0.0, 1)
    tr.state = "CONFIRMED"
    eng.tracker.tracks.append(tr)
    eng._track_losses(1.0, set())
    assert 999 not in eng.lost and 999 not in eng.snaps
