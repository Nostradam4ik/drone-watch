"""Лінія видимості між датчиком і ціллю з урахуванням рельєфу й будівель (для покриття буферної зони).

Модель свідомо проста й чесна щодо меж:
- висоти датчика, цілі й будинків відлічуються від землі; без файлу рельєфу земля пласка, з ним (terrain.py) кожна точка має свою
  висоту над рівнем моря, і промінь, що опинився нижче землі, закритий. Поза файлом рельєфу висота невідома: видимість закрита;
- кривизна Землі й рефракція не враховані (на 2 км це ~0,3 м, на 5 км ~2 м: менше за похибку висот);
- будинок суцільний від землі (або від min_height) до висоти height; двори ігноруються;
- датчик, що стоїть усередині контуру будинку, вважається встановленим на цьому даху: власний будинок його не затуляє;
- дроти, дерева й техніка не враховуються, тож це верхня межа видимості, а не гарантія.
Будинки з файлу, який зробив dronewatch-buildings (контури lon/lat). Растр висот рахується шматками й кешується, бо редактор розстановки
просить покриття щокілька сотень мс під час перетягування датчика.
"""
import math
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from dronewatch.service.terrain import Terrain

M_PER_DEG = 111320.0   # як у дашборді (map.js): ті самі метри на градус, щоб будинки й датчики лягали в одну сітку
RES = 5.0              # розмір пікселя растра, м: вужчі за нього проходи між будинками вважаються закритими (це консервативно)
BLOCK = 128            # сторона шматка растра, пікселів
MAX_BLOCKS = 300       # скільки шматків тримати в кеші (~200 КБ кожен)
MAX_RAY_STEPS = 600
CHUNK = 800            # скільки клітинок обробляти за раз: стеля памʼяті на матрицю «клітинки x кроки променя»

F32 = NDArray[np.float32]
Bbox = tuple[float, float, float, float]


