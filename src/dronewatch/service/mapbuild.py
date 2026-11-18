#!/usr/bin/env python3
"""Вирізає оффлайн-карту навколо точки з публічної збірки Protomaps (дані OpenStreetMap) в один файл PMTiles.

    pip install -e ".[map]"
    dronewatch-mapbuild --lat 48.8584 --lon 2.2945 --radius-km 12 --out data/map/paris.pmtiles

Навіщо: тайли з чужого сервера (OpenFreeMap, Mapbox тощо) видають йому, яку ділянку розглядає оператор охоронної
системи, і карта зникає без інтернету. З файлом дашборд віддає карту сам, без жодних зовнішніх запитів.
Інструмент потрібен лише один раз і лише там, де є інтернет; сервісу під час роботи він не потрібен.

Дані: © OpenStreetMap contributors (ODbL), збірка Protomaps. Атрибуція лишається в метаданих файлу й на карті.
Розмір: радіус 12 км до 15-го масштабу це кілька десятків МБ.
"""
import argparse
import datetime as dt
import gzip
import json
import math
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

BUILDS = "https://build.protomaps.com/{date}.pmtiles"
MAX_SOURCE_AGE_DAYS = 14
USER_AGENT = "dronewatch-mapbuild"   # без жодних відомостей про користувача чи проєкт: лише назва інструмента


