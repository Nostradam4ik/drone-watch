"""Секторний огляд: датчик дивиться в один бік (heading) на ширину fov, а не навколо себе."""
import types

import numpy as np
import pytest
from fastapi.testclient import TestClient

from dronewatch.emulator.emulate import clutter, in_sector
from dronewatch.fusion.sim import SENSOR_TYPES, Target, make_sensor, simulate
from dronewatch.fusion.tracker import Tracker
from dronewatch.service import config
from dronewatch.service.app import app, build_runtime
from dronewatch.service.config import SensorCfg, build_sensors
from dronewatch.service.coverage import BLIND, GOOD, coverage
from dronewatch.service.dashboard_web import DashboardAccess


def hits(sensor, target_xy, seconds=20):
    """Скільки справжніх детекцій дав датчик по нерухомій цілі в точці target_xy (без хибних)."""
    sensor.clutter_rate = 0.0
    tg = Target("T", [(0, *target_xy, 50), (seconds, *target_xy, 50)])
    return sum(len(dets) for _, dets, _ in simulate([sensor], [tg], duration=seconds, seed=3))


@pytest.mark.parametrize(("heading", "target", "sees"), [
    (0, (0, 500), True),       # дивиться на північ, ціль на півночі
    (0, (500, 0), False),      # ціль на сході, поза сектором 120° (±60° від півночі)
    (90, (500, 0), True),      # повернули на схід: тепер бачить
    (90, (0, 500), False),
    (0, (0, -500), False),     # ціль позаду
    (0, (400, 400), True),     # 45° від осі: усередині ±60°
    (0, (500, 100), False),    # ~79° від осі: ззовні
])
def test_sector_radar_only_detects_targets_inside_its_sector(heading, target, sees):
    s = make_sensor("radar-sector", "r1", 0, 0, heading_deg=heading)
    assert (hits(s, target) > 0) is sees


def test_full_circle_sensors_are_unaffected_by_heading():
    for heading in (0, 90, 200):
        assert hits(make_sensor("radar-mid", "r1", 0, 0, heading_deg=heading), (500, -300)) > 0


def test_in_fov_handles_the_wraparound_at_north():
    s = make_sensor("radar-sector", "r1", 0, 0, heading_deg=350)   # сектор 290°..50°: перетинає північ
    assert s.in_fov((10, 500)) and s.in_fov((-100, 500)) and not s.in_fov((500, 0)) and not s.in_fov((0, -500))


def test_sensor_position_matters_not_only_heading():
    s = make_sensor("radar-sector", "r1", 1000, 1000, heading_deg=180)   # дивиться на південь
    assert s.in_fov((1000, 200)) and not s.in_fov((1000, 1800))


def test_clutter_stays_inside_the_sector():
    s = make_sensor("radar-sector", "r1", 0, 0, heading_deg=90)
    s.clutter_rate = 5.0
    dets = [z for _, d, _ in simulate([s], [], duration=40, seed=1) for _, z in d]
    assert len(dets) > 20
    assert all(abs(((np.pi / 2 - z[1]) - np.pi / 2 + np.pi) % (2 * np.pi) - np.pi) <= np.radians(60) + 1e-9 for z in dets)   # азимут у математичній системі


def test_tracker_does_not_expect_a_radar_to_see_a_target_outside_its_sector():
    r = make_sensor("radar-sector", "r1", 0, 0, heading_deg=0)
    tr = Tracker([r])
    inside = types.SimpleNamespace(x=np.array([0.0, 500.0, 50.0, 0, 0, 0]))
    outside = types.SimpleNamespace(x=np.array([500.0, 0.0, 50.0, 0, 0, 0]))
    assert tr._cover_radars(inside) == [r] and tr._cover_radars(outside) == []


