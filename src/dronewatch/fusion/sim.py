"""Симулятор: траєкторії цілей, шум датчиків, пропуски, хибні детекції, bias і затримки."""
from collections.abc import Sequence
from typing import Any

import numpy as np

from dronewatch.fusion.models import Floats, Sensor

Waypoints = Sequence[Sequence[float | np.floating[Any]]]   # (час, x, y, z)
Step = tuple[float, list[tuple[str, Floats]], dict[str, Floats]]   # (час, детекції (датчик, вимірювання), істинні позиції)


def default_sensors(realistic: bool = False) -> list[Sensor]:
    """realistic=True додає те, чого не вистачало ідеальному сценарію:
    систематичну похибку (bias, її трекер не знає) і затримку датчиків (час мітки не збігається з часом вимірювання)."""
    sensors = [
        Sensor("radar1", "radar", (0, 0, 0), (5.0, np.radians(0.7), np.radians(1.5)), 0.90, 1500, 0.5, "radar"),
        Sensor("rf1", "bearing", (400, -300, 0), (np.radians(5),), 0.70, 1800, 0.3, "rf"),
        Sensor("ac1", "bearing", (-300, 200, 0), (np.radians(10),), 0.60, 700, 0.3, "acoustic"),
    ]
    if realistic:
        sensors[0].bias = np.array([8.0, np.radians(1.0), np.radians(0.5)])  # радар трохи «довертає» і мірить далі
        sensors[0].latency = 0.1
        sensors[1].bias = np.array([np.radians(3.0)])
        sensors[1].latency = 0.3
        sensors[2].bias = np.array([np.radians(-4.0)])
        sensors[2].latency = 0.5  # акустика: час проходження звуку + обробка
    sensors[0].type_id = "radar-mid"
    return sensors


# Палітра датчиків для розстановки на карті. sigma: радар (м, градуси азимут, градуси елевація) або (градуси,) для пеленгових.
SENSOR_TYPES: dict[str, dict[str, Any]] = {
    "radar-long": {"name": "Радар дальній", "kind": "radar", "label": "radar", "max_range": 3000.0,
                   "sigma": (10.0, 1.2, 2.5), "pd": 0.85, "clutter": 0.7},
    "radar-mid": {"name": "Радар середній", "kind": "radar", "label": "radar", "max_range": 1500.0,
                  "sigma": (5.0, 0.7, 1.5), "pd": 0.90, "clutter": 0.5},
    "radar-short": {"name": "Радар ближній (точний)", "kind": "radar", "label": "radar", "max_range": 800.0,
                    "sigma": (2.0, 0.4, 0.8), "pd": 0.95, "clutter": 0.3},
    # Узагальнений секторний радар (не конкретна модель): параметри як у середнього, але дивиться лише в сектор 120°
    "radar-sector": {"name": "Радар секторний 120°", "kind": "radar", "label": "radar", "max_range": 1500.0,
                     "sigma": (5.0, 0.7, 1.5), "pd": 0.90, "clutter": 0.5, "fov_deg": 120.0},
    "rf": {"name": "RF-детектор", "kind": "bearing", "label": "rf", "max_range": 1800.0, "sigma": (5.0,), "pd": 0.70, "clutter": 0.3},
    "acoustic": {"name": "Акустичний", "kind": "bearing", "label": "acoustic", "max_range": 700.0, "sigma": (10.0,), "pd": 0.60,
                 "clutter": 0.3},
}


def make_sensor(type_id: str, name: str, x: float, y: float, heading_deg: float = 0.0) -> Sensor:
    """Датчик із палітри в точці (x, y) метрів. Ідеальний: без зсуву й затримки (їх задають лише вбудовані датчики демо).
    heading_deg: куди він дивиться, компасні градуси (північ = 0, схід = 90); має значення лише для секторних типів."""
    p = SENSOR_TYPES[type_id]
    sigma = (p["sigma"][0], *np.radians(p["sigma"][1:])) if p["kind"] == "radar" else (np.radians(p["sigma"][0]),)
    s = Sensor(name, p["kind"], (x, y, 0.0), sigma, p["pd"], p["max_range"], p["clutter"], p["label"])
    s.type_id = type_id
    s.fov = np.radians(p.get("fov_deg", 360.0))
    s.heading = np.radians(90.0 - heading_deg)   # компас -> математичний кут
    return s


class Target:
    """Рух по вершинах [(t, x, y, z), ...] з лінійною інтерполяцією: різкі повороти = маневри."""

    def __init__(self, name: str, waypoints: Waypoints, model: str | None = None) -> None:
        self.name = name
        self.model = model   # ключ моделі з service/catalog.py, яку «розпізнає» RF-датчик; None = без ідентифікації
        self.wp = np.array(waypoints, float)

    def active(self, t: float) -> bool:
        return self.wp[0, 0] <= t <= self.wp[-1, 0]

    def pos(self, t: float) -> Floats:
        return np.array([np.interp(t, self.wp[:, 0], self.wp[:, k]) for k in (1, 2, 3)])