class Buildings:
    def __init__(self, xy: NDArray[np.float64], starts: NDArray[np.int64], height: F32, min_height: F32,
                 assumed_height: float = 0.0) -> None:
        self.xy = xy.astype(np.float32)
        self.starts = starts
        self.height = height.astype(np.float32)
        self.min_height = min_height.astype(np.float32)
        self.assumed_height = assumed_height   # висота, припущена там, де в даних її не було (для показу в підсумку)
        lo = starts[:-1]
        self.x0 = np.minimum.reduceat(self.xy[:, 0], lo)
        self.x1 = np.maximum.reduceat(self.xy[:, 0], lo)
        self.y0 = np.minimum.reduceat(self.xy[:, 1], lo)
        self.y1 = np.maximum.reduceat(self.xy[:, 1], lo)
        self.count = len(height)
        self._blocks: OrderedDict[tuple[int, int], tuple[F32, F32, NDArray[np.int32]]] = OrderedDict()
        self._lock = threading.Lock()

    @classmethod
    def load(cls, path: Path, origin: tuple[float, float]) -> "Buildings":
        """origin: (lat, lon) точки (0, 0) системи датчиків. Файл від dronewatch-buildings; помилка читання це виняток, а не тиша."""
        with np.load(path) as z:
            lonlat, starts, height, min_height = z["lonlat"], z["starts"], z["height"], z["min_height"]
            assumed = float(z["assumed"][0]) if "assumed" in z.files else 0.0
        if len(starts) != len(height) + 1 or starts[-1] != len(lonlat) or len(min_height) != len(height):
            raise ValueError(f"{path}: пошкоджений файл будівель")
        lat0, lon0 = origin
        x = (lonlat[:, 0] - lon0) * M_PER_DEG * math.cos(math.radians(lat0))
        y = (lonlat[:, 1] - lat0) * M_PER_DEG
        return cls(np.column_stack([x, y]), starts, height, min_height, assumed)

    # ---------- растр ----------
    def _paint(self, bx: int, by: int) -> tuple[F32, F32, NDArray[np.int32]]:
        gx0, gy0 = bx * BLOCK * RES, by * BLOCK * RES
        gx1, gy1 = gx0 + BLOCK * RES, gy0 + BLOCK * RES
        h = np.zeros((BLOCK, BLOCK), np.float32)
        m = np.zeros((BLOCK, BLOCK), np.float32)
        ids = np.zeros((BLOCK, BLOCK), np.int32)
        near = np.flatnonzero((self.x1 >= gx0) & (self.x0 < gx1) & (self.y1 >= gy0) & (self.y0 < gy1))
        for i in near[np.argsort(self.height[near], kind="stable")]:   # від нижчих до вищих: найвищий лишається зверху
            i0, i1 = max(0, int((self.x0[i] - gx0) // RES)), min(BLOCK, int((self.x1[i] - gx0) // RES) + 1)
            j0, j1 = max(0, int((self.y0[i] - gy0) // RES)), min(BLOCK, int((self.y1[i] - gy0) // RES) + 1)
            if i0 >= i1 or j0 >= j1:
                continue
            px = gx0 + (np.arange(i0, i1) + 0.5) * RES
            py = gy0 + (np.arange(j0, j1) + 0.5) * RES
            inside = _inside(self.xy[self.starts[i]:self.starts[i + 1]], px[:, None], py[None, :])
            if not inside.any():
                # будинок менший за піксель: ставимо його в піксель під центром, щоб малі споруди не зникали з розрахунку
                ci = int(((self.x0[i] + self.x1[i]) / 2 - gx0) // RES)
                cj = int(((self.y0[i] + self.y1[i]) / 2 - gy0) // RES)
                if not (0 <= ci < BLOCK and 0 <= cj < BLOCK):
                    continue
                inside = np.zeros((i1 - i0, j1 - j0), bool)
                inside[min(max(ci - i0, 0), i1 - i0 - 1), min(max(cj - j0, 0), j1 - j0 - 1)] = True
            h[i0:i1, j0:j1][inside] = self.height[i]
            m[i0:i1, j0:j1][inside] = self.min_height[i]
            ids[i0:i1, j0:j1][inside] = i + 1
        return h, m, ids

    def _block(self, bx: int, by: int) -> tuple[F32, F32, NDArray[np.int32]]:
        with self._lock:
            hit = self._blocks.get((bx, by))
            if hit is not None:
                self._blocks.move_to_end((bx, by))
                return hit
        blk = self._paint(bx, by)
        with self._lock:
            self._blocks[(bx, by)] = blk
            while len(self._blocks) > MAX_BLOCKS:
                self._blocks.popitem(last=False)
        return blk

    def window(self, box: Bbox) -> tuple[F32, F32, NDArray[np.int32], float, float]:
        """Растри висоти, нижньої межі й номера будинку для прямокутника box (x0, y0, x1, y1) метрів, плюс координата кута сітки."""
        bx0, by0 = math.floor(box[0] / (BLOCK * RES)), math.floor(box[1] / (BLOCK * RES))
        bx1, by1 = math.floor(box[2] / (BLOCK * RES)), math.floor(box[3] / (BLOCK * RES))
        nx, ny = bx1 - bx0 + 1, by1 - by0 + 1
        if nx * ny > MAX_BLOCKS:
            raise ValueError("ділянка для лінії видимості завелика")
        h = np.zeros((nx * BLOCK, ny * BLOCK), np.float32)
        m = np.zeros_like(h)
        ids = np.zeros((nx * BLOCK, ny * BLOCK), np.int32)
        for a in range(nx):
            for b in range(ny):
                bh, bm, bi = self._block(bx0 + a, by0 + b)
                sl = (slice(a * BLOCK, (a + 1) * BLOCK), slice(b * BLOCK, (b + 1) * BLOCK))
                h[sl], m[sl], ids[sl] = bh, bm, bi
        return h, m, ids, bx0 * BLOCK * RES, by0 * BLOCK * RES

    # ---------- видимість ----------
    def containing(self, x: float, y: float) -> list[int]:
        """Номери (з 1) будинків, у контурі яких лежить точка."""
        near = np.flatnonzero((self.x0 <= x) & (self.x1 >= x) & (self.y0 <= y) & (self.y1 >= y))
        return [int(i) + 1 for i in near
                if _inside(self.xy[self.starts[i]:self.starts[i + 1]], np.array([[x]]), np.array([[y]]))[0, 0]]


def _inside(ring: F32, px: NDArray[np.floating[Any]], py: NDArray[np.floating[Any]]) -> NDArray[np.bool_]:
    """Парне-непарне правило: чи лежать точки сітки (px по рядках, py по стовпцях) усередині кільця."""
    inside = np.zeros((px.shape[0], py.shape[1]), bool)
    x, y = ring[:, 0], ring[:, 1]
    j = len(ring) - 1
    for i in range(len(ring)):
        cond = (y[i] > py) != (y[j] > py)
        with np.errstate(divide="ignore", invalid="ignore"):
            xcross = (x[j] - x[i]) * (py - y[i]) / (y[j] - y[i]) + x[i]
        inside ^= cond & (px < xcross)
        j = i
    return inside


class LineOfSight:
    """Чи бачить датчик ціль: промінь не має зайти під землю (рельєф) і в будинок (будинки). Потрібне хоч одне з двох."""

    def __init__(self, buildings: Buildings | None = None, terrain: Terrain | None = None) -> None:
        if buildings is None and terrain is None:
            raise ValueError("потрібні будинки чи рельєф")
        self.buildings = buildings
        self.terrain = terrain

    def visible(self, sensor: NDArray[np.float64], cx: NDArray[np.float64], cy: NDArray[np.float64],
                target_alt: float) -> NDArray[np.bool_]:
        """Для кожної точки (cx, cy) на висоті target_alt над землею: чи бачить її датчик sensor = (x, y, висота над землею)."""
        n = len(cx)
        out = np.ones(n, bool)
        if n == 0:
            return out
        sx, sy, sz = float(sensor[0]), float(sensor[1]), float(sensor[2]) if len(sensor) > 2 else 0.0
        terrain, bld = self.terrain, self.buildings
        gs, gt = 0.0, np.zeros(n)
        if terrain is not None:
            gs = float(terrain.elevation(np.array([sx]), np.array([sy]))[0])
            gt = terrain.elevation(cx, cy)
            if math.isnan(gs):
                return np.zeros(n, bool)   # датчик поза файлом рельєфу: висоту землі не знаємо, покриття не обіцяємо
            out &= ~np.isnan(gt)
        h = m = ids = None
        gx0 = gy0 = 0.0
        own = np.array([-1], np.int32)
        if bld is not None:
            pad = 2 * RES
            try:
                h, m, ids, gx0, gy0 = bld.window((min(sx, float(cx.min())) - pad, min(sy, float(cy.min())) - pad,
                                                  max(sx, float(cx.max())) + pad, max(sy, float(cy.max())) + pad))
            except ValueError:
                return np.zeros(n, bool)   # ділянка завелика для растра: краще не обіцяти покриття, ніж пообіцяти зайве
            own = np.array(bld.containing(sx, sy) or [-1], np.int32)
        steps = int(min(MAX_RAY_STEPS, math.ceil(float(np.hypot(cx - sx, cy - sy).max()) / RES) + 1))
        t = (np.arange(1, steps) / steps)[None, :]
        for a in range(0, n, CHUNK):
            sl = slice(a, a + CHUNK)
            px, py = sx + (cx[sl, None] - sx) * t, sy + (cy[sl, None] - sy) * t
            alt = (gs + sz) + (gt[sl, None] + target_alt - gs - sz) * t   # висота променя над рівнем моря
            ground = terrain.elevation(px, py) if terrain is not None else 0.0
            hit = np.zeros(px.shape, bool)
            if terrain is not None:
                hit |= np.isnan(ground) | (ground > alt)
            if bld is not None and h is not None and m is not None and ids is not None:
                gi = np.floor((px - gx0) / RES).astype(int)
                gj = np.floor((py - gy0) / RES).astype(int)
                hh = h[gi, gj]
                hit |= (hh > 0) & (ground + hh > alt) & (ground + m[gi, gj] <= alt) & ~np.isin(ids[gi, gj], own)
            out[sl] &= ~hit.any(axis=1)
        return out
