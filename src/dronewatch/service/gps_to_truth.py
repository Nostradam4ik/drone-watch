#!/usr/bin/env python3
"""Конвертер GPS-логу (CSV) в еталон для `dronewatch-eval`: {"t": ts, "id": ..., "x": м, "y": м, "z": м}.

Координати переводяться в метри від точки origin (схід, північ) тією ж проєкцією, що дашборд, будинки й рельєф
(111320 м на градус, довгота множиться на cos(lat0)); для дальності до кількох км похибка мізерна.

    dronewatch-gps2truth --csv flight.csv --config deploy/config.yaml --out gps.jsonl
    dronewatch-gps2truth --csv flight.csv --origin 45.764 4.835 --lat-col latitude --lon-col longitude \\
        --time-col time --time-format iso --alt-col altitude_m --alt-offset -170 --out gps.jsonl

Час: `epoch` (unix-секунди, за замовчуванням), `epoch-ms`, `iso` (ISO 8601; без пояса вважається UTC) або `relative`
(секунди від початку польоту; тоді обовʼязково --t0 з unix-часом старту, того самого годинника, що й у датчиків).
Висота z: колонка --alt-col мінус --alt-offset (висота землі в точці датчиків над рівнем моря). Без --alt-col z=0.
Рядок із порожніми чи нескінченними полями пропускається й рахується; нічого не вгадується мовчки."""
import argparse
import csv
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dronewatch.service import config

M_PER_DEG = 111320.0   # як у dashboard map.js, occlusion.py, terrain.py


class GpsError(Exception):
    """Помилка вхідних даних: повідомлення вказує файл і рядок."""


def to_meters(lat: float, lon: float, origin: tuple[float, float]) -> tuple[float, float]:
    """(схід, північ) у метрах від origin=(lat0, lon0)."""
    lat0, lon0 = origin
    return (lon - lon0) * M_PER_DEG * math.cos(math.radians(lat0)), (lat - lat0) * M_PER_DEG


def _time(raw: str, fmt: str, t0: float | None) -> float:
    if fmt == "iso":
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()
    v = float(raw)
    if fmt == "epoch-ms":
        return v / 1000.0
    if fmt == "relative":
        assert t0 is not None   # перевірено в convert()
        return t0 + v
    return v


def convert(csv_path: Path, origin: tuple[float, float], *, lat_col: str, lon_col: str, time_col: str,
            time_format: str = "epoch", t0: float | None = None, alt_col: str | None = None, alt_offset: float = 0.0,
            target_id: str = "gps1", delimiter: str = ",") -> tuple[list[dict[str, Any]], int]:
    """(рядки еталону, скільки рядків CSV пропущено)."""
    if time_format == "relative" and t0 is None:
        raise GpsError("для --time-format relative потрібен --t0 (unix-час старту польоту)")
    rows: list[dict[str, Any]] = []
    skipped = 0
    try:
        with csv_path.open(encoding="utf-8-sig", newline="") as f:
            rd = csv.DictReader(f, delimiter=delimiter)
            need = [lat_col, lon_col, time_col] + ([alt_col] if alt_col else [])
            missing = [c for c in need if c not in (rd.fieldnames or [])]
            if missing:
                raise GpsError(f"{csv_path}: немає колонок {missing}; є: {rd.fieldnames}")
            for r in rd:
                try:
                    lat, lon = float(r[lat_col]), float(r[lon_col])
                    t = _time(r[time_col], time_format, t0)
                    z = float(r[alt_col]) - alt_offset if alt_col else 0.0
                except (ValueError, TypeError, KeyError):
                    skipped += 1
                    continue
                if not all(math.isfinite(v) for v in (lat, lon, t, z)) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    skipped += 1
                    continue
                if lat == 0.0 and lon == 0.0:   # типове «ще нема фіксації GPS»
                    skipped += 1
                    continue
                x, y = to_meters(lat, lon, origin)
                rows.append({"t": round(t, 3), "id": target_id, "x": round(x, 2), "y": round(y, 2), "z": round(z, 2)})
    except OSError as e:
        raise GpsError(f"{csv_path}: {e.strerror or e}") from None
    if not rows:
        raise GpsError(f"{csv_path}: жодного придатного рядка (пропущено {skipped})")
    rows.sort(key=lambda r: r["t"])
    return rows, skipped


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):   # Windows: канал за замовчуванням не вміє українську
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="dronewatch-gps2truth", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True, help="GPS-лог у CSV із заголовком")
    ap.add_argument("--out", required=True, help="куди писати еталон (JSONL)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--config", help="YAML-конфіг сервісу: origin береться звідти")
    src.add_argument("--origin", nargs=2, type=float, metavar=("LAT", "LON"), help="точка (0, 0) системи датчиків")
    ap.add_argument("--lat-col", default="lat")
    ap.add_argument("--lon-col", default="lon")
    ap.add_argument("--time-col", default="time")
    ap.add_argument("--time-format", choices=["epoch", "epoch-ms", "iso", "relative"], default="epoch")
    ap.add_argument("--t0", type=float, help="unix-час старту (лише для relative)")
    ap.add_argument("--alt-col", help="колонка висоти, м (без неї z=0)")
    ap.add_argument("--alt-offset", type=float, default=0.0, help="висота землі в точці датчиків, м (віднімається від --alt-col)")
    ap.add_argument("--id", default="gps1", help="імʼя цілі в еталоні")
    ap.add_argument("--delimiter", default=",")
    a = ap.parse_args(argv)
    try:
        if a.config:
            cfg = config.load(a.config)
            origin = (cfg.origin.lat, cfg.origin.lon)
        else:
            origin = (a.origin[0], a.origin[1])
        rows, skipped = convert(Path(a.csv), origin, lat_col=a.lat_col, lon_col=a.lon_col, time_col=a.time_col,
                                time_format=a.time_format, t0=a.t0, alt_col=a.alt_col, alt_offset=a.alt_offset,
                                target_id=a.id, delimiter=a.delimiter)
    except GpsError as e:
        print(f"помилка: {e}", file=sys.stderr)
        return 2
    with Path(a.out).open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    span = rows[-1]["t"] - rows[0]["t"]
    print(f"Записано {len(rows)} точок за {span:.0f} с (пропущено {skipped}) у {a.out}; origin {origin[0]:.6f}, {origin[1]:.6f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
