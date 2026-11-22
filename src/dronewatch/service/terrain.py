"""Висота землі над рівнем моря в точці (схід, північ у метрах від початку координат) з файлу, який зробив dronewatch-dem.

Мозаїка Terrarium лежить у піксельній сітці веб-меркатора; для точки рахуємо піксель і беремо білінійну інтерполяцію.
Поза файлом висота невідома (NaN): лінія видимості в таких місцях вважається закритою, а не вільною (див. occlusion.LineOfSight).
"""
import math
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

M_PER_DEG = 111320.0   # як у дашборді й occlusion.py: однакові метри на градус, щоб рельєф і будинки лягали в одну сітку
TILE = 256

Arr = NDArray[np.float64]


class Terrain:
    def __init__(self, elev: NDArray[np.float32], zoom: int, tile0: tuple[int, int], origin: tuple[float, float]) -> None:
        self.elev = elev
        self.zoom = zoom
        self.tile0 = tile0
        self.lat0, self.lon0 = origin
        self.height, self.width = elev.shape
        self._kx = M_PER_DEG * math.cos(math.radians(self.lat0))

    @classmethod
    def load(cls, path: Path, origin: tuple[float, float]) -> "Terrain":
        """origin: (lat, lon) точки (0, 0) системи датчиків. Пошкоджений файл це виняток, а не тиша."""
        with np.load(path) as z:
            elev, zoom, tile0 = z["elev"], int(z["zoom"][0]), z["tile0"]
        if elev.ndim != 2 or elev.shape[0] % TILE or elev.shape[1] % TILE or not 0 <= zoom <= 22 or len(tile0) != 2:
            raise ValueError(f"{path}: пошкоджений файл рельєфу")
        if not np.isfinite(elev).all() or elev.min() < -500 or elev.max() > 9000:
            raise ValueError(f"{path}: висоти поза правдоподібним діапазоном (-500..9000 м)")
        return cls(elev.astype(np.float32), zoom, (int(tile0[0]), int(tile0[1])), origin)

    def _pixels(self, x: Arr, y: Arr) -> tuple[Arr, Arr]:
        lon = self.lon0 + x / self._kx
        lat = self.lat0 + y / M_PER_DEG
        n = 2.0 ** self.zoom * TILE
        px = (lon + 180.0) / 360.0 * n - self.tile0[0] * TILE
        py = (1.0 - np.arcsinh(np.tan(np.radians(lat))) / math.pi) / 2.0 * n - self.tile0[1] * TILE
        return px, py

    def elevation(self, x: Arr, y: Arr) -> Arr:
        """Висота землі (м над рівнем моря) для масиву точок будь-якої форми; NaN там, де файл не покриває."""
        px, py = self._pixels(np.asarray(x, float), np.asarray(y, float))
        fx, fy = px - 0.5, py - 0.5   # піксель i має центр у i + 0.5
        i0, j0 = np.floor(fx).astype(int), np.floor(fy).astype(int)
        inside = (i0 >= 0) & (j0 >= 0) & (i0 + 1 < self.width) & (j0 + 1 < self.height)
        i0, j0 = np.clip(i0, 0, self.width - 2), np.clip(j0, 0, self.height - 2)
        tx, ty = fx - i0, fy - j0
        e = self.elev
        v = (e[j0, i0] * (1 - tx) * (1 - ty) + e[j0, i0 + 1] * tx * (1 - ty)
             + e[j0 + 1, i0] * (1 - tx) * ty + e[j0 + 1, i0 + 1] * tx * ty)
        return np.where(inside, v, np.nan)
