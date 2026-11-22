"""Математика вирізання оффлайн-карти (без мережі й без бібліотеки pmtiles)."""
import math

import pytest

from dronewatch.service.mapbuild import bbox, tile_xy, tiles_in_bbox

EIFFEL = (48.8584, 2.2945)


def test_known_tile_of_the_eiffel_tower():
    assert tile_xy(EIFFEL[1], EIFFEL[0], 0) == (0, 0)
    x, y = tile_xy(EIFFEL[1], EIFFEL[0], 15)
    # еталон: стандартна формула slippy-map, x = (lon+180)/360 * 2^z, y = (1 - ln(tan(lat) + sec(lat)) / pi) / 2 * 2^z
    assert (x, y) == (16592, 11272)


def test_bbox_is_a_square_of_the_requested_size_around_the_point():
    west, south, east, north = bbox(*EIFFEL, 12.0)
    assert (south + north) / 2 == pytest.approx(EIFFEL[0]) and (west + east) / 2 == pytest.approx(EIFFEL[1])
    height_km = (north - south) * 111.32
    width_km = (east - west) * 111.32 * math.cos(math.radians(EIFFEL[0]))
    assert height_km == pytest.approx(24.0, rel=0.01) and width_km == pytest.approx(24.0, rel=0.01)


def test_tiles_cover_every_zoom_and_grow_four_fold():
    box = bbox(*EIFFEL, 12.0)
    tiles = tiles_in_bbox(box, 0, 15)
    assert {z for z, _, _ in tiles} == set(range(16))
    assert (0, 0, 0) in tiles
    per_zoom = {z: sum(1 for t in tiles if t[0] == z) for z in range(16)}
    assert per_zoom[15] > per_zoom[14] * 3 and 800 < per_zoom[15] < 1100
    assert len(tiles) == len(set(tiles))
