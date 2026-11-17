"""Симулятор: траєкторії цілей, шум датчиків, пропуски, хибні детекції, bias і затримки."""
from collections.abc import Sequence
from typing import Any

import numpy as np

from models import Floats, Sensor

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
    return sensors


class Target:
    """Рух по вершинах [(t, x, y, z), ...] з лінійною інтерполяцією: різкі повороти = маневри."""

    def __init__(self, name: str, waypoints: Waypoints) -> None:
        self.name = name
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


def swarm_targets(n: int = 8, seed: int = 7) -> list[Target]:
    """Рій: n дронів входять із різних боків колами радіусом ~800 м, перетинають центр і вилітають.
    Шляхи перетинаються, тож у тріангуляції пеленгів неминуче з'являються «примарні» перетини."""
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
        targets.append(Target(f"S{k}", [(t0, *start, alt), (t1, *aim, alt), (t2, *end, alt + rng.uniform(-20, 20))]))
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
                if np.linalg.norm(pos - s.pos) > s.max_range or rng.random() > s.pd:
                    continue
                z = s.h(np.concatenate([pos, np.zeros(3)])) + s.bias + rng.normal(0, s.sigma)
                dets.append((s.name, z))
            for _ in range(rng.poisson(s.clutter_rate)):  # хибні детекції (птахи, відбиття, вітер)
                az = rng.uniform(-np.pi, np.pi)
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
