"""Метрики якості трекінгу: OSPA, RMSE, частка хибних треків, пропуски, перемикання ID, час до підтвердження.

Чисті функції без залежності від сервісу: на вхід кадри «треки» і «еталон» (де цілі були насправді), на вихід підсумок.
Без еталону (`truth` порожній) `Scorer` рахує лише те, що від нього не залежить: скільки треків було, як довго жили,
скільки «мигаючих» (коротших за `flicker_s`). OSPA, RMSE і частку хибних треків без еталону порахувати неможливо, і
підсумок прямо каже `has_truth: false`, а не підставляє нулі.

OSPA (Schuhmacher, Vo, Vo 2008): відстань між двома множинами точок, що штрафує і за похибку положення (locality), і за хибну
чи пропущену ціль (cardinality), кожна не дорожча за `c` метрів. Береться пошук оптимального зіставлення (угорський алгоритм)."""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.spatial.distance import cdist

Points = np.ndarray   # (n, d): положення в метрах


def ospa(x: Points, y: Points, c: float = 100.0, p: float = 2.0) -> tuple[float, float, float]:
    """(OSPA, складова положення, складова кількості) між множинами точок x (n, d) і y (m, d). Обидві порожні: (0, 0, 0)."""
    if c <= 0 or p < 1:
        raise ValueError("OSPA: потрібні c > 0 і p >= 1")
    m, n = len(x), len(y)
    if m == 0 and n == 0:
        return 0.0, 0.0, 0.0
    big = max(m, n)
    loc_sum = 0.0
    if m and n:
        cost = np.minimum(cdist(x, y), c) ** p
        rows, cols = linear_sum_assignment(cost)
        loc_sum = float(cost[rows, cols].sum())
    card_sum = (c ** p) * abs(m - n)
    return ((loc_sum + card_sum) / big) ** (1 / p), (loc_sum / big) ** (1 / p), (card_sum / big) ** (1 / p)


class TruthSet:
    """Еталон: для кожної цілі впорядковані за часом положення; між відліками лінійна інтерполяція. Ціль «активна» лише
    між першим і останнім відліком і не через розрив довший за `max_gap_s` (GPS-лог із пропуском не вигадуємо)."""

    def __init__(self, rows: list[tuple[float, str, float, float, float]], max_gap_s: float = 5.0) -> None:
        by: dict[str, list[tuple[float, float, float, float]]] = defaultdict(list)
        for t, ident, x, y, z in rows:
            by[ident].append((t, x, y, z))
        self.max_gap_s = max_gap_s
        self._t: dict[str, np.ndarray] = {}
        self._p: dict[str, np.ndarray] = {}
        for ident, pts in by.items():
            pts.sort()
            arr = np.array(pts, float)
            self._t[ident], self._p[ident] = arr[:, 0], arr[:, 1:4]

    def __bool__(self) -> bool:
        return bool(self._t)

    def ids(self) -> list[str]:
        return sorted(self._t)

    def first_time(self, ident: str) -> float:
        return float(self._t[ident][0])

    def at(self, t: float) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        for ident, ts in self._t.items():
            if t < ts[0] or t > ts[-1]:
                continue
            k = int(np.searchsorted(ts, t))
            if k > 0 and k < len(ts) and ts[k] != t and ts[k] - ts[k - 1] > self.max_gap_s:
                continue   # t лежить усередині розриву
            out[ident] = np.array([np.interp(t, ts, self._p[ident][:, d]) for d in range(3)])
        return out


@dataclass(slots=True)
class _Life:
    first: float
    last: float
    scans: int = 0
    matched: int = 0


