"""Конвертер GPS-логу в еталон: проєкція, формати часу, пропуск зіпсованих рядків, сумісність із read_truth."""
import json
import math

import pytest

from dronewatch.service.evaluate import read_truth
from dronewatch.service.gps_to_truth import GpsError, convert, main, to_meters

ORIGIN = (45.0, 4.0)
KW = {"lat_col": "lat", "lon_col": "lon", "time_col": "time"}


def write(tmp_path, text):
    p = tmp_path / "f.csv"
    p.write_text(text, encoding="utf-8")
    return p


def test_projection_matches_dashboard_scale():
    x, y = to_meters(45.001, 4.001, ORIGIN)
    assert y == pytest.approx(111.32, abs=0.01)
    assert x == pytest.approx(111.32 * math.cos(math.radians(45.0)), abs=0.01)
    assert to_meters(*ORIGIN, ORIGIN) == (0.0, 0.0)


def test_epoch_rows_sorted_and_altitude_offset(tmp_path):
    p = write(tmp_path, "time,lat,lon,alt\n101,45.001,4.0,270\n100,45.0,4.0,260\n")
    rows, skipped = convert(p, ORIGIN, alt_col="alt", alt_offset=250.0, **KW)
    assert skipped == 0
    assert [r["t"] for r in rows] == [100.0, 101.0]
    assert [r["z"] for r in rows] == [10.0, 20.0]
    assert rows[1]["y"] == pytest.approx(111.32, abs=0.01)


def test_time_formats(tmp_path):
    p = write(tmp_path, "time,lat,lon\n2025-01-01T00:00:00Z,45.0,4.0\n")
    assert convert(p, ORIGIN, time_format="iso", **KW)[0][0]["t"] == 1735689600.0
    p = write(tmp_path, "time,lat,lon\n1735689600500,45.0,4.0\n")
    assert convert(p, ORIGIN, time_format="epoch-ms", **KW)[0][0]["t"] == 1735689600.5
    p = write(tmp_path, "time,lat,lon\n2.5,45.0,4.0\n")
    assert convert(p, ORIGIN, time_format="relative", t0=1000.0, **KW)[0][0]["t"] == 1002.5
    with pytest.raises(GpsError, match="--t0"):
        convert(p, ORIGIN, time_format="relative", **KW)


def test_bad_rows_skipped_and_counted_not_guessed(tmp_path):
    p = write(tmp_path, "time,lat,lon\n1,45.0,4.0\n2,,4.0\n3,abc,4.0\n4,0,0\n5,95,4.0\n6,nan,4.0\n")
    rows, skipped = convert(p, ORIGIN, **KW)
    assert len(rows) == 1 and skipped == 5


def test_missing_column_and_empty_file_are_errors(tmp_path):
    with pytest.raises(GpsError, match="немає колонок"):
        convert(write(tmp_path, "a,b\n1,2\n"), ORIGIN, **KW)
    with pytest.raises(GpsError, match="жодного придатного"):
        convert(write(tmp_path, "time,lat,lon\n1,0,0\n"), ORIGIN, **KW)
    with pytest.raises(GpsError):
        convert(tmp_path / "нема.csv", ORIGIN, **KW)


def test_cli_output_is_readable_by_evaluate(tmp_path, capsys):
    src = write(tmp_path, "time,lat,lon\n100,45.0,4.0\n101,45.0005,4.0\n")
    out = tmp_path / "gps.jsonl"
    assert main(["--csv", str(src), "--origin", "45.0", "4.0", "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8").splitlines()[0])["id"] == "gps1"
    assert read_truth(out).at(100.5)   # еталон на проміжний момент знайдено
    assert main(["--csv", str(tmp_path / "x.csv"), "--origin", "45", "4", "--out", str(out)]) == 2
