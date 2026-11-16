"""Карта зверху: істинні траєкторії, confirmed-треки (живі та coasting), датчики."""
import argparse
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from run import SCENARIOS
from sim import default_sensors, simulate
from tracker import Tracker

MATCH_GATE = 60.0


def collect(seed, scenario):
    cfg = SCENARIOS[scenario]
    duration = cfg["dur"]
    sensors, targets = default_sensors(cfg["realistic"]), cfg["targets"]()
    tracker = Tracker(sensors)
    truth_path = {tg.name: [] for tg in targets}
    pts = {"ok": [], "live_false": [], "coast_false": []}
    for t, dets, truth in simulate(sensors, targets, duration=duration, seed=seed):
        tracker.step(t, dets)
        for n, p in truth.items():
            truth_path[n].append(p[:2])
        for tr in tracker.confirmed():
            near = min((np.linalg.norm(tr.x[:3] - p) for p in truth.values()), default=1e9)
            key = "ok" if near <= MATCH_GATE else ("coast_false" if tr.misses > 0 else "live_false")
            pts[key].append((tr.x[0], tr.x[1], tr.id))
    return sensors, truth_path, pts


def draw(seed, out, scenario):
    sensors, truth_path, pts = collect(seed, scenario)
    fig, ax = plt.subplots(figsize=(10, 8))
    for n, path in truth_path.items():
        p = np.array(path)
        ax.plot(p[:, 0], p[:, 1], "-", lw=3, alpha=0.35, color="black")
        ax.annotate(n, p[0], fontsize=9, weight="bold")
    style = {"ok": ("#2a9d8f", 6, "трек збігається з ціллю"),
             "live_false": ("#e63946", 14, "хибний (живий)"),
             "coast_false": ("#f4a261", 14, "хибний (coasting)")}
    for key, (c, s, lab) in style.items():
        if pts[key]:
            a = np.array(pts[key])
            ax.scatter(a[:, 0], a[:, 1], s=s, c=c, label=lab, zorder=3 if key != "ok" else 2)
    for s in sensors:
        ax.plot(*s.pos[:2], "^", ms=12, color="#264653")
        ax.annotate(s.name, s.pos[:2] + 15, fontsize=9)
    ax.set_aspect("equal")
    ax.set_xlabel("East, м")
    ax.set_ylabel("North, м")
    ax.set_title(f"{scenario}: істина (чорна лінія) і треки, seed={seed}")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=110)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default="tracks.png")
    ap.add_argument("--scenario", default="рій, bias+затримки (без захисту)")
    a = ap.parse_args()
    draw(a.seed, a.out, a.scenario)
    print("збережено", a.out)
