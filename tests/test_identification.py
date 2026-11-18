"""Ідентифікація цілі: RF-датчик повідомляє модель дрона, сервіс привʼязує її до треку й сам вирішує, свій це чи ворог."""
import math
import random

from dronewatch.fusion.models import Sensor
from dronewatch.service.catalog import DRONES, describe
from dronewatch.service.engine import Engine, Zone


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_engine(clock):
    radar = Sensor("radar1", "radar", (0, 0, 0), (5.0, 0.012, 0.026), 0.9, 1500, 0.0, "radar")
    rf = Sensor("rf1", "bearing", (400, -300, 0), (0.09,), 0.7, 1800, 0.0, "rf")
    ac = Sensor("ac1", "bearing", (-300, 200, 0), (0.17,), 0.6, 700, 0.0, "acoustic")
    return Engine([radar, rf, ac], [Zone("z", 600, "WARNING")], clock=clock, min_age_s=0.0)


def bearing(sensor_pos, target):
    return math.degrees(math.atan2(target[0] - sensor_pos[0], target[1] - sensor_pos[1])) % 360.0


def events_for(clock, targets, rng, models, rf_label_sensor="rf1"):
    """Радар + RF (+ модель з RF) для кожної цілі; models[i] = ключ моделі або None."""
    out = []
    for target, model in zip(targets, models):
        r = math.hypot(*target[:2], target[2])
        out.append({"sensor_id": "radar1", "ts": clock.now, "range_m": r + rng.gauss(0, 3),
                    "azimuth_deg": bearing((0, 0), target) + rng.gauss(0, 0.4),
                    "elevation_deg": math.degrees(math.atan2(target[2], math.hypot(*target[:2])))})
        e = {"sensor_id": rf_label_sensor, "ts": clock.now, "azimuth_deg": bearing((400, -300), target) + rng.gauss(0, 1.0)}
        if model:
            e["drone_model"] = model
        out.append(e)
    return out


def run(models, starts, velocity, steps=14, extra=None):
    clock = Clock()
    eng = make_engine(clock)
    rng = random.Random(3)
    for i in range(steps):
        pos = [(x + velocity[0] * i * 0.5, y + velocity[1] * i * 0.5, z) for (x, y, z) in starts]
        eng.ingest(events_for(clock, pos, rng, models) + (extra(clock, pos) if extra else []))
        eng.tick()
        clock.now += 0.5
    return eng


def test_catalog_decides_side_not_the_sensor():
    assert describe("geran-2")["side"] == "enemy"
    assert describe("anafi-usa")["side"] == "friend"
    assert describe("dji-mavic-3")["side"] == "unknown"
    unknown = describe("something-new")
    assert unknown is not None and unknown["side"] == "unknown"
    assert describe(None) is None
    assert all(info["side"] in ("enemy", "friend", "unknown") for info in DRONES.values())
    assert all(info["shape"] in ("quad", "delta", "straight", "loiter") for info in DRONES.values())
    assert describe("geran-2")["shape"] == "delta" and describe("anafi-usa")["shape"] == "quad"


def test_rf_model_is_attached_to_the_track():
    eng = run(["geran-2"], [(-300, 500, 80)], (15, 0))
    tracks = eng.state()["tracks"]
    assert len(tracks) == 1
    assert tracks[0]["model"] == "geran-2" and tracks[0]["side"] == "enemy" and tracks[0]["identified_by"] == "rf"
    assert tracks[0]["model_name"].startswith("Герань") and tracks[0]["shape"] == "delta"


def test_no_rf_model_means_unknown_not_a_guess():
    eng = run([None], [(-300, 500, 80)], (15, 0))
    t = eng.state()["tracks"][0]
    assert t["model"] is None and t["side"] == "unknown" and t["identified_by"] is None


def test_model_from_non_rf_sensor_is_ignored():
    """Радар, що «знає» модель, підозрілий: ідентифікацію приймаємо лише від RF-датчика."""
    eng = run([None], [(-300, 500, 80)], (15, 0))
    r = eng.ingest([{"sensor_id": "radar1", "ts": eng.clock(), "range_m": 500.0, "azimuth_deg": 10.0,
                     "elevation_deg": 5.0, "drone_model": "anafi-usa"}])
    eng.tick()
    assert all(t["model"] is None for t in eng.state()["tracks"])
    assert r[0] == 1


def test_ambiguous_bearing_does_not_guess():
    """Два цілі на майже однаковому пеленгу від RF-датчика: чия модель, невідомо, тож ніхто її не отримує."""
    starts = [(100, 100, 80), (110, 112, 80)]
    eng = run(["geran-2", "anafi-usa"], starts, (0, 0), steps=6)
    models = [t["model"] for t in eng.state()["tracks"]]
    assert "anafi-usa" not in models or "geran-2" not in models


def test_oversized_model_key_is_rejected():
    clock = Clock()
    eng = make_engine(clock)
    eng.ingest([{"sensor_id": "rf1", "ts": clock.now, "azimuth_deg": 10.0, "drone_model": "x" * 500}])
    assert eng.ident_buffer == []


def test_idents_are_dropped_with_the_track():
    eng = run(["geran-2"], [(-300, 500, 80)], (15, 0))
    assert eng.idents
    eng.reset()
    assert not eng.idents and not eng.ident_buffer