def tile_xy(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def bbox(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """(мін. lon, мін. lat, макс. lon, макс. lat) квадрата зі стороною 2 * radius_km навколо точки."""
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * math.cos(math.radians(lat)))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def tiles_in_bbox(box: tuple[float, float, float, float], min_zoom: int, max_zoom: int) -> list[tuple[int, int, int]]:
    west, south, east, north = box
    out = []
    for z in range(min_zoom, max_zoom + 1):
        x0, y1 = tile_xy(west, south, z)
        x1, y0 = tile_xy(east, north, z)
        out += [(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    return out


class RangeSource:
    """Читає діапазони байтів із віддаленого PMTiles. Каталоги кешуються (їх читають знову й знову), потокобезпечно."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.cache: dict[tuple[int, int], bytes] = {}
        self.lock = threading.Lock()

    def get(self, offset: int, length: int) -> bytes:
        key = (offset, length)
        with self.lock:
            hit = self.cache.get(key)
        if hit is not None:
            return hit
        data = self._fetch(offset, length)
        if length <= 4_000_000:   # каталоги й заголовок; самі тайли кешувати ні до чого
            with self.lock:
                self.cache[key] = data
        return data

    def _fetch(self, offset: int, length: int) -> bytes:
        last: Exception | None = None
        for _ in range(4):
            req = urllib.request.Request(self.url, headers={"Range": f"bytes={offset}-{offset + length - 1}", "User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    data: bytes = r.read()
                    if len(data) == length:
                        return data
                    last = OSError(f"отримано {len(data)} байтів замість {length}")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
        raise OSError(f"не вдалося прочитати діапазон {offset}+{length} з {self.url}: {last}")


def find_source(date: dt.date | None = None) -> str:
    """Найновіша доступна збірка Protomaps (вони зʼявляються щодня й старі прибираються)."""
    today = date or dt.datetime.now(dt.UTC).date()
    for back in range(MAX_SOURCE_AGE_DAYS):
        url = BUILDS.format(date=(today - dt.timedelta(days=back)).strftime("%Y%m%d"))
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT}), timeout=30):
                return url
        except urllib.error.URLError:
            continue
    raise SystemExit(f"не знайшов збірку Protomaps за останні {MAX_SOURCE_AGE_DAYS} днів: вкажіть --source вручну")


def build(source_url: str, out: Path, box: tuple[float, float, float, float], min_zoom: int, max_zoom: int,
          workers: int = 8, progress: Callable[[str], None] = print) -> int:
    try:
        from pmtiles.reader import deserialize_directory, deserialize_header, find_tile, zxy_to_tileid
        from pmtiles.tile import Compression, TileType
        from pmtiles.writer import Writer
    except ImportError:
        raise SystemExit('потрібна бібліотека pmtiles: pip install -e ".[map]"') from None

    src = RangeSource(source_url)
    header = deserialize_header(src.get(0, 127))
    if header["tile_type"] != TileType.MVT:
        raise SystemExit("джерело не з векторними тайлами (MVT)")
    raw_meta = src.get(header["metadata_offset"], header["metadata_length"])
    metadata = json.loads(gzip.decompress(raw_meta) if header["internal_compression"] == Compression.GZIP else raw_meta)

    def read_tile(zxy: tuple[int, int, int]) -> tuple[int, bytes] | None:
        tile_id = zxy_to_tileid(*zxy)
        offset, length = header["root_offset"], header["root_length"]
        for _ in range(4):
            entry = find_tile(deserialize_directory(src.get(offset, length)), tile_id)   # deserialize_directory сама розпаковує gzip
            if entry is None:
                return None
            if entry.run_length == 0:   # посилання на листовий каталог
                offset, length = header["leaf_directory_offset"] + entry.offset, entry.length
                continue
            return tile_id, src.get(header["tile_data_offset"] + entry.offset, entry.length)
        return None

    wanted = tiles_in_bbox(box, min_zoom, max_zoom)
    progress(f"тайлів у межах: {len(wanted)}; джерело: {source_url}")
    found: list[tuple[int, bytes]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, res in enumerate(pool.map(read_tile, wanted), 1):
            if res is not None:
                found.append(res)
            if i % 200 == 0 or i == len(wanted):
                progress(f"  {i}/{len(wanted)}")
    if not found:
        raise SystemExit("у цій ділянці немає тайлів")
    found.sort(key=lambda t: t[0])   # PMTiles вимагає тайли в порядку Гільберта

    out.parent.mkdir(parents=True, exist_ok=True)
    new_header: dict[str, Any] = {
        "version": 3, "tile_type": TileType.MVT, "tile_compression": header["tile_compression"],
        "min_lon_e7": int(box[0] * 1e7), "min_lat_e7": int(box[1] * 1e7),
        "max_lon_e7": int(box[2] * 1e7), "max_lat_e7": int(box[3] * 1e7),
        "center_zoom": min(14, max_zoom), "center_lon_e7": int((box[0] + box[2]) / 2 * 1e7),
        "center_lat_e7": int((box[1] + box[3]) / 2 * 1e7),
    }
    metadata["description"] = "Вирізка для dronewatch (dronewatch-mapbuild)"
    with out.open("wb") as f:
        writer = Writer(f)
        for tile_id, data in found:
            writer.write_tile(tile_id, data)
        writer.finalize(new_header, metadata)
    return len(found)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Оффлайн-карта (PMTiles) навколо точки зі збірки Protomaps",
                                 epilog=(__doc__ or "").split("Дані:")[1])
    ap.add_argument("--lat", type=float, default=48.8584, help="широта центру (за замовчуванням Ейфелева вежа)")
    ap.add_argument("--lon", type=float, default=2.2945, help="довгота центру")
    ap.add_argument("--radius-km", type=float, default=12.0, help="пів-сторони квадрата, км")
    ap.add_argument("--min-zoom", type=int, default=0)
    ap.add_argument("--max-zoom", type=int, default=15, help="у збірках Protomaps вище 15 даних немає")
    ap.add_argument("--source", help="URL PMTiles-збірки (за замовчуванням найновіша з build.protomaps.com)")
    ap.add_argument("--out", type=Path, default=Path("data/map/paris.pmtiles"))
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args(argv)
    if not 0 < a.radius_km <= 60:
        sys.exit("--radius-km: від 0 до 60")
    if not 0 <= a.min_zoom <= a.max_zoom <= 15:
        sys.exit("--min-zoom/--max-zoom: 0..15, мін. не більший за макс.")
    n = build(a.source or find_source(), a.out, bbox(a.lat, a.lon, a.radius_km), a.min_zoom, a.max_zoom, a.workers)
    print(f"готово: {a.out} ({n} тайлів, {a.out.stat().st_size / 1e6:.1f} МБ)")


if __name__ == "__main__":
    main()
