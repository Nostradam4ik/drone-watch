#!/usr/bin/env python3
"""Оцінка якості трекінгу на записі подій датчиків: OSPA, RMSE, частка хибних треків, пропуски, перемикання ID.

Запис це JSONL: кожен рядок одна подія у тому ж вигляді, що `POST /api/events` (sensor_id, ts, azimuth_deg, range_m,
elevation_deg, за потреби drone_model чи heartbeat). Події подаються в той самий `Engine`, що й у сервісі (керований годинник,
без мережі й потоків), тож міряється справжній код, а не окрема копія трекера.

Еталон (де цілі були насправді) необов'язковий, теж JSONL: {"t": ts, "id": "gps1", "x": м, "y": м, "z": м} у системі
координат датчиків (схід, північ, вгору від точки (0, 0)); z можна опустити. Без нього рахується лише те, що від еталону не
залежить (скільки треків, як довго жили, скільки «мигаючих»). OSPA без еталону не буває, і звіт це прямо каже.

    dronewatch-eval simulate --out rec/ --scenario swarm --seed 1          # згенерувати запис із симулятора (еталон відомий)
    dronewatch-eval run --events rec/events.jsonl --truth rec/truth.jsonl --sim-sensors ideal
    dronewatch-eval run --events flight.jsonl --truth gps.jsonl --config deploy/config.yaml --json report.json

Запис із симулятора потрібен для перевірки самих метрик і для порівняння версій трекера; це НЕ оцінка на справжніх датчиках."""
import argparse
import json
import math
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np

from dronewatch.fusion.metrics import Scorer, TruthSet
from dronewatch.fusion.models import Sensor
from dronewatch.fusion.sim import default_sensors, default_targets, simulate, swarm_targets
from dronewatch.service import config
from dronewatch.service.engine import Engine, math_to_compass

BASE_TS = 1_760_000_000.0   # початок епохи для записів із симулятора


class EvalError(Exception):
    """Помилка вхідних даних: повідомлення вказує файл і рядок."""


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    try:
        with path.open(encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except ValueError as e:
                    raise EvalError(f"{path}:{n}: не JSON ({e})") from None
                if not isinstance(obj, dict):
                    raise EvalError(f"{path}:{n}: очікується JSON-обʼєкт")
                yield n, obj
    except OSError as e:
        raise EvalError(f"{path}: {e.strerror or e}") from None


def _num(obj: dict[str, Any], key: str, where: str, *, default: float | None = None) -> float:
    v = obj.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
        raise EvalError(f"{where}: поле {key!r} має бути скінченним числом")
    return float(v)


def read_events(path: Path) -> list[dict[str, Any]]:
    events = []
    for n, e in _jsonl(path):
        _num(e, "ts", f"{path}:{n}")
        if not isinstance(e.get("sensor_id"), str):
            raise EvalError(f"{path}:{n}: потрібне рядкове поле 'sensor_id'")
        events.append(e)
    if not events:
        raise EvalError(f"{path}: у записі немає жодної події")
    events.sort(key=lambda e: e["ts"])
    return events


def read_truth(path: Path, offset_s: float = 0.0, max_gap_s: float = 5.0) -> TruthSet:
    rows = []
    for n, r in _jsonl(path):
        where = f"{path}:{n}"
        if not isinstance(r.get("id"), str):
            raise EvalError(f"{where}: потрібне рядкове поле 'id'")
        rows.append((_num(r, "t", where) + offset_s, r["id"], _num(r, "x", where), _num(r, "y", where), _num(r, "z", where, default=0.0)))
    if not rows:
        raise EvalError(f"{path}: еталон порожній")
    return TruthSet(rows, max_gap_s)


# ---------- запис із симулятора ----------
def simulate_recording(*, targets: str = "swarm", realistic: bool = False, duration: float = 110.0, dt: float = 0.5,
                       seed: int = 1) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Sensor]]:
    """(події, еталон, датчики) симулятора. Події такі, як їх слав би справжній пристрій, разом із heartbeat на кожен скан."""
    sensors = default_sensors(realistic)
    by_name = {s.name: s for s in sensors}
    tg = swarm_targets() if targets == "swarm" else default_targets()
    events: list[dict[str, Any]] = []
    truth: list[dict[str, Any]] = []
    for t, dets, tr in simulate(sensors, tg, duration=duration, dt=dt, seed=seed):
        ts = round(BASE_TS + float(t), 3)
        events += [{"sensor_id": s.name, "ts": ts, "heartbeat": True} for s in sensors]
        for name, z in dets:
            if by_name[name].kind == "radar":
                events.append({"sensor_id": name, "ts": ts, "range_m": round(float(z[0]), 2),
                               "azimuth_deg": round(math_to_compass(float(z[1])), 3), "elevation_deg": round(math.degrees(float(z[2])), 3)})
            else:
                events.append({"sensor_id": name, "ts": ts, "azimuth_deg": round(math_to_compass(float(z[0])), 3)})
        truth += [{"t": ts, "id": n, "x": round(float(p[0]), 2), "y": round(float(p[1]), 2), "z": round(float(p[2]), 2)}
                  for n, p in tr.items()]
    return events, truth, sensors


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")


