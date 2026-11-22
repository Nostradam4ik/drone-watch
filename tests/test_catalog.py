"""Каталог реальних моделей датчиків: числа з даташитів, жодного мовчки вигаданого параметра, PDF лежать у проєкті."""
import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dronewatch.fusion.sim import SENSOR_TYPES, make_sensor
from dronewatch.service import config
from dronewatch.service.app import app, build_runtime
from dronewatch.service.dashboard_web import DashboardAccess
from helpers_auth import login_as

ROOT = Path(__file__).resolve().parent.parent
REAL = {k: v for k, v in SENSOR_TYPES.items() if "real" in v}
PARAMS = {"max_range", "fov", "sigma", "pd", "clutter"}   # усе, що симуляція бере з типу датчика


def test_catalog_has_the_models_we_have_datasheets_for():
    assert set(REAL) == {"robin-iris", "echodyne-echoguard", "blighter-a422", "blighter-a402"}


@pytest.mark.parametrize("tid", sorted(REAL))
def test_every_simulated_parameter_is_either_sourced_or_marked_as_assumed(tid):
    r = REAL[tid]["real"]
    known, assumed = set(r["known"]), set(r["assumed"])
    assert known | assumed >= PARAMS, "параметр без позначки: невідомо, виробник це заявив чи ми вигадали"
    assert known | assumed <= PARAMS | {"sigma_range"}
    assert not known & assumed


@pytest.mark.parametrize("tid", sorted(REAL))
def test_every_real_model_cites_a_vendor_https_source_and_a_local_datasheet(tid):
    r = REAL[tid]["real"]
    assert r["vendor"] and r["model"] and r["sources"] and r["files"] and r["note"]
    assert all(u.startswith("https://") for u in r["sources"])
    for f in r["files"]:
        path = ROOT / f
        if not path.is_file():   # PDF не комітяться (.gitignore): на чистому клоні їх немає, і це нормально
            pytest.skip(f"{f}: даташит лише локально, у репозиторії його немає")
        assert path.read_bytes()[:5] == b"%PDF-", f"{f}: це не PDF"


def test_robin_iris_numbers_match_the_product_sheet():
    s = SENSOR_TYPES["robin-iris"]
    assert s["max_range"] == 3100.0                                       # клас Micro C1, 0,9 кг
    assert "fov_deg" not in s                                             # 360° по азимуту
    assert s["sigma"][0] == 0.6 and s["sigma"][1:] == (0.75, 1.0)         # дальність 0,6 м; кути 0,75° і 1°
    assert set(s["real"]["assumed"]) == {"pd", "clutter"}                 # точність є в даташиті, виявлення й хибних немає


def test_echoguard_numbers_match_the_datasheet():
    s = SENSOR_TYPES["echodyne-echoguard"]
    assert s["fov_deg"] == 120.0 and s["max_range"] == 1000.0             # 120° азимут; понад 1 км для Phantom 4
    assert s["sigma"][1:] == (1.0, 1.5)                                   # менше 1° азимут, менше 1,5° кут місця
    assert "sigma_range" in s["real"]["assumed"]                          # точності по дальності в даташиті немає


def test_blighter_variants_differ_only_in_how_wide_they_look():
    a422, a402 = SENSOR_TYPES["blighter-a422"], SENSOR_TYPES["blighter-a402"]
    assert (a422["fov_deg"], a402["fov_deg"]) == (180.0, 90.0)
    assert a422["max_range"] == a402["max_range"] == 3000.0               # Phantom: понад 3 км
    assert {"sigma", "pd", "clutter"} <= set(a422["real"]["assumed"])     # точності в даташиті немає


def test_a_sourced_value_is_actually_what_the_sensor_uses():
    s = make_sensor("robin-iris", "iris1", 0, 0)
    assert s.max_range == 3100.0 and s.kind == "radar" and s.type_id == "robin-iris"
    assert math.isclose(float(s.fov), 2 * math.pi)
    e = make_sensor("echodyne-echoguard", "e1", 0, 0, heading_deg=90)
    assert math.isclose(math.degrees(e.fov), 120.0) and e.in_fov((500, 0)) and not e.in_fov((0, 500))


def test_generic_models_are_not_labelled_as_real():
    assert all("real" not in v for k, v in SENSOR_TYPES.items() if k not in REAL)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    app.state.runtime = build_runtime(config.Config(mode="demo"))
    app.state.access = DashboardAccess()   # вхід вимкнено: анонім лише viewer, тож редагувати розкладку входимо як admin
    client = TestClient(app)
    login_as(client, "admin")
    return client


def test_api_tells_dashboard_which_models_are_real_and_what_is_assumed(client):
    types = {t["id"]: t for t in client.get("/api/config").json()["sensor_types"]}
    assert types["robin-iris"]["real"]["vendor"] == "Robin Radar Systems"
    assert types["robin-iris"]["real"]["assumed"] == ["pd", "clutter"]
    assert types["echodyne-echoguard"]["fov_deg"] == 120.0
    assert types["radar-mid"]["real"] is None


@pytest.mark.parametrize("tid", sorted(REAL))
def test_every_real_model_can_be_placed_with_a_heading_and_gets_zones_and_coverage(client, tid):
    body = {"sensors": [{"id": "s1", "type": tid, "x": 100, "y": -50, "heading": 45}]}
    assert client.put("/api/layout", json=body).status_code == 200
    cfg = client.get("/api/config").json()
    assert cfg["sensors"][0]["type"] == tid and cfg["sensors"][0]["heading_deg"] == 45.0
    assert cfg["zones"][0]["radius"] == pytest.approx(SENSOR_TYPES[tid]["max_range"] * 0.4, abs=0.1)
    assert client.post("/api/coverage", json=body).status_code == 200
