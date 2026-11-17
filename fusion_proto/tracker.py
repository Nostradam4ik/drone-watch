"""Трекер: EKF + gating за Махаланобісом + Hungarian + життєвий цикл M/N."""
from collections import defaultdict, deque
from collections.abc import Iterable
from typing import Literal

import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.stats import chi2

from models import Floats, Sensor, cv_matrices

GATE_P = 0.999
BIG = 1e9
N_WINDOW = 5                 # вікно підтвердження (скани)
CONFIRM_W = 7                # поріг ваги: радар-детекція важить 2, кожен пеленг 1, максимум 3 за скан
MAX_MISSES = 6               # скільки сканів confirmed-трек живе без детекцій (coasting)
Q_MODELS = (1.0, 12.0)       # м/с²: шум прискорення спокійної та маневрової моделей (IMM)
MU0 = (0.8, 0.2)
TRANS = np.array([[0.96, 0.04], [0.10, 0.90]])  # перехід між моделями за скан
MERGE_DIST = 40.0            # м, для видалення дублікатів та ghost-ініціалізацій
ASSUMED_ALT, ALT_SIGMA = 60.0, 100.0
BIAS_RATE = 0.02            # швидкість навчання онлайн-оцінки bias
RADAR_COVER = 0.9            # частка дальності радара, де він має «бачити» ціль
RADAR_MISS_LIMIT = 6         # сканів без радара в його зоні, і трек (з пеленгів) видаляється
MAX_TRACKS = 150             # стеля на кількість треків: інакше потік хибних детекцій роздуває стан без меж
MAX_BEARING_CANDIDATES = 30  # скільки непривʼязаних пеленгів від одного датчика йде в тріангуляцію за скан (вона O(n^2))


class Track:
    _next_id = 1

    def __init__(self, x: Floats, P: Floats, t: float) -> None:
        self.id = Track._next_id
        Track._next_id += 1
        self.models = [(x.copy(), P.copy()) for _ in Q_MODELS]
        self.mu = np.array(MU0)
        self.x, self.P = x, P
        self._combine()
        self.state: Literal["TENTATIVE", "CONFIRMED"] = "TENTATIVE"
        self.hist: deque[int] = deque(maxlen=N_WINDOW)
        self.misses = 0
        self.born = t
        self.confirmed_at: float | None = None   # час підтвердження; None, доки трек TENTATIVE
        self.kinds: defaultdict[str, float] = defaultdict(lambda: -1e9)  # фізичний тип датчика -> час останньої детекції
        self.updated = False
        self.hit_w = 0
        self.radar_hit = False
        self.radar_scans: deque[int] = deque(maxlen=N_WINDOW)
        self.radar_miss = 0

    def _combine(self) -> None:
        """Моментне злиття моделей у єдиний стан x, P (його бачать gating і решта коду)."""
        self.x = sum(m * x for m, (x, _) in zip(self.mu, self.models))
        self.P = sum(m * (P + np.outer(x - self.x, x - self.x)) for m, (x, P) in zip(self.mu, self.models))

    def predict(self, dt: float) -> None:
        """IMM: змішування моделей за матрицею переходів, потім предикція кожної зі своїм шумом."""
        c = TRANS.T @ self.mu                                  # прогнозовані ваги моделей
        mixed = []
        for j in range(len(Q_MODELS)):
            w = TRANS[:, j] * self.mu / c[j]                   # P(модель i була | зараз j)
            x0 = sum(wi * x for wi, (x, _) in zip(w, self.models))
            P0 = sum(wi * (P + np.outer(x - x0, x - x0)) for wi, (x, P) in zip(w, self.models))
            F, Q = cv_matrices(dt, Q_MODELS[j])
            mixed.append((F @ x0, F @ P0 @ F.T + Q))
        self.models, self.mu = mixed, c
        self._combine()

    def update(self, sensor: Sensor, z: Floats, t: float) -> None:
        like = np.zeros(len(self.models))
        for j, (x, P) in enumerate(self.models):
            H = sensor.jacobian(x)
            y = sensor.residual(z, sensor.h(x))
            S = H @ P @ H.T + sensor.R
            K = P @ H.T @ np.linalg.inv(S)
            I_KH = np.eye(6) - K @ H
            self.models[j] = (x + K @ y, I_KH @ P @ I_KH.T + K @ sensor.R @ K.T)  # форма Джозефа
            like[j] = np.exp(-0.5 * y @ np.linalg.solve(S, y)) / np.sqrt((2 * np.pi) ** sensor.dim * np.linalg.det(S))
        self.mu = self.mu * like + 1e-12
        self.mu /= self.mu.sum()
        self._combine()
        self.updated = True
        self.hit_w += 2 if sensor.kind == "radar" else 1
        self.radar_hit |= sensor.kind == "radar"
        self.kinds[sensor.label] = t

    def score(self, t: float, window: float = 3.0) -> float:
        """Достовірність: різні за фізикою датчики цінніші за однакові; історія додає довіри."""
        n_kinds = sum(1 for ts in self.kinds.values() if t - ts <= window)
        return n_kinds + 0.05 * sum(self.hist)

    def gate_costs(self, sensor: Sensor, zs: Floats) -> tuple[Floats, Floats]:
        """Відстань Махаланобіса d2 і вартість (d2 + ln det S) від треку до КОЖНОГО вимірювання з zs (m, dim).
        Прогноз h(x), якобіан, S, S^-1 і ln det S залежать лише від пари «трек × датчик», тож рахуємо їх один раз,
        а не для кожного вимірювання, і весь рядок вартостей отримуємо однією матричною операцією."""
        H = sensor.jacobian(self.x)
        S = H @ self.P @ H.T + sensor.R
        Y = sensor.residuals(zs, sensor.h(self.x))
        d2 = np.einsum("ij,jk,ik->i", Y, np.linalg.inv(S), Y)
        return d2, d2 + np.linalg.slogdet(S)[1]