# ---------- прогін ----------
def replay(engine: Engine, clock: _Clock, events: list[dict[str, Any]], truth: TruthSet | None, scorer: Scorer, *,
           scan_dt: float, synthetic_heartbeat: bool = False) -> dict[str, Any]:
    """Подає події в рушій по сканах (подія потрапляє в перший скан, чий час не раніший за її мітку) і після кожного скану
    віддає скорерові підтверджені треки (ті, що оператор бачить живими) та еталон на цей момент."""
    t0 = events[0]["ts"]
    buckets: dict[int, list[dict[str, Any]]] = {}
    for e in events:
        buckets.setdefault(max(0, math.ceil((e["ts"] - t0) / scan_dt - 1e-9)), []).append(e)
    last = max(buckets)
    seen = sorted({e["sensor_id"] for e in events if e["sensor_id"] in engine.sensors})
    for k in range(last + 1):
        t = t0 + k * scan_dt
        clock.now = t
        batch = buckets.get(k, [])
        if synthetic_heartbeat:
            batch = batch + [{"sensor_id": sid, "ts": t, "heartbeat": True} for sid in seen]
        engine.ingest(batch)
        engine.tick()
        live = {tr.id: np.array(tr.x[:3], float) for tr in engine.tracker.confirmed() if tr.misses <= engine.max_coast}
        scorer.step(t, live, truth.at(t) if truth else {}, truth_present=bool(truth))
    out = scorer.summary()
    out["events"] = {"in_file": len(events), "accepted": engine.stats["events_in"], "heartbeats": engine.stats["heartbeats"],
                     "rejected": engine.stats["events_rejected"], "dropped": engine.stats["events_dropped"]}
    out["duration_s"] = round(last * scan_dt, 1)
    return out


def engine_for(sensors: list[Sensor], clock: _Clock, *, scan_dt: float, cfg: config.Config | None = None) -> Engine:
    """Рушій для прогону: у памʼяті, без ключів датчиків (запис уже «прийнятий»), параметри трекінгу з конфігу, якщо він є."""
    if cfg is None:
        return Engine(sensors, [], db_path=":memory:", scan_dt=scan_dt, clock=clock)
    return Engine(sensors, config.build_zones(cfg), db_path=":memory:", scan_dt=scan_dt, min_age_s=cfg.alerts.min_age_s,
                  require_radar=cfg.alerts.require_radar_in_cover, max_coast=cfg.alerts.max_coast_scans,
                  sensor_timeout_s=cfg.sensor_timeout_s, max_events_per_sensor=cfg.max_events_per_sensor_scan,
                  max_tracks=cfg.max_tracks, origin=(cfg.origin.lat, cfg.origin.lon), clock=clock)


def evaluate_files(events: list[dict[str, Any]], truth: TruthSet | None, *, sensors: list[Sensor], cfg: config.Config | None = None,
                   scan_dt: float = 0.5, cutoff: float = 100.0, p: float = 2.0, gate: float = 60.0, dim: int = 2,
                   synthetic_heartbeat: bool = False) -> dict[str, Any]:
    clock = _Clock(events[0]["ts"])
    engine = engine_for(sensors, clock, scan_dt=scan_dt, cfg=cfg)
    scorer = Scorer(cutoff=cutoff, p=p, gate=gate, dim=dim)
    return replay(engine, clock, events, truth, scorer, scan_dt=scan_dt, synthetic_heartbeat=synthetic_heartbeat)


# ---------- звіт і CLI ----------
def format_report(r: dict[str, Any]) -> str:
    ev = r["events"]
    lines = [f"Запис: {ev['in_file']} рядків (прийнято подій {ev['accepted']}, heartbeat {ev['heartbeats']}), "
             f"{r['scans']} сканів, {r['duration_s']} с"]
    if ev["rejected"] or ev["dropped"]:
        lines.append(f"УВАГА: відхилено {ev['rejected']}, відкинуто стелею {ev['dropped']} подій (невідомий датчик, невалідні поля?)")
    lines.append(f"Треки: усього {r['tracks_total']}, разом максимум {r['tracks_peak']}, "
                 f"«мигаючих» (<{r['params']['flicker_s']:g} с) {r['flicker_tracks']}, середня тривалість {r['mean_track_life_s']:.1f} с")
    if not r["has_truth"]:
        lines.append("Еталону немає: OSPA, RMSE, частка хибних треків і пропуски не рахуються (потрібен --truth).")
        return "\n".join(lines)
    pr = r["params"]
    lines += [
        f"Еталон: {r['truth_targets']} цілей. Параметри: c={pr['cutoff_m']:g} м, p={pr['p']:g}, привʼязка ≤{pr['gate_m']:g} м, "
        f"{pr['dim']}D",
        f"  OSPA            {r['ospa_mean']:7.1f} м   (лише кадри з ціллю чи треком: {r['ospa_mean_busy']:.1f})",
        f"    положення     {r['ospa_loc']:7.1f} м",
        f"    кількість     {r['ospa_card']:7.1f} м",
        f"  RMSE            {r['rmse_m']:7.1f} м",
        f"  пропуски        {r['missed_pct']:7.1f} %",
        f"  хибні треки     {r['false_tracks']} з {r['tracks_total']} ({r['false_track_pct']:.1f} %); "
        f"кадрів хибних {r['false_track_scan_pct']:.1f} %",
        f"  перемикань ID   {r['id_switches']}",
        f"  час до підтвердження {r['time_to_confirm_s']:.1f} с, не знайдено цілей {r['never_found']}",
    ]
    return "\n".join(lines)


