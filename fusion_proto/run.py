"""Запуск сценаріїв і підрахунок метрик."""
import argparse
import io
import sys
import time
from typing import Any

import numpy as np
from scipy.optimize import linear_sum_assignment

from sim import default_sensors, default_targets, reference_flight_calibration, simulate, swarm_targets
from tracker import Tracker

if isinstance(sys.stdout, io.TextIOWrapper):   # у тестах (pytest) stdout підмінено і reconfigure немає
    sys.stdout.reconfigure(encoding="utf-8")

MATCH_GATE = 60.0  # м: трек вважається привʼязаним до цілі, якщо ближче за це

# опції: lat = трекер компенсує відому затримку, online = онлайн-оцінка bias пеленгових датчиків,
# ref = калібрування еталонним польотом (дає і bias радара)
SCENARIOS: dict[str, dict[str, Any]] = {
    "базовий, ідеальні датчики": {"realistic": False, "targets": default_targets, "dur": 90.0},
    "базовий, bias+затримки": {"realistic": True, "targets": default_targets, "dur": 90.0},
    "рій, ідеальні датчики": {"realistic": False, "targets": swarm_targets, "dur": 110.0},
    "рій, bias+затримки (без захисту)": {"realistic": True, "targets": swarm_targets, "dur": 110.0},
    "рій, + компенсація затримок": {"realistic": True, "targets": swarm_targets, "dur": 110.0, "lat": True},
    "рій, + онлайн-bias": {"realistic": True, "targets": swarm_targets, "dur": 110.0, "lat": True, "online": True},
    "рій, + еталонний політ": {"realistic": True, "targets": swarm_targets, "dur": 110.0, "lat": True, "online": True, "ref": True},
}


def evaluate(scenario, seed, verbose=False):
    cfg = SCENARIOS[scenario]
    duration = cfg["dur"]
    sensors, targets = default_sensors(cfg["realistic"]), cfg["targets"]()
    if cfg.get("lat"):
        for s in sensors:
            s.latency_comp = s.latency
    if cfg.get("ref"):
        reference_flight_calibration(sensors, seed=1000 + seed)
    tracker = Tracker(sensors, online_bias=cfg.get("online", False))
    appear = {tg.name: tg.wp[0, 0] for tg in targets}
    first_conf: dict[str, float] = {}
    last_track: dict[str, int] = {}
    switches = 0
    errs: list[float] = []
    false_live, false_coast, missed_steps, target_steps = 0, 0, 0, 0

    for t, dets, truth in simulate(sensors, targets, duration=duration, seed=seed):
        tracker.step(t, dets)
        conf = tracker.confirmed()
        names = list(truth)
        target_steps += len(names)
        matched_tracks = set()
        if conf and names:
            cost = np.array([[np.linalg.norm(tr.x[:3] - truth[n]) for n in names] for tr in conf])
            rows, cols = linear_sum_assignment(cost)
            for i, j in zip(rows, cols):
                if cost[i, j] <= MATCH_GATE:
                    tr, n = conf[i], names[j]
                    matched_tracks.add(i)
                    errs.append(cost[i, j])
                    first_conf.setdefault(n, t)
                    if n in last_track and last_track[n] != tr.id:
                        switches += 1
                    last_track[n] = tr.id
        missed_steps += len(names) - len(matched_tracks)
        for i, tr in enumerate(conf):
            if i not in matched_tracks:
                if tr.misses > 0:
                    false_coast += 1
                else:
                    false_live += 1
        if verbose and int(t * 2) % 20 == 0:
            print(f"t={t:5.1f} цілей={len(names)} confirmed={len(conf)} tentative={len(tracker.tracks) - len(conf)}")

    return {
        "rmse": float(np.sqrt(np.mean(np.square(errs)))) if errs else float("nan"),
        "ttc": float(np.nanmean([first_conf.get(n, float("nan")) - appear[n] for n in appear])),
        "never": sum(1 for n in appear if n not in first_conf),
        "switches": switches,
        "false_live": false_live,
        "false_coast": false_coast,
        "missed_pct": 100 * missed_steps / target_steps,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()

    print(f"Усереднено по {a.seeds} запусках з різним шумом і clutter\n")
    header = (f"{'сценарій':<36}{'RMSE,м':>8}{'підтв.,с':>10}{'пропуск,%':>11}{'хибні жив.':>12}"
              f"{'хибні coast':>13}{'ID sw.':>8}{'не знайд.':>11}")
    print(header)
    print("-" * len(header))
    for name in SCENARIOS:
        t0 = time.time()
        res = [evaluate(name, s, verbose=a.v and s == 1) for s in range(1, a.seeds + 1)]
        mean = {k: float(np.mean([r[k] for r in res])) for k in res[0]}
        print(f"{name:<36}{mean['rmse']:>8.1f}{mean['ttc']:>10.1f}{mean['missed_pct']:>11.1f}{mean['false_live']:>12.1f}"
              f"{mean['false_coast']:>13.1f}{mean['switches']:>8.2f}{mean['never']:>11.2f}   ({time.time() - t0:.0f}с)")