# ---------- покриття ----------
def test_coverage_of_a_sector_radar_is_only_in_front_of_it():
    r = coverage([make_sensor("radar-sector", "r1", 0, 0, heading_deg=0)])
    s = r["cell"]

    def at(x, y):
        return next(c for a, b, c in r["cells"] if (a, b) == (int(x // s), int(y // s)))
    assert at(0, 300) == GOOD and at(0, -300) == BLIND and at(300, 0) == BLIND
    assert 25 < r["stats"]["good"] < 40   # 120° із 360°: близько третини зони


def test_turning_the_radar_moves_the_covered_part_of_the_zone():
    north = coverage([make_sensor("radar-sector", "r1", 0, 0, heading_deg=0)])
    east = coverage([make_sensor("radar-sector", "r1", 0, 0, heading_deg=90)])
    n, e = ({(a, b) for a, b, c in r["cells"] if c == GOOD} for r in (north, east))
    assert n and e
    assert all(b >= -1 for _, b in n) and all(a >= -1 for a, _ in e)   # північний сектор над радаром, східний праворуч від нього
    assert len(n & e) / len(n) < 0.35                                   # сектори 120° перекриваються лише 30° із 120°


# ---------- конфіг і емулятор ----------
def test_live_config_sensors_can_declare_a_sector():
    cfg = SensorCfg(id="r1", type="radar", position=[0, 0], max_range=1000, heading_deg=90, fov_deg=120)
    s = build_sensors(config.Config.model_construct(sensors=[cfg]))[0]
    assert s.in_fov((500, 0)) and not s.in_fov((0, 500))
    assert np.degrees(s.fov) == pytest.approx(120)


@pytest.mark.parametrize("bad", [{"heading_deg": 360}, {"heading_deg": -1}, {"fov_deg": 0}, {"fov_deg": 361}])
def test_bad_sector_values_in_config_are_rejected(bad):
    with pytest.raises(ValueError, match=r"heading_deg|fov_deg"):
        SensorCfg(id="r1", type="radar", position=[0, 0], max_range=1000, **bad)


def test_emulator_respects_the_sector_and_old_configs_stay_circular():
    sector = {"id": "r1", "pos": [0, 0], "max_range": 1000, "heading_deg": 0, "fov_deg": 90}
    assert in_sector(sector, 100, 500) and not in_sector(sector, 500, 100) and not in_sector(sector, 0, -300)
    assert in_sector({"id": "old", "pos": [0, 0], "max_range": 1000}, -700, -700)   # без fov_deg: круговий
    import random
    rng = random.Random(1)
    assert all(abs(((clutter(rng, {**sector, "kind": "bearing"})["azimuth_deg"] + 180) % 360) - 180) <= 45 for _ in range(200))


# ---------- API ----------
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    app.state.runtime = build_runtime(config.Config(mode="demo"))
    app.state.access = DashboardAccess()
    return TestClient(app)


def test_palette_offers_a_sector_radar_and_config_reports_heading_and_fov(client):
    types_ = {t["id"]: t for t in client.get("/api/config").json()["sensor_types"]}
    assert types_["radar-sector"]["fov_deg"] == 120.0 and types_["radar-mid"]["fov_deg"] == 360.0
    assert all("fov_deg" in t for t in types_.values())
    body = {"sensors": [{"id": "r1", "type": "radar-sector", "x": 10, "y": 20, "heading": 135.5}]}
    assert client.put("/api/layout", json=body).status_code == 200
    s = client.get("/api/config").json()["sensors"][0]
    assert (s["heading_deg"], s["fov_deg"]) == (135.5, 120.0)


def test_heading_defaults_to_north_and_must_be_a_compass_angle(client):
    client.put("/api/layout", json={"sensors": [{"id": "r1", "type": "radar-sector", "x": 0, "y": 0}]})
    assert client.get("/api/config").json()["sensors"][0]["heading_deg"] == 0.0
    for bad in (360, -0.1, 720, "north", None):
        body = {"sensors": [{"id": "r1", "type": "radar-sector", "x": 0, "y": 0, "heading": bad}]}
        assert client.put("/api/layout", json=body).status_code == 422, bad


def test_all_palette_types_still_build():
    for tid in SENSOR_TYPES:
        s = make_sensor(tid, "x1", 0, 0, 45)
        assert np.degrees(s.fov) == pytest.approx(SENSOR_TYPES[tid].get("fov_deg", 360.0))