def _cmd_run(a: argparse.Namespace) -> int:
    events = read_events(Path(a.events))
    truth = read_truth(Path(a.truth), a.truth_offset, a.max_gap) if a.truth else None
    cfg = config.load(a.config) if a.config else None
    sensors = config.build_sensors(cfg) if cfg is not None else default_sensors(a.sim_sensors == "realistic")
    scan_dt = a.scan_dt or (cfg.scan_dt if cfg else 0.5)
    result = evaluate_files(events, truth, sensors=sensors, cfg=cfg, scan_dt=scan_dt, cutoff=a.cutoff, p=a.p, gate=a.gate,
                            dim=a.dim, synthetic_heartbeat=a.synthetic_heartbeat)
    print(format_report(result))
    if a.json:
        Path(a.json).write_text(json.dumps(result, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    return 0


def _cmd_simulate(a: argparse.Namespace) -> int:
    events, truth, _ = simulate_recording(targets=a.scenario, realistic=a.realistic, duration=a.duration, dt=a.scan_dt, seed=a.seed)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    write_jsonl(out / "events.jsonl", events)
    write_jsonl(out / "truth.jsonl", truth)
    sensors = "realistic" if a.realistic else "ideal"
    print(f"Записано {len(events)} подій і {len(truth)} точок еталону в {out}\n"
          f"Оцінити: dronewatch-eval run --events {out / 'events.jsonl'} --truth {out / 'truth.jsonl'} --sim-sensors {sensors} "
          f"--scan-dt {a.scan_dt:g}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="dronewatch-eval", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="оцінити запис подій")
    r.add_argument("--events", required=True, help="JSONL із подіями датчиків")
    r.add_argument("--truth", help="JSONL з еталоном (де цілі були насправді); без нього лише статистика треків")
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument("--config", help="YAML-конфіг сервісу: датчики, параметри трекінгу, scan_dt")
    src.add_argument("--sim-sensors", choices=["ideal", "realistic"], help="датчики симулятора (для записів із `simulate`)")
    r.add_argument("--scan-dt", type=float, default=0.0, help="крок скану, с (за замовчуванням із конфігу, інакше 0.5)")
    r.add_argument("--cutoff", type=float, default=100.0, help="OSPA: c, м")
    r.add_argument("-p", type=float, default=2.0, help="OSPA: p")
    r.add_argument("--gate", type=float, default=60.0, help="трек привʼязаний до цілі, якщо ближче, м")
    r.add_argument("--dim", type=int, choices=[2, 3], default=2, help="2: лише горизонталь; 3: з висотою")
    r.add_argument("--truth-offset", type=float, default=0.0, help="зсув годинника еталону відносно датчиків, с (додається до t еталону)")
    r.add_argument("--max-gap", type=float, default=5.0, help="розрив в еталоні довший за це, с: ціль на цей час не активна")
    r.add_argument("--synthetic-heartbeat", action="store_true",
                   help="запис без heartbeat: слати його за кожен датчик із запису на кожен скан "
                        "(інакше датчик, що мовчить понад 5 с, вважається втраченим)")
    r.add_argument("--json", help="зберегти звіт у JSON")
    r.set_defaults(fn=_cmd_run)
    s = sub.add_parser("simulate", help="згенерувати запис із симулятора (еталон відомий)")
    s.add_argument("--out", required=True, help="тека для events.jsonl і truth.jsonl")
    s.add_argument("--scenario", choices=["swarm", "base"], default="swarm")
    s.add_argument("--realistic", action="store_true", help="датчики з bias і затримками")
    s.add_argument("--duration", type=float, default=110.0)
    s.add_argument("--scan-dt", type=float, default=0.5)
    s.add_argument("--seed", type=int, default=1)
    s.set_defaults(fn=_cmd_simulate)
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):   # Windows: у каналі кодування за замовчуванням не вміє українську (у тестах потік підмінено)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    a = build_parser().parse_args(argv)
    try:
        return int(a.fn(a))
    except EvalError as e:
        print(f"помилка: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
