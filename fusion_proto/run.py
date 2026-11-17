"""Запуск сценаріїв і підрахунок метрик."""
import argparse
import sys
import time

import numpy as np
from scipy.optimize import linear_sum_assignment
from sim import default_sensors, default_targets, reference_flight_calibration, simulate, swarm_targets
from tracker import Tracker

sys.stdout.reconfigure(encoding="utf-8")

MATCH_GATE = 60.0  # м: трек вважається привʼязаним до цілі, якщо ближче за це

# опції: lat = трекер компенсує відому затримку, online = онлайн-оцінка bias пеленгових датчиків,
# ref = калібрування еталонним польотом (дає і bias радара)
SCENARIOS = {
    "базовий, ідеальні датчики": dict(realistic=False, targets=default_targets, dur=90.0),
    "базовий, bias+затримки": dict(realistic=True, targets=default_targets, dur=90.0),
    "рій, ідеальні датчики": dict(realistic=False, targets=swarm_targets, dur=110.0),
    "рій, bias+затримки (без захисту)": dict(realistic=True, targets=swarm_targets, dur=110.0),
    "рій, + компенсація затримок": dict(realistic=True, targets=swarm_targets, dur=110.0, lat=True),
    "рій, + онлайн-bias": dict(realistic=True, targets=swarm_targets, dur=110.0, lat=True, online=True),
    "рій, + еталонний політ": dict(realistic=True, targets=swarm_targets, dur=110.0, lat=True, online=True, ref=True),
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
    first_conf, last_track, switches = {}, {}, 0
    errs, false_live, false_coast, missed_steps, target_steps = [], 0, 0, 0, 0

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
    header = f"{'сценарій':<36}{'RMSE,м':>8}{'підтв.,с':>10}{'пропуск,%':>11}{'хибні жив.':>12}{'хибні coast':>13}{'ID sw.':>8}{'не знайд.':>11}"
    print(header)
    print("-" * len(header))
    for name in SCENARIOS:
        t0 = time.time()
        res = [evaluate(name, s, verbose=a.v and s == 1) for s in range(1, a.seeds + 1)]
        m = lambda k: np.mean([r[k] for r in res])
        print(f"{name:<36}{m('rmse'):>8.1f}{m('ttc'):>10.1f}{m('missed_pct'):>11.1f}{m('false_live'):>12.1f}"
              f"{m('false_coast'):>13.1f}{m('switches'):>8.2f}{m('never'):>11.2f}   ({time.time() - t0:.0f}с)")
