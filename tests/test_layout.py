"""Розстановка датчиків на карті (demo): палітра типів, перевірка вводу, підміна датчиків у рушії й симуляторі."""
import math

import pytest
from fastapi.testclient import TestClient

from dronewatch.fusion.sim import SENSOR_TYPES, make_sensor
from dronewatch.service import config
from dronewatch.service.app import app, build_runtime
from dronewatch.service.dashboard_web import DashboardAccess
from dronewatch.service.demo import DemoSource, make_sensors, to_event
from dronewatch.service.engine import Engine, Zone
from dronewatch.service.layout import LIMIT_M, MAX_SENSORS
from helpers_auth import login_as


def placed(sid, typ, x=0.0, y=0.0):
    return {"id": sid, "type": typ, "x": x, "y": y}


def one_radar():
    return {"sensors": [placed("r1", "radar-mid")]}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    app.state.runtime = build_runtime(config.Config(mode="demo"))
    app.state.access = DashboardAccess()   # вхід вимкнено: анонім лише viewer, тож редагувати розкладку входимо як admin
    client = TestClient(app)
    login_as(client, "admin")
    return client


# ---------- палітра ----------
def test_palette_has_several_radar_types_that_really_differ():
    radars = {k: v for k, v in SENSOR_TYPES.items() if v["kind"] == "radar" and "fov_deg" not in v}   # круговий огляд
    assert len(radars) >= 3
    ranges = sorted(v["max_range"] for v in radars.values())
    assert ranges == sorted(set(ranges))   # дальності різні
    assert radars["radar-short"]["sigma"][0] < radars["radar-long"]["sigma"][0]   # ближній точніший за дальній


@pytest.mark.parametrize("type_id", sorted(SENSOR_TYPES))
def test_make_sensor_follows_its_preset(type_id):
    p = SENSOR_TYPES[type_id]
    s = make_sensor(type_id, "x1", 120.0, -80.0)
    assert (s.kind, s.label, s.type_id, s.max_range) == (p["kind"], p["label"], type_id, p["max_range"])
    assert s.pos.tolist() == [120.0, -80.0, 0.0]
    assert s.dim == (3 if p["kind"] == "radar" else 1)
    assert not s.bias.any() and s.latency == 0.0   # ідеальний: зсувів і затримок немає


def test_builtin_demo_sensors_know_their_palette_type():
    types = {s.name: s.type_id for s in make_sensors()}
    assert types == {"radar1": "radar-mid", "rf1": "rf", "ac1": "acoustic"}


# ---------- API ----------
def test_config_tells_dashboard_it_can_edit_and_what_to_offer(client):
    c = client.get("/api/config").json()
    assert c["editable"] is True and c["max_sensors"] == MAX_SENSORS
    assert {t["id"] for t in c["sensor_types"]} == set(SENSOR_TYPES)
    assert all("type" in s for s in c["sensors"])


def test_config_reports_sensor_height_and_it_survives_a_layout_save(client):
    """Редактор повертає розстановку з конфігу: без поля height у конфігу кожне збереження скидало б висоту датчика в нуль."""
    assert all(s["height"] == 0.0 for s in client.get("/api/config").json()["sensors"])
    body = {"sensors": [{**placed("r1", "radar-mid"), "height": 12.5}]}
    assert client.put("/api/layout", json=body).status_code == 200
    assert [s["height"] for s in client.get("/api/config").json()["sensors"]] == [12.5]


def test_put_layout_replaces_sensors_and_bumps_revision(client):
    before = client.get("/api/config").json()["layout_rev"]
    body = {"sensors": [placed("alpha", "radar-long", 100, 200), placed("rf9", "rf", -300, 50), placed("ac9", "acoustic", 40, 40)]}
    r = client.put("/api/layout", json=body)
    assert r.status_code == 200
    cfg = client.get("/api/config").json()
    assert [(s["id"], s["type"], s["pos"], s["max_range"]) for s in cfg["sensors"]] == [
        ("alpha", "radar-long", [100.0, 200.0], 3000.0), ("rf9", "rf", [-300.0, 50.0], 1800.0), ("ac9", "acoustic", [40.0, 40.0], 700.0)]
    assert cfg["layout_rev"] == before + 1 == r.json()["layout_rev"]
    assert client.get("/api/state").json()["layout_rev"] == cfg["layout_rev"]
    assert {s["id"] for s in client.get("/api/sensors").json()} == {"alpha", "rf9", "ac9"}


def test_delete_layout_restores_builtin_sensors(client):
    client.put("/api/layout", json=one_radar())
    assert client.delete("/api/layout").status_code == 200
    assert [s["id"] for s in client.get("/api/config").json()["sensors"]] == ["radar1", "rf1", "ac1"]