class Tracker:
    def __init__(self, sensors: Iterable[Sensor], online_bias: bool = False, max_tracks: int = MAX_TRACKS) -> None:
        self.online_bias = online_bias
        self.max_tracks = max_tracks
        self.dropped = {"tracks_capped": 0, "bearings_truncated": 0}  # що відкинуто через стелі в останньому скані
        self.sensors = {s.name: s for s in sensors}
        self.tracks: list[Track] = []
        self.t: float | None = None
        self.radars = [s for s in sensors if s.kind == "radar"]
        self.down: set[str] = set()    # імена датчиків, визнаних відмовленими (виставляє сервіс за станом здоровʼя)
        self.heard: set[str] | None = None    # імена датчиків, що озивалися в цьому скані; None = усі (поведінка прототипу)

    def _init_from_radar(self, s: Sensor, z: Floats, t: float) -> Track:
        r, az, el = z
        pos = s.pos + r * np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)])
        P = np.diag([15 ** 2] * 3 + [15 ** 2] * 3)
        return Track(np.concatenate([pos, np.zeros(3)]), P, t)

    def _init_from_bearings(self, s1: Sensor, z1: Floats, s2: Sensor, z2: Floats, t: float) -> Track | None:
        """Перетин двох променів у горизонтальній площині (тріангуляція)."""
        d1 = np.array([np.cos(z1[0]), np.sin(z1[0])])
        d2 = np.array([np.cos(z2[0]), np.sin(z2[0])])
        A = np.array([d1, -d2]).T
        if abs(np.linalg.det(A)) < 0.15:  # майже паралельні промені: геометрія погана
            return None
        k = np.linalg.solve(A, s2.pos[:2] - s1.pos[:2])
        if k[0] < 0 or k[1] < 0 or k[0] > 2500 or k[1] > 2500:
            return None
        if k[0] > s1.max_range or k[1] > s2.max_range:
            return None
        xy = s1.pos[:2] + k[0] * d1
        P = np.diag([60 ** 2, 60 ** 2, ALT_SIGMA ** 2] + [15 ** 2] * 3)
        return Track(np.array([xy[0], xy[1], ASSUMED_ALT, 0, 0, 0], float), P, t)

    def _cover_radars(self, tr: Track) -> list[Sensor]:
        """Справні радари, у зоні покриття яких зараз трек. Відмовлений радар (self.down) цілі «не бачить» нормально,
        тож його мовчання не доказ, що ціль хибна, і правила «обовʼязкове радарне підтвердження» на нього не діють."""
        return [r for r in self.radars
                if r.name not in self.down and np.linalg.norm(tr.x[:3] - r.pos) < RADAR_COVER * r.max_range]

    def _in_radar_cover(self, tr: Track) -> bool:
        return bool(self._cover_radars(tr))

    def _near_existing(self, pos: Floats) -> bool:
        if not self.tracks:
            return False
        X = np.array([tr.x[:3] for tr in self.tracks])
        return bool((np.linalg.norm(X - pos, axis=1) < MERGE_DIST).any())

    def step(self, t: float, detections: list[tuple[str, Floats]], heard: set[str] | None = None,
             down: Iterable[str] = ()) -> None:
        """detections: список (sensor_name, z). Викликається раз на скан.
        heard: датчики, від яких у цьому скані було ХОЧ ЩОСЬ (детекції або heartbeat). Скан, у якому радар мовчав
        цілком, не рахується як «радар не побачив ціль»: це відсутність звʼязку, а не відсутність цілі.
        down: датчики, визнані відмовленими; їхні правила покриття вимкнені."""
        dt = 0.0 if self.t is None else t - self.t
        self.t = t
        self.heard, self.down = heard, set(down)
        self.dropped = {"tracks_capped": 0, "bearings_truncated": 0}
        for tr in self.tracks:
            tr.updated = False
            tr.hit_w = 0
            tr.radar_hit = False
            if dt > 0:
                tr.predict(dt)

        by_sensor = defaultdict(list)
        for name, z in detections:
            by_sensor[name].append(z - self.sensors[name].bias_est)

        leftovers_bearing: list[tuple[Sensor, Floats]] = []
        for name, zs in by_sensor.items():
            s = self.sensors[name]
            used = self._associate(s, zs, t, leftovers_bearing)
            # radar: непривʼязані детекції стають кандидатами на нові треки
            if s.kind == "radar":
                for i, z in enumerate(zs):
                    if i not in used:
                        if len(self.tracks) >= self.max_tracks:
                            self.dropped["tracks_capped"] += 1
                            continue
                        tr = self._init_from_radar(s, z, t)
                        if not self._near_existing(tr.x[:3]):
                            tr.update(s, z, t)
                            self.tracks.append(tr)

        self._triangulate(leftovers_bearing, t)
        self._lifecycle(t)

    def _associate(self, s: Sensor, zs: list[Floats], t: float, leftovers_bearing: list[tuple[Sensor, Floats]]) -> set[int]:
        used = set()
        # трекам без оновлення в цьому скані; один датчик дає не більше одного оновлення на трек
        tracks = self.tracks
        if tracks:
            thr = chi2.ppf(GATE_P, s.dim)
            cost = np.full((len(tracks), len(zs)), BIG)
            Z = np.array(zs)
            for i, tr in enumerate(tracks):
                d2, c = tr.gate_costs(s, Z)
                inside = d2 <= thr
                cost[i, inside] = c[inside]
            rows, cols = linear_sum_assignment(cost)
            for i, j in zip(rows, cols):
                if cost[i, j] < BIG:
                    self._learn_bias(s, tracks[i], zs[j])
                    tracks[i].update(s, zs[j], t)
                    used.add(j)
        if s.kind == "bearing":
            leftovers_bearing.extend((s, z) for j, z in enumerate(zs) if j not in used)
        return used

    def _learn_bias(self, s: Sensor, tr: Track, z: Floats) -> None:
        """Відносний bias пеленгових датчиків: їхні нев'язки відносно підтвердженого треку, який щойно тримав радар.
        Абсолютний bias самого радара так не оцінити (треки тримаються за ним), для цього потрібен еталон."""
        if not self.online_bias or s.kind == "radar" or tr.state != "CONFIRMED" or sum(list(tr.radar_scans)[-2:]) == 0:
            return
        y = s.residual(z, s.h(tr.x))
        s.bias_est = s.bias_est + BIAS_RATE * np.clip(y, -3 * s.sigma, 3 * s.sigma)

    def _triangulate(self, leftovers: list[tuple[Sensor, Floats]], t: float) -> None:
        per_sensor: defaultdict[str, int] = defaultdict(int)
        kept: list[tuple[Sensor, Floats]] = []
        for s, z in leftovers:  # стеля кандидатів: за нормальної роботи їх одиниці, тож це лише захист від потопу
            if per_sensor[s.name] < MAX_BEARING_CANDIDATES:
                per_sensor[s.name] += 1
                kept.append((s, z))
            else:
                self.dropped["bearings_truncated"] += 1
        leftovers = kept
        for a in range(len(leftovers)):
            for b in range(a + 1, len(leftovers)):
                (s1, z1), (s2, z2) = leftovers[a], leftovers[b]
                if s1.name == s2.name:
                    continue
                if len(self.tracks) >= self.max_tracks:
                    self.dropped["tracks_capped"] += 1
                    return
                tr = self._init_from_bearings(s1, z1, s2, z2, t)
                if tr is None or self._near_existing(tr.x[:3]):
                    continue
                tr.update(s1, z1, t)
                tr.update(s2, z2, t)
                self.tracks.append(tr)

    def _lifecycle(self, t: float) -> None:
        alive = []
        for tr in self.tracks:
            tr.hist.append(min(3, tr.hit_w))
            tr.radar_scans.append(1 if tr.radar_hit else 0)
            cover_radars = self._cover_radars(tr)
            cover = bool(cover_radars)
            if tr.radar_hit or not cover:
                tr.radar_miss = 0
            elif self.heard is None or any(r.name in self.heard for r in cover_radars):
                tr.radar_miss += 1  # радар на звʼязку й цілі не бачить
            # інакше радар цілком мовчав у цьому скані: лічильник заморожено, трек не вбиваємо
            if tr.radar_miss >= RADAR_MISS_LIMIT:
                continue  # у зоні радара, а той цілі не бачить: це ghost
            tr.misses = 0 if tr.updated else tr.misses + 1
            if tr.state == "TENTATIVE":
                if sum(tr.hist) >= CONFIRM_W and (not cover or sum(tr.radar_scans) >= 1):
                    tr.state, tr.confirmed_at = "CONFIRMED", t
                elif len(tr.hist) == N_WINDOW:
                    continue  # не підтвердився: видаляємо
            elif tr.misses >= MAX_MISSES:
                continue
            alive.append(tr)
        # злиття дублікатів: лишаємо старіший трек
        alive.sort(key=lambda x: x.born)
        merged: list[Track] = []
        for tr in alive:
            if any(np.linalg.norm(tr.x[:3] - m.x[:3]) < MERGE_DIST / 2 and tr.state == m.state for m in merged):
                continue
            merged.append(tr)
        self.tracks = merged

    def confirmed(self) -> list[Track]:
        return [tr for tr in self.tracks if tr.state == "CONFIRMED"]
