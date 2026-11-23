#!/usr/bin/env python3
"""Витягує контури будівель із власного оффлайн-файлу PMTiles у компактний .npz для розрахунку лінії видимості датчиків.

    pip install -e ".[map]"
    dronewatch-buildings --pmtiles data/map/paris.pmtiles --out data/map/buildings.npz

Джерело те саме, що й карта (dronewatch-mapbuild), нових завантажень немає. Робиться один раз, сервісу під час роботи потрібен лише
результат (numpy). Беремо найдетальніший масштаб, де є шар buildings. Для кожного будинку лишається зовнішній контур і висота:
внутрішні двори ігноруються (будинок вважається суцільним, тож покриття не завищується).
Висота береться з тега height (метри). Будинок без висоти отримує --default-height: це припущення, а не дані, тому його видно в підсумку.
"""
import argparse
import gzip
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

DEFAULT_HEIGHT_M = 10.0   # приблизно 3 поверхи: OSM часто не знає висоти, а нуль означав би «будинок не заважає»


# ---------- мінімальний декодер Mapbox Vector Tile (protobuf), щоб не тягнути зайву залежність ----------
def _varint(b: bytes, i: int) -> tuple[int, int]:
    shift = out = 0
    while True:
        c = b[i]
        i += 1
        out |= (c & 0x7F) << shift
        if not c & 0x80:
            return out, i
        shift += 7


def _fields(b: bytes) -> list[tuple[int, Any]]:
    """Список (номер поля, значення): varint як int, довгі поля як bytes, 32/64-бітні як сирі bytes."""
    out: list[tuple[int, Any]] = []
    i = 0
    while i < len(b):
        key, i = _varint(b, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            v, i = _varint(b, i)
            out.append((num, v))
        elif wire == 2:
            n, i = _varint(b, i)
            out.append((num, b[i:i + n]))
            i += n
        elif wire == 1:
            out.append((num, b[i:i + 8]))
            i += 8
        elif wire == 5:
            out.append((num, b[i:i + 4]))
            i += 4
        else:
            raise ValueError(f"невідомий тип поля protobuf: {wire}")
    return out


def _packed(b: bytes) -> list[int]:
    out, i = [], 0
    while i < len(b):
        v, i = _varint(b, i)
        out.append(v)
    return out


def _value(raw: bytes) -> Any:
    for num, v in _fields(raw):
        if num == 1:
            return v.decode("utf-8", "replace")
        if num == 2:
            return float(np.frombuffer(v, "<f4")[0])
        if num == 3:
            return float(np.frombuffer(v, "<f8")[0])
        if num in (4, 5):
            return int(v)
        if num == 6:
            return (v >> 1) ^ -(v & 1)
        if num == 7:
            return bool(v)
    return None


def polygon_rings(commands: list[int]) -> list[list[tuple[int, int]]]:
    """Геометрія полігона тайла в кільця точок (в одиницях тайла, вісь y вниз)."""
    rings: list[list[tuple[int, int]]] = []
    cur: list[tuple[int, int]] = []
    x = y = i = 0
    while i < len(commands):
        cmd, count = commands[i] & 7, commands[i] >> 3
        i += 1
        if cmd == 7:   # ClosePath
            if cur:
                rings.append(cur)
                cur = []
            continue
        for _ in range(count):
            dx, dy = commands[i], commands[i + 1]
            i += 2
            x += (dx >> 1) ^ -(dx & 1)
            y += (dy >> 1) ^ -(dy & 1)
            if cmd == 1 and cur:   # MoveTo починає нове кільце
                rings.append(cur)
                cur = []
            cur.append((x, y))
    if cur:
        rings.append(cur)
    return rings


def _signed_area(ring: list[tuple[int, int]]) -> float:
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:] + ring[:1], strict=True)) / 2


