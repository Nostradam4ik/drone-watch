#!/usr/bin/env python3
"""Заміряє найгірший скан під потопом подій: чи вкладається він у scan_dt на ЦЬОМУ залізі.

Потоп = випадкові детекції від усіх датчиків (збій датчика або навмисна атака), у кілька разів більше за стелю.
Саме такий випадок визначає, які max_events_per_sensor_scan і max_tracks можна безпечно ставити в конфіг.

Потрібен PYTHONPATH із fusion_proto і service (бібліотеки не пакет): PYTHONPATH=fusion_proto:service (у PowerShell розділювач ;).

Приклади (з кореня репозиторію):
    python service/flood_bench.py                           # стелі за замовчуванням, датчики з sim
    python service/flood_bench.py --config deploy/config.yaml # стелі, датчики й scan_dt з бойового конфігу (потрібні pyyaml, pydantic)
    python service/flood_bench.py --max-events 300 --max-tracks 500
    python service/flood_bench.py --sweep                  # таблиця: скан залежно від стель

Код виходу 1, якщо найгірший скан довший за --budget (за замовчуванням scan_dt): зручно для перевірки на цільовому залізі.
"""
import argparse
import io
import random
import sys
import time
from typing import TYPE_CHECKING, Any

from engine import Engine, Zone
from models import Sensor
from sim import default_sensors

if TYPE_CHECKING:   # лише для типів: сам config (pyyaml, pydantic) бенчмарк без --config не вантажить
    import config

if isinstance(sys.stdout, io.TextIOWrapper):   # у тестах (pytest) stdout підмінено і reconfigure немає
    sys.stdout.reconfigure(encoding="utf-8")

WARMUP = 4   # перші скани дешевші: хибні треки ще накопичуються до стелі, міряємо сталий режим
SWEEP = [(50, 60), (100, 120), (150, 200), (200, 300), (300, 500)]   # (подій на датчик, треків)


def flood_events(rng: random.Random, sensors: list[Sensor], per_sensor: int) -> list[dict[str, Any]]:
    events = []
    for s in sensors:
        for _ in range(per_sensor):
            e = {"sensor_id": s.name, "ts": time.time(), "azimuth_deg": rng.uniform(0, 360)}
            if s.kind == "radar":
                e.update(range_m=rng.uniform(50, max(60.0, s.max_range * 0.95)), elevation_deg=rng.uniform(0, 30))
            events.append(e)
    return events


def run(sensors: list[Sensor], max_events: int, max_tracks: int, flood: int, scans: int, verbose: bool = False,
        seed: int = 1) -> tuple[float, float, dict[str, int]]:
    """Повертає (середній скан сталого режиму, максимальний скан, stats рушія)."""
    rng = random.Random(seed)
    eng = Engine(sensors, [Zone("z", 600, "WARNING")], max_events_per_sensor=max_events, max_tracks=max_tracks)
    times = []
    for i in range(scans):
        eng.ingest(flood_events(rng, sensors, flood))
        t0 = time.perf_counter()
        eng.tick()
        times.append(time.perf_counter() - t0)
        if verbose:
            print(f"  скан {i + 1:>2}: {times[-1] * 1000:6.0f} мс   треків {len(eng.tracker.tracks):>4}")
    steady = times[WARMUP:] or times
    return sum(steady) / len(steady), max(steady), eng.stats


def load_config(path: str) -> "tuple[config.Config, list[Sensor]]":
    import config  # лише тут: рушію й бенчмарку без --config pyyaml/pydantic не потрібні
    cfg = config.load(path)
    sensors = config.build_sensors(cfg) if cfg.sensors else default_sensors()
    return cfg, sensors


def main() -> int:
    ap = argparse.ArgumentParser(description="Скан під потопом подій", epilog=__doc__.split("Приклади")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="бойовий YAML: стелі, датчики й scan_dt беруться звідти")
    ap.add_argument("--max-events", type=int, help="стеля подій від датчика за скан (перекриває конфіг)")
    ap.add_argument("--max-tracks", type=int, help="стеля треків (перекриває конфіг)")
    ap.add_argument("--flood", type=int, help="подій від кожного датчика за скан (за замовчуванням втричі більше за стелю)")
    ap.add_argument("--scans", type=int, default=10, help="скільки сканів прогнати")
    ap.add_argument("--budget", type=float, help="ліміт на скан, с (за замовчуванням scan_dt з конфігу, інакше 0.5)")
    ap.add_argument("--sweep", action="store_true", help="таблиця для кількох пар стель замість одного прогону")
    a = ap.parse_args()

    scan_dt, max_events, max_tracks, sensors = 0.5, 150, 200, default_sensors()
    if a.config:
        cfg, sensors = load_config(a.config)
        scan_dt, max_events, max_tracks = cfg.scan_dt, cfg.max_events_per_sensor_scan, cfg.max_tracks
    max_events, max_tracks = a.max_events or max_events, a.max_tracks or max_tracks
    budget = a.budget or scan_dt
    print(f"датчиків: {len(sensors)} ({', '.join(s.name for s in sensors)}), бюджет скану {budget * 1000:.0f} мс")

    if a.sweep:
        print(f"\n{'подій/датчик':>13}{'треків':>8}{'скан середній':>15}{'скан макс':>11}")
        for ev, tr in SWEEP:
            avg, mx, _ = run(sensors, ev, tr, a.flood or ev * 3, max(a.scans, WARMUP + 3))
            print(f"{ev:>13}{tr:>8}{avg:>14.2f}с{mx:>10.2f}с{'   перевищує бюджет' if mx > budget else ''}", flush=True)
        return 0

    flood = a.flood or max_events * 3
    print(f"стелі: {max_events} подій/датчик, {max_tracks} треків; потоп: {flood} подій від кожного датчика за скан\n")
    avg, mx, st = run(sensors, max_events, max_tracks, flood, max(a.scans, WARMUP + 1), verbose=True)
    print(f"\nсталий режим: середній {avg * 1000:.0f} мс, максимум {mx * 1000:.0f} мс "
          f"(відкинуто подій {st['events_dropped']}, треків {st['tracks_capped']}, пеленгів {st['bearings_truncated']})")
    if mx > budget:
        print(f"НЕ вкладається в бюджет {budget * 1000:.0f} мс: зменште стелі або прискорте залізо")
        return 1
    print(f"вкладається в бюджет {budget * 1000:.0f} мс із запасом ×{budget / mx:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
