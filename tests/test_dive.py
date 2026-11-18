"""Пікірування (кінцева фаза атаки) і типи дронів у вбудованому demo-режимі."""
import random
import time

from dronewatch.service.catalog import DRONES
from dronewatch.service.demo import DemoSource, make_sensors
from test_identification import Clock, bearing, make_engine


def fly(vz, steps=16, v=(15.0, 0.0), z0=120.0):
    clock = Clock()
    eng = make_engine(clock)
    rng = random.Random(4)
    for i in range(steps):
        t = i * 0.5
        pos = (-300 + v[0] * t, 500 + v[1] * t, max(z0 + vz * t, 1.0))
        r = (pos[0] ** 2 + pos[1] ** 2 + pos[2] ** 2) ** 0.5
        import math
        eng.ingest([
            {"sensor_id": "radar1", "ts": clock.now, "range_m": r + rng.gauss(0, 3), "azimuth_deg": bearing((0, 0), pos) + rng.gauss(0, 0.4),
             "elevation_deg": math.degrees(math.atan2(pos[2], math.hypot(pos[0], pos[1])))},
            {"sensor_id": "rf1", "ts": clock.now, "azimuth_deg": bearing((400, -300), pos) + rng.gauss(0, 1.0)},
        ])
        eng.tick()
        clock.now += 0.5
    return eng.state()["tracks"]


def test_steep_descent_is_reported_as_diving_with_time_to_ground():
    tracks = fly(vz=-8.0, steps=34, z0=250.0)
    assert len(tracks) == 1
    t = tracks[0]
    assert t["diving"] is True and t["vz"] < -3.0
    assert t["ttg"] is not None and 0 < t["ttg"] < 60


def test_level_flight_is_not_diving():
    t = fly(vz=0.0, steps=34, z0=250.0)[0]
    assert t["diving"] is False and t["ttg"] is None


def test_gentle_descent_is_not_diving():
    """Повільне зниження (глісада, ~1 м/с) не пікірування: шум висоти не має лякати оператора."""
    t = fly(vz=-1.0, steps=34, z0=250.0)[0]
    assert t["diving"] is False


def test_demo_rf_detections_carry_a_model_and_radar_never_does():
    src = DemoSource(make_sensors())
    src.started = time.time() - 60   # ніби демо йде вже хвилину: poll віддасть усі події цих 60 с
    events, _truth, _ = src.poll()
    by_sensor = {}
    for e in events:
        by_sensor.setdefault(e["sensor_id"], []).append(e)
    rf_models = {e["drone_model"] for e in by_sensor["rf1"] if "drone_model" in e}
    assert rf_models and rf_models <= set(DRONES)
    assert not any("drone_model" in e for e in by_sensor["radar1"] + by_sensor["ac1"])


def test_demo_swarm_has_a_target_that_dives_to_the_ground():
    src = DemoSource(make_sensors())
    s0 = next(t for t in src.targets if t.name == "S0")
    assert s0.wp[-1][3] == 0.0 and s0.wp[-2][3] > 30   # пікірує до землі з висоти
    assert all(t.wp[-1][3] > 0 for t in src.targets if t.name != "S0")


def test_a_brand_new_track_is_never_called_diving():
    """У щойно створеного треку оцінка вертикальної швидкості ще не зійшлась: пікірування не оголошуємо, хоч би що вона показувала."""
    for t in fly(vz=-8.0, steps=8):
        assert t["diving"] is False