def default_targets() -> list[Target]:
    return [
        # A: пролітає через зону з поворотом
        Target("A", [(0, -900, 500, 80), (35, -300, 150, 80), (50, 100, 100, 90), (90, 700, -200, 90)]),
        # B: підлітає, зависає, потім різко йде геть
        Target("B", [(10, 600, 700, 60), (35, 250, 250, 60), (60, 250, 250, 60), (80, 800, 600, 120)]),
        # C: низький і повільний, лише з середини сценарію
        Target("C", [(40, -500, -400, 30), (90, -50, -100, 30)]),
    ]


# Склад роя для демо: суміш ворожих, своїх і цивільних (ключі з service/catalog.py)
SWARM_MODELS = ["geran-2", "orlan-10", "leleka-100", "lancet-3", "anafi-usa", "dji-mini-4", "furia", "zala-421"]


def swarm_targets(n: int = 8, seed: int = 7, models: bool = False, dive: bool = False) -> list[Target]:
    """Рій: n дронів входять із різних боків колами радіусом ~800 м, перетинають центр і вилітають.
    Шляхи перетинаються, тож у тріангуляції пеленгів неминуче з'являються «примарні» перетини.
    models: призначити кожному модель; dive: перший дрон наприкінці йде в пікірування до землі (кінцева фаза атаки)."""
    rng = np.random.default_rng(seed)
    targets = []
    for k in range(n):
        ang = 2 * np.pi * k / n + rng.uniform(-0.2, 0.2)
        start = 800 * np.array([np.cos(ang), np.sin(ang)])
        aim = rng.uniform(-150, 150, 2)           # точка, повз яку летить
        end = aim + (aim - start) * rng.uniform(0.8, 1.1)
        alt = rng.uniform(40, 120)
        t0 = 2.0 * k
        speed = rng.uniform(14, 20)
        t1 = t0 + np.linalg.norm(aim - start) / speed
        t2 = t1 + np.linalg.norm(end - aim) / speed
        end_alt = alt + rng.uniform(-20, 20)   # викликається завжди, щоб послідовність ГВЧ (а отже й інші цілі) не змінювалась
        wp = [(t0, *start, alt), (t1, *aim, alt), (t2, *end, end_alt)]
        if dive and k == 0:   # за 75% другого плеча пікірує до землі
            td = t1 + 0.75 * (t2 - t1)
            wp = [(t0, *start, alt), (t1, *aim, alt), (td, *(aim + 0.75 * (end - aim)), alt), (t2, *end, 0.0)]
        targets.append(Target(f"S{k}", wp, SWARM_MODELS[k % len(SWARM_MODELS)] if models else None))
    return targets


def simulate(sensors: list[Sensor], targets: list[Target], duration: float = 90.0, dt: float = 0.5, seed: int = 1) -> list[Step]:
    """Повертає список (t, [(sensor_name, z), ...], {target: pos}) у порядку часу."""
    rng = np.random.default_rng(seed)
    out = []
    for t in np.arange(0, duration + 1e-9, dt):
        truth = {tg.name: tg.pos(t) for tg in targets if tg.active(t)}
        dets = []
        for s in sensors:
            for tg in targets:
                tm = t - s.latency  # вимірювання зроблене раніше, але мітка часу = t
                if not tg.active(tm):
                    continue
                pos = tg.pos(tm)
                if np.linalg.norm(pos - s.pos) > s.max_range or not s.in_fov(pos) or rng.random() > s.pd:
                    continue
                z = s.h(np.concatenate([pos, np.zeros(3)])) + s.bias + rng.normal(0, s.sigma)
                dets.append((s.name, z))
            for _ in range(rng.poisson(s.clutter_rate)):  # хибні детекції (птахи, відбиття, вітер)
                az = rng.uniform(-np.pi, np.pi) if s.fov >= 2 * np.pi - 1e-9 else rng.uniform(s.heading - s.fov / 2, s.heading + s.fov / 2)
                z = np.array([rng.uniform(50, s.max_range), az, rng.uniform(0, 0.5)]) if s.kind == "radar" else np.array([az])
                dets.append((s.name, z))
        rng.shuffle(dets)
        out.append((t, dets, truth))
    return out


def reference_flight_calibration(sensors: list[Sensor], seed: int = 999, radius: float = 300.0, speed: float = 15.0) -> None:
    """Калібрування еталонним дроном: він летить колом навколо об'єкта, його позиція відома з GNSS (Remote ID).
    Медіана нев'язок «вимірювання мінус очікуване з істинної позиції» дає bias кожного датчика, радара теж.
    Політ колом усереднює вплив затримки: вона зсуває вимірювання то в один бік, то в протилежний."""
    n = 24
    period = 2 * np.pi * radius / speed
    wp = [(period * k / n, radius * np.cos(2 * np.pi * k / n), radius * np.sin(2 * np.pi * k / n), 70.0) for k in range(n + 1)]
    ref = Target("REF", wp)
    saved = {s.name: s.clutter_rate for s in sensors}
    for s in sensors:
        s.clutter_rate = 0.0  # на калібруванні ефір чистий
    data = simulate(sensors, [ref], duration=period, seed=seed)
    for s in sensors:
        s.clutter_rate = saved[s.name]
        res = []
        for _, dets, truth in data:
            if "REF" not in truth:
                continue
            expected = s.h(np.concatenate([truth["REF"], np.zeros(3)]))
            for name, z in dets:
                if name == s.name:
                    res.append(s.residual(z, expected))
        if res:
            s.bias_est = np.median(np.array(res), axis=0)
