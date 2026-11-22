#!/usr/bin/env python3
"""Завантажує висоти рельєфу навколо точки в один компактний файл .npz (для лінії видимості датчиків повз пагорби).

    pip install -e ".[map]"
    dronewatch-dem --lat 48.8584 --lon 2.2945 --radius-km 12 --out data/map/dem.npz

Джерело: відкриті тайли Terrarium (AWS Open Data, збірка Mapzen: SRTM, GMTED, національні моделі висот; ключ не потрібен).
Висота кодується в кольорі пікселя PNG: R*256 + G + B/256 - 32768 метрів над рівнем моря. Роздільність на 12-му масштабі ~25 м
на широті Парижа; нативні дані SRTM мають ~30 м, тож вищий масштаб точності не додає.
Робиться один раз там, де є інтернет; сервісу під час роботи потрібен лише результат (numpy).

Дані: SRTM (NASA/USGS), GMTED2010, національні набори висот у збірці Mapzen/Tilezen terrain tiles. Атрибуція в README.
"""
import argparse
import io
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from dronewatch.service.mapbuild import bbox, tile_xy

URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
USER_AGENT = "dronewatch-dem"   # без жодних відомостей про користувача чи проєкт: лише назва інструмента
TILE = 256
MAX_TILES = 2500   # стеля: ~160 МБ масиву; більше означає помилку в радіусі чи масштабі


def decode_terrarium(rgb: np.ndarray) -> np.ndarray:
    """Масив (H, W, 3) uint8 у метри над рівнем моря."""
    r, g, b = (rgb[..., i].astype(np.float32) for i in range(3))
    return r * 256.0 + g + b / 256.0 - 32768.0


def fetch_tile(z: int, x: int, y: int) -> np.ndarray:
    try:
        from PIL import Image
    except ImportError:
        raise SystemExit('потрібна бібліотека pillow: pip install -e ".[map]"') from None
    last: Exception | None = None
    for _ in range(4):
        req = urllib.request.Request(URL.format(z=z, x=x, y=y), headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                img = Image.open(io.BytesIO(r.read())).convert("RGB")
            arr = np.asarray(img)
            if arr.shape != (TILE, TILE, 3):
                raise OSError(f"тайл {z}/{x}/{y}: розмір {arr.shape}")
            return decode_terrarium(arr)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
    raise OSError(f"не вдалося отримати тайл {z}/{x}/{y}: {last}")


def build(box: tuple[float, float, float, float], zoom: int, workers: int = 8, progress: Any = print) -> dict[str, np.ndarray]:
    west, south, east, north = box
    x0, y1 = tile_xy(west, south, zoom)
    x1, y0 = tile_xy(east, north, zoom)
    nx, ny = x1 - x0 + 1, y1 - y0 + 1
    if nx * ny > MAX_TILES:
        raise SystemExit(f"забагато тайлів ({nx * ny} > {MAX_TILES}): зменште --radius-km чи --zoom")
    progress(f"тайлів: {nx * ny} (масштаб {zoom})")
    coords = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        tiles = list(ex.map(lambda c: fetch_tile(zoom, *c), coords))
    mosaic = np.zeros((ny * TILE, nx * TILE), np.float32)
    for (x, y), t in zip(coords, tiles, strict=True):
        mosaic[(y - y0) * TILE:(y - y0 + 1) * TILE, (x - x0) * TILE:(x - x0 + 1) * TILE] = t
    progress(f"висоти від {mosaic.min():.0f} до {mosaic.max():.0f} м над рівнем моря")
    return {"elev": mosaic, "zoom": np.array([zoom]), "tile0": np.array([x0, y0])}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Висоти рельєфу (тайли Terrarium) навколо точки в один файл .npz",
                                 epilog=(__doc__ or "").split("Дані:")[1])
    ap.add_argument("--lat", type=float, default=48.8584, help="широта центру (за замовчуванням Ейфелева вежа)")
    ap.add_argument("--lon", type=float, default=2.2945, help="довгота центру")
    ap.add_argument("--radius-km", type=float, default=12.0, help="пів-сторони квадрата, км")
    ap.add_argument("--zoom", type=int, default=12, help="масштаб тайлів (12 ~ 25 м на піксель; вище даних більше не стає)")
    ap.add_argument("--out", type=Path, default=Path("data/map/dem.npz"))
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args(argv)
    if not 0 < a.radius_km <= 60:
        sys.exit("--radius-km: від 0 до 60")
    if not 6 <= a.zoom <= 14:
        sys.exit("--zoom: 6..14")
    data = build(bbox(a.lat, a.lon, a.radius_km), a.zoom, a.workers)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(a.out, **data)  # type: ignore[arg-type]
    print(f"записано {a.out} ({a.out.stat().st_size // 1024} КБ)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
