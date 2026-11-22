"""Лінія видимості повз будинки: растр, перетин променя, зв'язок із покриттям, розбір тайла й файлу будівель."""
import math

import numpy as np
import pytest

from dronewatch.fusion.sim import make_sensor
from dronewatch.service.buildings_build import polygon_rings, tile_buildings
from dronewatch.service.coverage import BLIND, GOOD, coverage
from dronewatch.service.occlusion import Buildings, LineOfSight
from dronewatch.service.terrain import TILE, Terrain


def box_buildings(*boxes: tuple[float, float, float, float, float], min_height: float = 0.0) -> Buildings:
    """Прямокутні будинки (x0, y0, x1, y1, висота) у метрах."""
    ring_pts, starts, heights = [], [0], []
    for x0, y0, x1, y1, h in boxes:
        ring_pts += [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        starts.append(len(ring_pts))
        heights.append(h)
    return Buildings(np.array(ring_pts, float), np.array(starts), np.array(heights, np.float32),
                     np.full(len(heights), min_height, np.float32))


WALL = (100, -50, 120, 50, 40)   # 20 м завширшки, 100 м у довжину, 40 м заввишки на схід від початку координат


def vis(b: Buildings, sensor, x, y, alt=30.0) -> bool:
    return bool(LineOfSight(b).visible(np.array(sensor, float), np.array([float(x)]), np.array([float(y)]), alt)[0])


def test_a_tall_building_between_sensor_and_target_blocks_the_view():
    b = box_buildings(WALL)
    assert not vis(b, (0, 0, 0), 200, 0)      # стіна на промені
    assert vis(b, (0, 0, 0), 0, 200)          # у протилежний бік нічого немає
    assert vis(b, (0, 0, 0), 200, 120)        # промінь обходить стіну збоку


def test_sensor_above_the_roof_line_sees_over_the_building():
    b = box_buildings(WALL)
    assert vis(b, (0, 0, 100), 200, 0)        # на висоті 100 м промінь над дахом 40 м
    assert not vis(b, (0, 0, 10), 200, 0)


def test_target_below_the_roof_line_is_hidden_even_when_close_behind():
    b = box_buildings(WALL)
    assert not vis(b, (0, 0, 60), 130, 0, alt=5.0)    # за стіною низько над землею: промінь ще над дахом, але падає за нею
    assert vis(b, (0, 0, 60), 130, 0, alt=60.0)


def test_sensor_on_a_roof_is_not_blocked_by_its_own_building():
    b = box_buildings((-10, -10, 10, 10, 25), WALL)
    assert b.containing(0, 0) == [1]
    assert vis(b, (0, 0, 0), -200, 0)         # свій будинок не затуляє
    assert not vis(b, (0, 0, 0), 200, 0)      # чужа стіна затуляє


def test_building_that_floats_above_the_ray_does_not_block():
    b = box_buildings((100, -50, 120, 50, 80), min_height=60.0)   # естакада 60-80 м
    assert vis(b, (0, 0, 0), 200, 0, alt=30.0)   # промінь на висотах 0-30 м проходить під нею


def test_tiny_building_smaller_than_a_pixel_still_counts():
    b = box_buildings((100, -1, 101, 1, 60))     # 1 x 2 м
    assert not vis(b, (0, 0, 0), 200, 0)


def test_too_large_a_window_fails_closed():
    b = box_buildings(WALL)
    assert not vis(b, (0, 0, 0), 30_000, 0)      # ділянка завелика для растра: покриття не обіцяємо


def test_coverage_marks_cells_behind_a_wall_as_blind_and_reports_los():
    radar = make_sensor("radar-mid", "r1", 0, 0)
    free = coverage([radar])
    walled = coverage([radar], None, LineOfSight(box_buildings((100, -200, 120, 200, 200))))   # стіна 200 м завтовшки по висоті, довга
    assert free["los"] is False and walled["los"] is True
    assert free["stats"]["good"] == 100.0 and walled["stats"]["blind"] > 10
    s = walled["cell"]

    def at(x, y):
        return next(c for a, b, c in walled["cells"] if (a, b) == (int(x // s), int(y // s)))
    assert at(300, 0) == BLIND and at(-300, 0) == GOOD


def test_a_radar_on_a_mast_covers_what_a_ground_radar_loses():
    wall = LineOfSight(box_buildings((100, -200, 120, 200, 40)))
    low = coverage([make_sensor("radar-mid", "r1", 0, 0)], None, wall)
    high = coverage([make_sensor("radar-mid", "r1", 0, 0, 0.0, 120.0)], None, wall)
    assert high["stats"]["good"] > low["stats"]["good"]


def test_acoustic_sensor_is_not_occluded_by_buildings():
    """Звук огинає будинки, тож пеленговий акустичний датчик за стіною лишається зі слабким покриттям."""
    wall = LineOfSight(box_buildings((100, -400, 120, 400, 200)))
    r = coverage([make_sensor("acoustic", "a1", 0, 0), make_sensor("acoustic", "a2", 0, 50)], None, wall)
    assert r["stats"]["weak"] > 0 and r["stats"]["blind"] == 0 and r["los"] is True


# ---------- файл будівель і розбір тайла ----------
def test_load_converts_lonlat_to_local_metres_and_rejects_a_corrupt_file(tmp_path):
    lat0, lon0 = 48.8584, 2.2945
    dlon = 100 / (111320 * math.cos(math.radians(lat0)))
    ring = np.array([[lon0 + dlon, lat0], [lon0 + 2 * dlon, lat0], [lon0 + 2 * dlon, lat0 + 1e-3], [lon0 + dlon, lat0 + 1e-3]])
    ok = tmp_path / "b.npz"
    np.savez(ok, lonlat=ring, starts=np.array([0, 4]), height=np.array([30], np.float32), min_height=np.zeros(1, np.float32))
    b = Buildings.load(ok, (lat0, lon0))
    assert b.count == 1 and b.x0[0] == pytest.approx(100, abs=0.01) and b.x1[0] == pytest.approx(200, abs=0.01)
    bad = tmp_path / "bad.npz"
    np.savez(bad, lonlat=ring, starts=np.array([0, 9]), height=np.array([30], np.float32), min_height=np.zeros(1, np.float32))
    with pytest.raises(ValueError, match="пошкоджений"):
        Buildings.load(bad, (lat0, lon0))


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(num: int, wire: int, payload: bytes | int) -> bytes:
    key = _varint(num << 3 | wire)
    if wire == 0:
        assert isinstance(payload, int)
        return key + _varint(payload)
    assert isinstance(payload, bytes)
    return key + payload if wire == 1 else key + _varint(len(payload)) + payload


def _zigzag(n: int) -> int:
    return (n << 1) ^ (n >> 31)


def make_tile(rings: list[list[tuple[int, int]]], height: float | None) -> bytes:
    geom: list[int] = []
    x = y = 0
    for ring in rings:
        geom += [1 | 1 << 3, _zigzag(ring[0][0] - x), _zigzag(ring[0][1] - y)]
        x, y = ring[0]
        geom += [2 | (len(ring) - 1) << 3]
        for px, py in ring[1:]:
            geom += [_zigzag(px - x), _zigzag(py - y)]
            x, y = px, py
        geom += [7 | 1 << 3]
    feat = _field(3, 0, 3) + _field(4, 2, b"".join(_varint(g) for g in geom))
    layer = _field(1, 2, b"buildings") + _field(5, 0, 4096)
    if height is not None:
        feat = _field(2, 2, _varint(0) + _varint(0)) + feat
        layer += _field(3, 2, b"height") + _field(4, 2, _field(3, 1, np.float64(height).tobytes()))
    return _field(3, 2, layer + _field(2, 2, feat))


def test_tile_decoder_reads_height_defaults_missing_height_and_skips_courtyards():
    outer = [(1000, 1000), (3000, 1000), (3000, 3000), (1000, 3000)]       # додатна площа (y вниз): зовнішній контур
    court = [(1500, 1500), (1500, 2500), (2500, 2500), (2500, 1500)]       # протилежний обхід: внутрішній двір
    got = tile_buildings(make_tile([outer, court], 24.0), 15, 16384, 10, 9.0)
    assert len(got) == 1
    ring, h, mh, known = got[0]
    assert (h, mh, known) == (24.0, 0.0, True) and ring.shape == (4, 2)
    assert -180 <= ring[:, 0].min() < ring[:, 0].max() <= 180 and 0 < ring[:, 1].min() < 90
    (_, h2, _, known2), = tile_buildings(make_tile([outer], None), 15, 16384, 10, 9.0)
    assert (h2, known2) == (9.0, False)    # без висоти: припущення, і воно помічене як припущення


def test_polygon_rings_splits_on_moveto_and_closepath():
    cmds = [9, 0, 0, 18, 20, 0, 0, 20, 15, 9, 10, 10, 18, 4, 0, 0, 4, 15]   # два трикутники: MoveTo, 2 x LineTo, ClosePath
    rings = polygon_rings(cmds)
    assert [len(r) for r in rings] == [3, 3]


# ---------- рельєф ----------
ORIGIN = (48.8584, 2.2945)


def make_terrain(height_at, zoom: int = 14, tiles: int = 2) -> Terrain:
    """Рельєф із функції висоти height_at(схід, північ) у метрах: заповнюємо мозаїку з центром на ORIGIN."""
    import math as m
    n = 2 ** zoom
    tx0 = int((ORIGIN[1] + 180) / 360 * n) - tiles // 2
    ty0 = int((1 - m.asinh(m.tan(m.radians(ORIGIN[0]))) / m.pi) / 2 * n) - tiles // 2
    side = tiles * TILE
    t = Terrain(np.zeros((side, side), np.float32), zoom, (tx0, ty0), ORIGIN)
    cols, rows = np.meshgrid(np.arange(side) + 0.5, np.arange(side) + 0.5)
    lon = (cols + tx0 * TILE) / (n * TILE) * 360 - 180
    lat = np.degrees(np.arctan(np.sinh(m.pi * (1 - 2 * (rows + ty0 * TILE) / (n * TILE)))))
    x = (lon - ORIGIN[1]) * 111320 * m.cos(m.radians(ORIGIN[0]))
    y = (lat - ORIGIN[0]) * 111320
    t.elev = height_at(x, y).astype(np.float32)
    return t


def test_terrain_elevation_matches_the_surface_and_is_nan_outside_the_file():
    t = make_terrain(lambda x, y: 100 + 0.05 * x)      # схил 5%
    e = t.elevation(np.array([0.0, 200.0, -150.0]), np.array([0.0, 50.0, -80.0]))
    assert e == pytest.approx([100, 110, 92.5], abs=0.5)
    assert np.isnan(t.elevation(np.array([20_000.0]), np.array([0.0]))[0])


def hill(x, y):
    return 100 + 60 * np.exp(-(((x - 150) / 40) ** 2 + (y / 200) ** 2))   # пагорб 60 м на сході, за 150 м


def test_a_hill_between_sensor_and_target_blocks_the_view_and_a_mast_clears_it():
    los = LineOfSight(terrain=make_terrain(hill))
    ray = lambda sz, x, y, alt=10.0: bool(los.visible(np.array([0.0, 0.0, sz]), np.array([x]), np.array([y]), alt)[0])  # noqa: E731
    assert not ray(0, 300, 0)          # за пагорбом, датчик на землі
    assert ray(0, -300, 0)             # у протилежний бік рівнина
    assert ray(0, 300, 900)            # промінь проходить далеко збоку від пагорба
    assert ray(120, 300, 0, 40.0)      # щогла 120 м і ціль 40 м над землею: промінь над вершиною
    assert not ray(10, 300, 0, 10.0)


def test_sensor_on_the_hilltop_sees_down_the_slopes():
    los = LineOfSight(terrain=make_terrain(hill))
    assert bool(los.visible(np.array([150.0, 0.0, 3.0]), np.array([-200.0, 400.0]), np.array([0.0, 0.0]), 10.0).all())


def test_outside_the_terrain_file_nothing_is_promised():
    los = LineOfSight(terrain=make_terrain(lambda x, y: 100 + 0 * x))
    assert not los.visible(np.array([0.0, 0.0, 5.0]), np.array([20_000.0]), np.array([0.0]), 30.0)[0]      # ціль поза файлом
    assert not los.visible(np.array([30_000.0, 0.0, 5.0]), np.array([0.0]), np.array([0.0]), 30.0).any()   # датчик поза файлом


def test_buildings_stand_on_the_terrain_not_at_sea_level():
    """Та сама стіна 40 м і те саме висотне положення датчика й цілі над землею, але земля піднімається до стіни й за нею:
    дах і ціль піднялися разом із землею, а датчик лишився внизу, тож промінь, що на рівнині йшов над дахом, тепер упирається в нього."""
    wall = box_buildings(WALL)

    def ray(los):
        return bool(los.visible(np.array([0.0, 0.0, 60.0]), np.array([200.0]), np.array([0.0]), 30.0)[0])
    assert ray(LineOfSight(wall, make_terrain(lambda x, y: 100 + 0 * x)))                        # рівнина: промінь над дахом
    assert not ray(LineOfSight(wall, make_terrain(lambda x, y: 100 + 0.2 * np.clip(x, 0, 150))))   # схил: уперся в дах
    assert ray(LineOfSight(None, make_terrain(lambda x, y: 100 + 0.2 * np.clip(x, 0, 150))))      # той самий схил без будинків: видно


def test_terrain_file_validation(tmp_path):
    t = make_terrain(lambda x, y: 100 + 0 * x)
    good = tmp_path / "d.npz"
    np.savez(good, elev=t.elev, zoom=np.array([t.zoom]), tile0=np.array(t.tile0))
    assert Terrain.load(good, ORIGIN).elev.shape == t.elev.shape
    bad = tmp_path / "bad.npz"
    np.savez(bad, elev=np.full((300, 300), 100.0, np.float32), zoom=np.array([14]), tile0=np.array([0, 0]))   # не кратно 256
    with pytest.raises(ValueError, match="пошкоджений"):
        Terrain.load(bad, ORIGIN)
    wild = tmp_path / "wild.npz"
    np.savez(wild, elev=np.full(t.elev.shape, 99999.0, np.float32), zoom=np.array([14]), tile0=np.array(t.tile0))
    with pytest.raises(ValueError, match="діапазон"):
        Terrain.load(wild, ORIGIN)


def test_terrarium_decoding():
    from dronewatch.service.dem_build import decode_terrarium
    px = np.array([[[128, 0, 0], [128, 100, 128], [127, 255, 0]]], np.uint8)   # 0 м, 100,5 м, -1 м
    assert decode_terrarium(px)[0] == pytest.approx([0.0, 100.5, -1.0])
