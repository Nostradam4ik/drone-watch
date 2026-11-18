"""Покриття буферної зони датчиками: що саме система зможе зробити з ціллю в кожній точці зони (для редактора розстановки)."""
import math
from typing import Any

import numpy as np

from dronewatch.fusion.models import Sensor
from dronewatch.service.layout import derive_zones

BLIND, WEAK, GOOD, STRONG = 0, 1, 2, 3
CLASS_NAMES = ("blind", "weak", "good", "strong")
MIN_CROSS = math.radians(20)   # під яким кутом мають перетинатися пеленги, щоб тріангуляція вважалась надійною (трекер відкидає < ~9°)
MAX_CELLS = 6000               # стеля клітинок у відповіді: розмір клітинки росте, поки зони не вміщуються
MAX_GRID = 1_500_000           # стеля сітки навколо всіх зон (охоплювальний прямокутник)


def _grid_size(zones: list[Any]) -> float:
    smallest = min(z.radius for z in zones)
    size = min(60.0, max(15.0, round(smallest / 15 / 5) * 5))
    xs0 = min(z.center[0] - z.radius for z in zones)
    xs1 = max(z.center[0] + z.radius for z in zones)
    ys0 = min(z.center[1] - z.radius for z in zones)
    ys1 = max(z.center[1] + z.radius for z in zones)
    area = sum(math.pi * z.radius ** 2 for z in zones)
    while area / size ** 2 > MAX_CELLS or ((xs1 - xs0) / size) * ((ys1 - ys0) / size) > MAX_GRID:
        size *= 1.25
    return size


def coverage(sensors: list[Sensor]) -> dict[str, Any]:
    """Клас кожної клітинки буферних зон (зони виводяться з радарів, див. layout.derive_zones):
    strong: радар І (тріангуляція пеленгами або другий радар) І RF, тобто позиція з висотою, запасний канал і ідентифікація;
    good: є радар (позиція й висота); weak: покриває лише пеленговий датчик чи пара без надійного перетину (грубо, без висоти);
    blind: не бачить жоден."""
    zones = [z for z in derive_zones(sensors) if z.level == "WARNING"]
    if not zones:
        return {"cell": 0.0, "cells": [], "stats": dict.fromkeys(CLASS_NAMES, 0.0) | {"total": 0, "ident": 0.0}}
    size = _grid_size(zones)
    x0 = math.floor(min(z.center[0] - z.radius for z in zones) / size)
    x1 = math.ceil(max(z.center[0] + z.radius for z in zones) / size)
    y0 = math.floor(min(z.center[1] - z.radius for z in zones) / size)
    y1 = math.ceil(max(z.center[1] + z.radius for z in zones) / size)
    ix, iy = np.meshgrid(np.arange(x0, x1), np.arange(y0, y1), indexing="ij")
    cx, cy = (ix + 0.5) * size, (iy + 0.5) * size
    inside = np.zeros(cx.shape, bool)
    for z in zones:
        inside |= np.hypot(cx - z.center[0], cy - z.center[1]) <= z.radius
    ix, iy, cx, cy = ix[inside], iy[inside], cx[inside], cy[inside]
    n = len(cx)

    radars = np.zeros(n, int)
    rf = np.zeros(n, bool)
    bearings: list[tuple[Any, Any]] = []   # (покриває?, напрямок на датчик) для кожного пеленгового датчика
    for s in sensors:
        dx, dy = s.pos[0] - cx, s.pos[1] - cy
        seen = np.hypot(dx, dy) <= s.max_range
        if s.fov < 2 * np.pi - 1e-9:   # секторний датчик бачить лише в межах свого кута огляду
            off = np.abs((np.arctan2(-dy, -dx) - s.heading + np.pi) % (2 * np.pi) - np.pi)
            seen &= off <= s.fov / 2
        if s.kind == "radar":
            radars += seen
        else:
            bearings.append((seen, np.arctan2(dy, dx)))
            rf |= seen & (s.label == "rf")
    triangulated = np.zeros(n, bool)
    for i in range(len(bearings)):
        for j in range(i + 1, len(bearings)):
            diff = np.abs((bearings[i][1] - bearings[j][1] + np.pi) % (2 * np.pi) - np.pi)
            triangulated |= bearings[i][0] & bearings[j][0] & (np.minimum(diff, np.pi - diff) >= MIN_CROSS)
    any_bearing = np.zeros(n, bool)
    for seen, _ in bearings:
        any_bearing |= seen

    cls = np.full(n, BLIND)
    cls[any_bearing | triangulated] = WEAK
    cls[radars >= 1] = GOOD
    cls[(radars >= 1) & (triangulated | (radars >= 2)) & rf] = STRONG
    stats = {name: round(float(np.mean(cls == k)) * 100, 1) if n else 0.0 for k, name in enumerate(CLASS_NAMES)}
    stats["total"] = n
    stats["ident"] = round(float(np.mean(rf)) * 100, 1) if n else 0.0   # частка зони, де RF може визначити модель дрона
    return {"cell": size, "cells": [[int(a), int(b), int(c)] for a, b, c in zip(ix, iy, cls, strict=True)], "stats": stats}