@dataclass
class Scorer:
    """Накопичує кадри через `step()`, підсумок у `summary()`."""
    cutoff: float = 100.0     # c для OSPA, м
    p: float = 2.0
    gate: float = 60.0        # м: трек привʼязаний до цілі, якщо ближче за це
    dim: int = 2              # 2: лише горизонталь (висоту без радара трекер лише припускає); 3: з висотою
    flicker_s: float = 5.0    # трек, що прожив менше, вважається «мигаючим»
    false_track_share: float = 0.5   # трек хибний, якщо менше цієї частки його кадрів привʼязано до цілі
    scans: int = 0
    _ospa: list[tuple[float, float, float]] = field(default_factory=list)
    _errs: list[float] = field(default_factory=list)
    _life: dict[int, _Life] = field(default_factory=dict)
    _truth_first: dict[str, float] = field(default_factory=dict)
    _first_match: dict[str, float] = field(default_factory=dict)
    _last_track: dict[str, int] = field(default_factory=dict)
    _switches: int = 0
    _missed: int = 0
    _target_steps: int = 0
    _false_scans: int = 0
    _track_scans: int = 0
    _peak: int = 0
    _has_truth: bool = False

    def step(self, t: float, tracks: dict[int, np.ndarray], truth: dict[str, np.ndarray], *, truth_present: bool = True) -> None:
        """tracks: id -> положення (x, y[, z]); truth: ім'я -> положення на момент t. `truth_present` false: еталону в цьому
        прогоні немає взагалі (порожній `truth` тоді не означає «ціль зникла»)."""
        self.scans += 1
        d = self.dim
        self._peak = max(self._peak, len(tracks))
        for tid in tracks:
            life = self._life.get(tid)
            if life is None:
                life = self._life[tid] = _Life(t, t)
            life.last, life.scans = t, life.scans + 1
        if not truth_present:
            return
        self._has_truth = True
        for name in truth:
            self._truth_first.setdefault(name, t)
        t_ids, t_pos = list(tracks), np.array([v[:d] for v in tracks.values()], float).reshape(len(tracks), d)
        g_names, g_pos = list(truth), np.array([v[:d] for v in truth.values()], float).reshape(len(truth), d)
        self._ospa.append(ospa(t_pos, g_pos, self.cutoff, self.p))
        matched_tracks: set[int] = set()
        if t_ids and g_names:
            dist = cdist(t_pos, g_pos)
            rows, cols = linear_sum_assignment(dist)
            for i, j in zip(rows, cols, strict=True):
                if dist[i, j] <= self.gate:
                    tid, name = t_ids[i], g_names[j]
                    matched_tracks.add(tid)
                    self._life[tid].matched += 1
                    self._errs.append(float(dist[i, j]))
                    self._first_match.setdefault(name, t)
                    if name in self._last_track and self._last_track[name] != tid:
                        self._switches += 1
                    self._last_track[name] = tid
        self._target_steps += len(g_names)
        self._missed += len(g_names) - len(matched_tracks)
        self._track_scans += len(t_ids)
        self._false_scans += len(t_ids) - len(matched_tracks)

    def summary(self) -> dict[str, Any]:
        lives = list(self._life.values())
        out: dict[str, Any] = {
            "scans": self.scans, "has_truth": self._has_truth, "tracks_total": len(lives), "tracks_peak": self._peak,
            "flicker_tracks": sum(1 for lf in lives if lf.last - lf.first < self.flicker_s),
            "mean_track_life_s": float(np.mean([lf.last - lf.first for lf in lives])) if lives else math.nan,
            "params": {"cutoff_m": self.cutoff, "p": self.p, "gate_m": self.gate, "dim": self.dim,
                       "false_track_share": self.false_track_share, "flicker_s": self.flicker_s},
        }
        if not self._has_truth:
            return out
        arr = np.array(self._ospa, float).reshape(-1, 3)
        busy = arr[(arr != 0).any(axis=1)] if len(arr) else arr   # кадри, де щось було (еталон чи треки)
        false_tracks = sum(1 for lf in lives if lf.scans and lf.matched / lf.scans < self.false_track_share)
        ttc = [self._first_match[n] - self._truth_first[n] for n in self._first_match]
        out |= {
            "truth_targets": len(self._truth_first),
            "ospa_mean": float(arr[:, 0].mean()) if len(arr) else math.nan,
            "ospa_mean_busy": float(busy[:, 0].mean()) if len(busy) else math.nan,   # без порожніх кадрів
            "ospa_loc": float(arr[:, 1].mean()) if len(arr) else math.nan,
            "ospa_card": float(arr[:, 2].mean()) if len(arr) else math.nan,
            "rmse_m": float(np.sqrt(np.mean(np.square(self._errs)))) if self._errs else math.nan,
            "missed_pct": 100 * self._missed / self._target_steps if self._target_steps else math.nan,
            "false_track_scan_pct": 100 * self._false_scans / self._track_scans if self._track_scans else 0.0,
            "false_tracks": false_tracks,
            "false_track_pct": 100 * false_tracks / len(lives) if lives else 0.0,
            "id_switches": self._switches,
            "time_to_confirm_s": float(np.mean(ttc)) if ttc else math.nan,
            "never_found": len(self._truth_first) - len(self._first_match),
        }
        return out