@pytest.mark.parametrize("body", [
    {"sensors": [placed("bad id", "radar-mid")]},                                  # пробіл в id
    {"sensors": [placed("1abc", "radar-mid")]},                                    # id з цифри
    {"sensors": [placed("a" * 17, "radar-mid")]},                                  # задовгий id
    {"sensors": [placed("r1", "tank")]},                                           # невідомий тип
    {"sensors": [placed("r1", "radar-mid", LIMIT_M + 1, 0)]},                      # за межами
    {"sensors": [placed("r1", "radar-mid", 0, -LIMIT_M - 1)]},
    {"sensors": [placed("r1", "radar-mid"), placed("r1", "radar-short", 5, 5)]},   # однакові id
    {"sensors": []},                                                               # порожньо
    {"sensors": [placed("rf1", "rf")]},                                            # лише один пеленговий
    {"sensors": [placed(f"r{i}", "radar-mid", i, 0) for i in range(MAX_SENSORS + 1)]},   # забагато
    {"sensors": [{"id": "r1", "type": "radar-mid", "x": 0, "y": 0, "pd": 1.0}]},   # зайве поле
    {"sensors": [{"id": "r1", "type": "radar-mid", "x": "0", "y": None}]},         # не числа
    {},
])
def test_bad_layouts_are_rejected_and_change_nothing(client, body):
    before = client.get("/api/config").json()["sensors"]
    assert client.put("/api/layout", json=body).status_code == 422
    assert client.get("/api/config").json()["sensors"] == before


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_coordinates_are_rejected(client, literal):
    raw = '{"sensors": [{"id": "r1", "type": "radar-mid", "x": ' + literal + ', "y": 0}]}'
    assert client.put("/api/layout", content=raw, headers={"Content-Type": "application/json"}).status_code == 422


def test_two_bearing_sensors_are_enough_without_a_radar(client):
    body = {"sensors": [placed("rf1", "rf", 300, 0), placed("ac1", "acoustic", -300, 0)]}
    assert client.put("/api/layout", json=body).status_code == 200


def zones(client):
    return [(z["name"], z["level"], z["radius"], z["center"]) for z in client.get("/api/config").json()["zones"]]


def test_demo_starts_with_the_zones_of_its_builtin_radar(client):
    assert zones(client) == [("Буферна зона radar1", "WARNING", 600.0, [0.0, 0.0]), ("Критична зона radar1", "CRITICAL", 250.0, [0.0, 0.0])]


def test_every_radar_brings_its_own_zones_scaled_by_its_range(client):
    body = {"sensors": [placed("far", "radar-long", 100, 200), placed("near", "radar-short", -300, 50)]}
    assert client.put("/api/layout", json=body).status_code == 200
    assert zones(client) == [("Буферна зона far", "WARNING", 1200.0, [100.0, 200.0]), ("Критична зона far", "CRITICAL", 500.0, [100.0, 200.0]),
                             ("Буферна зона near", "WARNING", 320.0, [-300.0, 50.0]), ("Критична зона near", "CRITICAL", 133.3, [-300.0, 50.0])]


def test_zones_follow_a_moved_added_and_removed_radar(client):
    client.put("/api/layout", json={"sensors": [placed("r1", "radar-mid", 0, 0)]})
    client.put("/api/layout", json={"sensors": [placed("r1", "radar-mid", 400, -250)]})   # радар пересунули: зони поїхали з ним
    assert {tuple(c) for *_, c in zones(client)} == {(400.0, -250.0)}
    client.put("/api/layout", json={"sensors": [placed("r1", "radar-mid", 400, -250), placed("r2", "radar-short", -500, 0)]})   # додали
    assert [n for n, *_ in zones(client)] == ["Буферна зона r1", "Критична зона r1", "Буферна зона r2", "Критична зона r2"]
    client.put("/api/layout", json={"sensors": [placed("r2", "radar-short", -500, 0)]})   # прибрали перший: його зони зникли
    assert [n for n, *_ in zones(client)] == ["Буферна зона r2", "Критична зона r2"]


def test_rf_and_acoustic_sensors_do_not_make_zones_but_a_bearing_only_layout_still_has_some(client):
    body = {"sensors": [placed("r1", "radar-mid", 0, 0), placed("rf1", "rf", 900, 900), placed("ac1", "acoustic", -900, -900)]}
    client.put("/api/layout", json=body)
    assert {n for n, *_ in zones(client)} == {"Буферна зона r1", "Критична зона r1"}
    client.put("/api/layout", json={"sensors": [placed("rf1", "rf", 300, 100), placed("ac1", "acoustic", 100, 300)]})
    assert zones(client) == [("Буферна зона", "WARNING", 600.0, [200.0, 200.0]), ("Критична зона", "CRITICAL", 250.0, [200.0, 200.0])]


