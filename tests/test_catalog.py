"""Каталог реальних моделей датчиків: жодного мовчки вигаданого числа, у кожного джерело, чесна позначка припущень."""
import pytest
from fastapi.testclient import TestClient

from dronewatch.fusion.sim import SENSOR_TYPES, make_sensor
from dronewatch.service import config
from dronewatch.service.app import app, build_runtime
from dronewatch.service.dashboard_web import DashboardAccess

REAL = {k: v for k, v in SENSOR_TYPES.items() if "real" in v}
PARAMS = {"max_range", "fov", "sigma", "pd", "clutter"}   # усе, що симуляція бере з типу датчика


def test_there_are_real_models_in_the_catalog():
    assert {"robin-iris", "robin-iris-lrm"} <= set(REAL)


@pytest.mark.parametrize("tid", sorted(REAL))
def test_every_simulated_parameter_is_either_sourced_or_marked_as_assumed(tid):
    r = REAL[tid]["real"]
    known, assumed = set(r["known"]), set(r["assumed"])
    assert known | assumed == PARAMS, "параметр без позначки: невідомо, виробник це заявив чи ми вигадали"
    assert not known & assumed


@pytest.mark.parametrize("tid", sorted(REAL))
def test_every_real_model_cites_official_sources_over_https(tid):
    r = REAL[tid]["real"]
    assert r["vendor"] and r["model"] and r["sources"]
    assert all(u.startswith("https://") for u in r["sources"])


@pytest.mark.parametrize("tid", sorted(REAL))
def test_real_models_carry_a_note_that_range_is_not_a_small_drone_detection_range(tid):
    assert "instrumented range" in REAL[tid]["real"]["note"]


def test_robin_iris_numbers_match_what_the_vendor_publishes():
    std, lrm = SENSOR_TYPES["robin-iris"], SENSOR_TYPES["robin-iris-lrm"]
    assert (std["max_range"], lrm["max_range"]) == (5000.0, 12000.0)    # 5 км стандартно, 12 км у Long Range Mode
    assert "fov_deg" not in std and "fov_deg" not in lrm                  # 360° по азимуту: круговий огляд
    assert "360" in std["real"]["known"]["fov"]


def test_a_sourced_value_is_actually_what_the_sensor_uses():
    s = make_sensor("robin-iris", "iris1", 0, 0)
    assert s.max_range == 5000.0 and s.kind == "radar" and s.type_id == "robin-iris"
    assert round(float(s.fov), 6) == round(2 * 3.141592653589793, 6)


def test_generic_models_are_not_labelled_as_real():
    assert all("real" not in v for k, v in SENSOR_TYPES.items() if k not in REAL)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    app.state.runtime = build_runtime(config.Config(mode="demo"))
    app.state.access = DashboardAccess()
    return TestClient(app)


def test_api_tells_dashboard_which_models_are_real_and_what_is_assumed(client):
    types = {t["id"]: t for t in client.get("/api/config").json()["sensor_types"]}
    assert types["robin-iris"]["real"]["vendor"] == "Robin Radar Systems"
    assert types["robin-iris"]["real"]["assumed"] == ["sigma", "pd", "clutter"]
    assert types["radar-mid"]["real"] is None


def test_a_real_model_can_be_placed_and_gets_its_own_zones(client):
    body = {"sensors": [{"id": "iris1", "type": "robin-iris", "x": 100, "y": -50}]}
    assert client.put("/api/layout", json=body).status_code == 200
    cfg = client.get("/api/config").json()
    assert cfg["sensors"][0]["max_range"] == 5000.0 and cfg["sensors"][0]["type"] == "robin-iris"
    assert cfg["zones"][0]["radius"] == 2000.0   # правило зон: 40 % дальності
    assert client.post("/api/coverage", json=body).status_code == 200
