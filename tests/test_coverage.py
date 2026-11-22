"""Підсвітка покриття буферної зони: що система зможе зробити з ціллю в кожній точці (радар, тріангуляція, ідентифікація)."""
import pytest
from fastapi.testclient import TestClient

from dronewatch.fusion.sim import make_sensor
from dronewatch.service import config
from dronewatch.service.app import app, build_runtime
from dronewatch.service.coverage import BLIND, GOOD, MAX_CELLS, STRONG, WEAK, coverage
from dronewatch.service.dashboard_web import DashboardAccess
from helpers_auth import login_as


def cls_at(result, x, y):
    """Клас клітинки, що містить точку (x, y) метрів."""
    s = result["cell"]
    ix, iy = int(x // s), int(y // s)
    return next(c for a, b, c in result["cells"] if (a, b) == (ix, iy))


def test_lone_radar_gives_good_coverage_everywhere_in_its_zone_but_never_strong():
    r = coverage([make_sensor("radar-mid", "r1", 0, 0)])
    assert r["stats"]["good"] == 100.0 and r["stats"]["strong"] == 0.0 and r["stats"]["ident"] == 0.0
    assert {c for *_, c in r["cells"]} == {GOOD}


def test_zone_is_a_disc_of_the_buffer_radius_around_the_radar():
    r = coverage([make_sensor("radar-mid", "r1", 0, 0)])   # буферна зона 600 м
    s = r["cell"]
    far = max(((a + 0.5) * s) ** 2 + ((b + 0.5) * s) ** 2 for a, b, _ in r["cells"]) ** 0.5
    assert 550 < far <= 600
    assert all(abs((a + 0.5) * s) <= 600 for a, _, _ in r["cells"])


def test_rf_plus_acoustic_at_a_good_angle_complete_a_radar_into_strong_coverage():
    sensors = [make_sensor("radar-mid", "r1", 0, 0), make_sensor("acoustic", "ac1", -80, 50), make_sensor("rf", "rf1", 350, -250)]
    r = coverage(sensors)
    assert r["stats"]["ident"] == 100.0                    # RF на 1800 м накриває всю зону
    assert cls_at(r, 100, 300) == STRONG                    # напрямки на акустичний і RF тут розходяться широко
    assert r["stats"]["strong"] > 50


def test_acoustic_that_is_too_far_leaves_part_of_the_zone_only_good():
    near = coverage([make_sensor("radar-mid", "r1", 0, 0), make_sensor("acoustic", "ac1", -80, 50), make_sensor("rf", "rf1", 350, -250)])
    far = coverage([make_sensor("radar-mid", "r1", 0, 0), make_sensor("acoustic", "ac1", -450, 0), make_sensor("rf", "rf1", 350, -250)])
    assert near["stats"]["strong"] > far["stats"]["strong"]   # акустика на 450 м не чує східний край зони (450+600 > 700)
    assert cls_at(far, 500, 0) == GOOD


def test_bearings_that_line_up_with_the_target_do_not_triangulate():
    """Два датчики з одного боку на одній прямій із ціллю: пеленги майже збігаються, надійної точки перетину немає."""
    sensors = [make_sensor("radar-mid", "r1", 0, 0), make_sensor("acoustic", "ac1", -100, 0), make_sensor("rf", "rf1", -300, 0)]
    r = coverage(sensors)
    assert cls_at(r, 400, 5) == GOOD       # ціль на лінії датчиків: кут між пеленгами майже нуль
    assert cls_at(r, 0, 350) == STRONG     # збоку кут широкий


def test_second_radar_is_a_backup_channel_so_radar_plus_rf_is_enough():
    r = coverage([make_sensor("radar-mid", "r1", 0, 0), make_sensor("radar-short", "r2", 100, 0), make_sensor("rf", "rf1", 200, 0)])
    assert cls_at(r, 0, 100) == STRONG


def test_without_a_radar_nothing_is_better_than_weak():
    r = coverage([make_sensor("rf", "rf1", 300, 100), make_sensor("acoustic", "ac1", 100, 300)])
    classes = {c for *_, c in r["cells"]}
    assert classes <= {BLIND, WEAK} and WEAK in classes
    assert r["stats"]["good"] == r["stats"]["strong"] == 0.0


def test_stats_add_up_and_cell_count_stays_bounded_for_a_huge_zone():
    r = coverage([make_sensor("radar-long", "r1", 0, 0), make_sensor("radar-long", "r2", 4000, 4000)])   # зони по 1200 м, далеко одна від одної
    assert len(r["cells"]) <= MAX_CELLS * 1.2
    assert sum(r["stats"][k] for k in ("blind", "weak", "good", "strong")) == pytest.approx(100.0, abs=0.5)
    assert r["stats"]["total"] == len(r["cells"])


# ---------- API ----------
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    app.state.runtime = build_runtime(config.Config(mode="demo", map=config.MapCfg(buildings=str(tmp_path / "none.npz"), dem=str(tmp_path / "none2.npz"))))   # без будинків і рельєфу: тест не залежить від локальних файлів у data/map
    app.state.access = DashboardAccess()   # вхід вимкнено: анонім лише viewer, тож редагувати розкладку входимо як admin
    client = TestClient(app)
    login_as(client, "admin")
    return client


def test_coverage_endpoint_computes_without_changing_the_live_layout(client):
    before = client.get("/api/config").json()["sensors"]
    body = {"sensors": [{"id": "r1", "type": "radar-short", "x": 0, "y": 0}, {"id": "rf1", "type": "rf", "x": 100, "y": 0},
                        {"id": "ac1", "type": "acoustic", "x": 0, "y": 100}]}
    r = client.post("/api/coverage", json=body)
    assert r.status_code == 200 and r.json()["stats"]["ident"] == 100.0 and r.json()["cells"]
    assert client.get("/api/config").json()["sensors"] == before


@pytest.mark.parametrize("body", [{"sensors": []}, {"sensors": [{"id": "bad id", "type": "rf", "x": 0, "y": 0}]},
                                  {"sensors": [{"id": "r1", "type": "tank", "x": 0, "y": 0}]}])
def test_coverage_endpoint_rejects_the_same_bad_layouts_as_the_editor(client, body):
    assert client.post("/api/coverage", json=body).status_code == 422


def test_get_coverage_describes_the_current_sensors_and_matches_the_editor_preview(client):
    body = {"sensors": [{"id": "radar1", "type": "radar-mid", "x": 0, "y": 0}, {"id": "rf1", "type": "rf", "x": 400, "y": -300},
                        {"id": "ac1", "type": "acoustic", "x": -300, "y": 200}]}
    client.delete("/api/layout")   # вбудована розстановка демо: ті самі позиції
    current = client.get("/api/coverage").json()
    preview = client.post("/api/coverage", json=body).json()
    assert current["stats"] == preview["stats"] and current["cells"]


def test_live_sensors_from_a_config_get_coverage_over_the_configured_zones():
    """У бойовому режимі датчики з конфігу (тип radar/rf/acoustic, не з палітри), а зони теж з конфігу."""
    from dronewatch.service.config import SensorCfg, build_sensors
    from dronewatch.service.engine import Zone
    cfg = config.Config.model_construct(sensors=[
        SensorCfg(id="radar1", type="radar", position=[0, 0], max_range=1500),
        SensorCfg(id="rf1", type="rf", position=[400, -300], max_range=1800)])
    r = coverage(build_sensors(cfg), [Zone("Буферна", 600, "WARNING"), Zone("Критична", 250, "CRITICAL")])
    assert r["stats"]["good"] + r["stats"]["strong"] == 100.0 and r["stats"]["ident"] == 100.0
    assert max(abs(a) for a, _, _ in r["cells"]) * r["cell"] <= 600 + r["cell"]   # лише буферна зона, критична не береться


def test_coverage_endpoint_says_it_ignores_buildings_without_a_buildings_file(client):
    r = client.post("/api/coverage", json={"sensors": [{"id": "r1", "type": "radar-short", "x": 0, "y": 0}]}).json()
    assert r["los"] is False and r["buildings"] is False and r["terrain"] is False and r["target_alt_m"] == 30.0


def test_coverage_endpoint_uses_buildings_and_sensor_height_when_the_file_exists(tmp_path, monkeypatch):
    import numpy as np
    lat0, lon0 = 48.8584, 2.2945
    dlat, dlon = 1 / 111320, 1 / (111320 * np.cos(np.radians(lat0)))
    wall = np.array([[lon0 + 100 * dlon, lat0 - 200 * dlat], [lon0 + 120 * dlon, lat0 - 200 * dlat],
                     [lon0 + 120 * dlon, lat0 + 200 * dlat], [lon0 + 100 * dlon, lat0 + 200 * dlat]])
    np.savez(tmp_path / "b.npz", lonlat=wall, starts=np.array([0, 4]), height=np.array([40], np.float32), min_height=np.zeros(1, np.float32))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    app.state.runtime = build_runtime(config.Config(mode="demo", map=config.MapCfg(buildings=str(tmp_path / "b.npz"), dem=str(tmp_path / "none.npz"))))
    app.state.access = DashboardAccess()
    c = TestClient(app)
    login_as(c, "admin")

    def good(height):
        body = {"sensors": [{"id": "r1", "type": "radar-mid", "x": 0, "y": 0, "height": height}]}
        r = c.post("/api/coverage", json=body).json()
        assert r["los"] is True
        return r["stats"]["good"]
    assert good(0) < 100.0 and good(150) > good(0)
