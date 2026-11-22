"""Висоту цілі міряє лише радар. Трек лише з пеленгів стартує з припущених ASSUMED_ALT (60 м), і показувати це число
як виміряну висоту (і рахувати від нього час до землі) не можна: рушій позначає alt_known, а UI пише «—»."""
import math
import random

from dronewatch.fusion.models import Sensor
from dronewatch.service.engine import Engine, Zone
from test_identification import Clock, bearing, events_for, make_engine


def bearings_only_engine(clock):
    rf = Sensor("rf1", "bearing", (400, -300, 0), (0.09,), 0.7, 1800, 0.0, "rf")
    ac = Sensor("ac1", "bearing", (-300, 200, 0), (0.17,), 0.6, 700, 0.0, "acoustic")
    return Engine([rf, ac], [Zone("z", 600, "WARNING")], clock=clock, min_age_s=0.0, require_radar=False)


def test_bearing_only_track_has_unknown_altitude_and_no_time_to_ground():
    clock, rng = Clock(), random.Random(5)
    eng = bearings_only_engine(clock)
    for i in range(16):
        target = (100 + 10 * i * 0.5, 150, 120)   # справжня висота 120 м: рушій її не знає
        eng.ingest([{"sensor_id": "rf1", "ts": clock.now, "azimuth_deg": bearing((400, -300), target) + rng.gauss(0, 1)},
                    {"sensor_id": "ac1", "ts": clock.now, "azimuth_deg": bearing((-300, 200), target) + rng.gauss(0, 1)}])
        eng.tick()
        clock.now += 0.5
    tracks = eng.state()["tracks"]
    assert tracks, "пеленги двох датчиків мали дати трек"
    assert all(t["alt_known"] is False and t["ttg"] is None for t in tracks)


def test_radar_track_has_known_altitude_until_radar_goes_quiet():
    clock, rng = Clock(), random.Random(3)
    eng = make_engine(clock)
    eng.require_radar = False   # інакше трек без радара прибирається, а перевіряємо саме показ висоти
    pos = [(-300, 500, 80)]
    for _ in range(14):
        eng.ingest(events_for(clock, pos, rng, [None]))
        eng.tick()
        clock.now += 0.5
    t = eng.state()["tracks"][0]
    assert t["alt_known"] is True and math.isclose(t["z"], 80, abs_tol=25)
    for _ in range(14):   # 7 с лише RF: висота вже не підтверджена радаром
        eng.ingest([e for e in events_for(clock, pos, rng, [None]) if e["sensor_id"] != "radar1"])
        eng.tick()
        clock.now += 0.5
    tracks = eng.state()["tracks"]
    assert tracks and all(t["alt_known"] is False for t in tracks)