def test_delete_layout_brings_the_builtin_zones_back(client):
    client.put("/api/layout", json={"sensors": [placed("x", "radar-long", 700, 700)]})
    client.delete("/api/layout")
    assert zones(client)[0][3] == [0.0, 0.0] and len(zones(client)) == 2


def test_a_zone_that_moved_with_its_radar_decides_where_alerts_fire():
    import numpy as np

    from dronewatch.service.layout import derive_zones
    here = Engine([make_sensor("radar-mid", "r1", 0, 0)], derive_zones([make_sensor("radar-mid", "r1", 0, 0)]))
    point = np.array([1500.0, 0.0, 50.0])   # далеко від радара в нулі
    assert here._highest_zone(point) is None
    moved = [make_sensor("radar-mid", "r1", 1500, 0)]
    here.set_sensors(moved, derive_zones(moved))
    zone = here._highest_zone(point)
    assert zone is not None and zone.level == "CRITICAL"   # точка тепер у центрі критичної зони радара


def test_layout_is_refused_outside_demo(client):
    app.state.runtime.demo_src = None   # так виглядає бойовий режим: симулятора нема, датчики з конфігу з ключами
    assert client.put("/api/layout", json=one_radar()).status_code == 409
    assert client.delete("/api/layout").status_code == 409
    assert client.get("/api/config").json()["editable"] is False


# ---------- рушій ----------
def test_engine_set_sensors_swaps_them_and_starts_a_fresh_session():
    now = [1000.0]
    eng = Engine(make_sensors(), [Zone("z", 600, "WARNING")], clock=lambda: now[0], sensor_timeout_s=5.0)
    now[0] += 100   # рушій працює давно: старий відлік «від старту» зробив би новий датчик одразу LOST
    eng.ingest([{"sensor_id": "rf1", "heartbeat": True}])
    eng.set_sensors([make_sensor("radar-short", "new1", 10, 10), make_sensor("rf", "rf1", 0, 0)])
    st = {s["id"]: s["state"] for s in eng.sensor_status().values()}
    assert set(st) == {"new1", "rf1"}                       # radar1 і ac1 зникли
    assert st["new1"] == "WAITING"                           # щойно додали: очікуємо, а не втрата
    assert st["rf1"] == "OK"                                 # лишився: його heartbeat не загубився
    assert eng.config()["layout_rev"] == 1 and eng.state()["tracks"] == []
    eng.ingest([{"sensor_id": "radar1", "heartbeat": True}])   # прибраний датчик більше не приймається
    assert eng.stats["events_rejected"] >= 1
    now[0] += 6
    assert {s["id"]: s["state"] for s in eng.sensor_status().values()}["new1"] == "LOST"   # а мовчання далі рахується як відмова


# ---------- розстановка справді впливає на трекінг ----------
def run_demo(sensors, seconds=45.0):
    src = DemoSource(sensors, seed=5)
    now = [1000.0]
    eng = Engine(sensors, [Zone("z", 600, "WARNING")], clock=lambda: now[0], min_age_s=5.0)
    best = 0
    for t, dets, _truth in src.steps:
        if t > seconds:
            break
        now[0] = 1000.0 + t
        eng.ingest([to_event(src.sensors[n], z, now[0]) for n, z in dets] + [{"sensor_id": n, "heartbeat": True} for n in src.sensors])
        eng.tick()
        best = max(best, len(eng.state()["tracks"]))
    return best


def test_radar_in_the_middle_sees_the_swarm_but_one_far_away_sees_nothing():
    assert run_demo([make_sensor("radar-mid", "r1", 0, 0)]) >= 3
    far = run_demo([make_sensor("radar-short", "r1", 4500, 4500)])   # рій літає в межах ~1 км, дальність 800 м
    assert far == 0


def test_longer_range_radar_picks_up_targets_earlier_than_a_short_one():
    short = run_demo([make_sensor("radar-short", "r1", 0, 0)], seconds=12.0)
    long = run_demo([make_sensor("radar-long", "r1", 0, 0)], seconds=12.0)
    assert long >= short


def test_demo_source_set_sensors_restarts_the_simulation():
    src = DemoSource(make_sensors(), seed=1)
    src.i = 50
    src.set_sensors([make_sensor("radar-mid", "r1", 0, 0)])
    assert list(src.sensors) == ["r1"] and src.i == 0
    assert math.isclose(src.steps[0][0], 0.0)
