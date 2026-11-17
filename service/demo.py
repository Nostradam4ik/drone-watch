"""Демо-джерело: симулятор датчиків, що шле події в Engine тим самим шляхом, що й реальні адаптери."""
import math
import time
from typing import Any

from engine import math_to_compass
from models import Floats, Sensor
from sim import default_sensors, reference_flight_calibration, simulate, swarm_targets


def make_sensors() -> list[Sensor]:
    """Датчики з реалістичними спотвореннями (bias, затримки) + калібрування еталонним польотом."""
    sensors = default_sensors(realistic=True)
    for s in sensors:
        s.latency_comp = s.latency          # затримку виміряно PTP/NTP
    reference_flight_calibration(sensors)   # bias з еталонного польоту дрона з GNSS
    return sensors


def to_event(sensor: Sensor, z: Floats, ts: float) -> dict[str, Any]:
    """Внутрішнє вимірювання -> публічний формат події (компасні градуси)."""
    if sensor.kind == "radar":
        return {"sensor_id": sensor.name, "ts": ts, "range_m": float(z[0]),
                "azimuth_deg": math_to_compass(z[1]), "elevation_deg": math.degrees(z[2])}
    return {"sensor_id": sensor.name, "ts": ts, "azimuth_deg": math_to_compass(z[0])}


class DemoSource:
    def __init__(self, sensors: list[Sensor], seed: int = 1, duration: float = 110.0, dt: float = 0.5) -> None:
        self.sensors = {s.name: s for s in sensors}
        self.seed, self.duration, self.dt = seed, duration, dt
        self.restart()

    def restart(self) -> None:
        self.targets = swarm_targets()
        self.steps = simulate(list(self.sensors.values()), self.targets, duration=self.duration, dt=self.dt, seed=self.seed)
        self.i = 0
        self.started = time.time()
        self.seed += 1  # наступний цикл буде з іншим шумом
        self.finished_at: float | None = None

    def poll(self) -> tuple[list[dict[str, Any]], dict[str, Floats], bool]:
        """Повертає (події, істина) для всіх кроків симуляції, час яких уже настав; None коли цикл завершено."""
        elapsed = time.time() - self.started
        events: list[dict[str, Any]] = []
        truth: dict[str, Floats] = {}
        while self.i < len(self.steps) and self.steps[self.i][0] <= elapsed:
            _, dets, truth = self.steps[self.i]
            events += [to_event(self.sensors[n], z, time.time()) for n, z in dets]
            events += [{"sensor_id": n, "heartbeat": True} for n in self.sensors]  # реальні датчики теж так звітують
            self.i += 1
        if self.i >= len(self.steps):
            if self.finished_at is None:
                self.finished_at = time.time()
            return events, truth, time.time() - self.finished_at > 4.0
        return events, truth, False