def tile_buildings(tile: bytes, z: int, tx: int, ty: int, default_height: float) -> list[tuple[np.ndarray, float, float, bool]]:
    """Будинки одного розпакованого тайла: (контур lon/lat, висота, нижня межа, чи висота справжня)."""
    out: list[tuple[np.ndarray, float, float, bool]] = []
    n = 2.0 ** z
    for num, layer in _fields(tile):
        if num != 3:
            continue
        name, keys, values, feats, extent = "", [], [], [], 4096
        for ln, lv in _fields(layer):
            if ln == 1:
                name = lv.decode()
            elif ln == 2:
                feats.append(lv)
            elif ln == 3:
                keys.append(lv.decode())
            elif ln == 4:
                values.append(_value(lv))
            elif ln == 5:
                extent = lv
        if name != "buildings":
            continue
        for f in feats:
            gtype, geom, tags = 0, [], []
            for fn, fv in _fields(f):
                if fn == 3:
                    gtype = fv
                elif fn == 4:
                    geom = _packed(fv)
                elif fn == 2:
                    tags = _packed(fv)
            if gtype != 3:
                continue
            props = {keys[k]: values[v] for k, v in zip(tags[0::2], tags[1::2], strict=False)}
            h = props.get("height")
            known = isinstance(h, int | float) and not isinstance(h, bool) and h > 0
            height = float(h) if isinstance(h, int | float) and known else default_height
            mh = props.get("min_height")
            min_h = float(mh) if isinstance(mh, int | float) and not isinstance(mh, bool) and mh > 0 else 0.0
            if min_h >= height:
                continue   # нависаюча частина без «тіла» до землі: для лінії видимості дрібниця, а ризик помилки більший
            for ring in polygon_rings(geom):
                if len(ring) < 3 or _signed_area(ring) <= 0:   # від'ємна площа = внутрішній двір
                    continue
                pts = np.array(ring, float) / extent
                lon = (tx + pts[:, 0]) / n * 360.0 - 180.0
                lat = np.degrees(np.arctan(np.sinh(math.pi * (1 - 2 * (ty + pts[:, 1]) / n))))
                out.append((np.column_stack([lon, lat]), height, min_h, bool(known)))
    return out


def extract(pmtiles: Path, default_height: float = DEFAULT_HEIGHT_M, progress: Any = print) -> dict[str, np.ndarray]:
    try:
        from pmtiles.reader import MmapSource, deserialize_header
        from pmtiles.tile import Compression
    except ImportError:
        raise SystemExit('потрібна бібліотека pmtiles: pip install -e ".[map]"') from None
    with pmtiles.open("rb") as f:
        get = MmapSource(f)
        header = deserialize_header(get(0, 127))
        zmax = header["max_zoom"]
        tiles = [(z, x, y, get(header["tile_data_offset"] + e.offset, e.length)) for (z, x, y), e in _entries(get, header)
                 if z == zmax]
        gz = header["tile_compression"] == Compression.GZIP
        progress(f"тайлів на масштабі {zmax}: {len(tiles)}")
        rings: list[np.ndarray] = []
        heights: list[float] = []
        mins: list[float] = []
        known = 0
        for z, x, y, raw in tiles:
            for ring, h, mh, k in tile_buildings(gzip.decompress(raw) if gz else raw, z, x, y, default_height):
                rings.append(ring)
                heights.append(h)
                mins.append(mh)
                known += k
    if not rings:
        raise SystemExit("у файлі немає будівель (шар buildings порожній)")
    starts = np.concatenate([[0], np.cumsum([len(r) for r in rings])]).astype(np.int64)
    progress(f"будинків: {len(rings)}, з відомою висотою: {known} ({known / len(rings):.0%}); решті припущено {default_height} м")
    return {"lonlat": np.vstack(rings).astype(np.float64), "starts": starts, "height": np.array(heights, np.float32),
            "min_height": np.array(mins, np.float32), "assumed": np.array([default_height], np.float32)}


def _entries(get: Any, header: Any) -> Any:
    """Усі тайли файлу як ((z, x, y), запис каталогу): із запису потрібні лише зміщення й довжина."""
    from pmtiles.reader import deserialize_directory, tileid_to_zxy

    def walk(offset: int, length: int) -> Any:
        for e in deserialize_directory(get(offset, length)):
            if e.run_length == 0:
                yield from walk(header["leaf_directory_offset"] + e.offset, e.length)
            else:
                for k in range(e.run_length):
                    yield tileid_to_zxy(e.tile_id + k), e

    yield from walk(header["root_offset"], header["root_length"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Контури будівель з оффлайн-карти PMTiles для розрахунку лінії видимості")
    ap.add_argument("--pmtiles", type=Path, default=Path("data/map/paris.pmtiles"))
    ap.add_argument("--out", type=Path, default=Path("data/map/buildings.npz"))
    ap.add_argument("--default-height", type=float, default=DEFAULT_HEIGHT_M, help="висота (м) для будинків, де її немає в даних")
    a = ap.parse_args(argv)
    if not a.pmtiles.is_file():
        print(f"немає файлу {a.pmtiles}: спершу dronewatch-mapbuild", file=sys.stderr)
        return 2
    data = extract(a.pmtiles, a.default_height)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(a.out, **data)  # type: ignore[arg-type]
    print(f"записано {a.out} ({a.out.stat().st_size // 1024} КБ)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
