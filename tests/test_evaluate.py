"""Метрики трекінгу (OSPA тощо) і прогін запису через справжній Engine: відомі відповіді, межі, формати файлів."""
import json
import math

import numpy as np
import pytest

from dronewatch.fusion.metrics import Scorer, TruthSet, ospa
from dronewatch.service import evaluate
from dronewatch.service.evaluate import EvalError, evaluate_files, read_events, read_truth, simulate_recording


def pts(*rows):
    return np.array(rows, float).reshape(len(rows), 2)


# ---------- OSPA ----------
def test_ospa_both_empty_is_zero_and_one_empty_is_cutoff():
    assert ospa(pts(), pts()) == (0.0, 0.0, 0.0)
    total, loc, card = ospa(pts(), pts((0, 0)), c=100)
    assert (total, loc, card) == (100.0, 0.0, 100.0)
    assert ospa(pts((0, 0), (5, 5)), pts(), c=50)[0] == 50.0


def test_ospa_identical_sets_is_zero():
    x = pts((0, 0), (10, 10))
    assert ospa(x, x[::-1]) == (0.0, 0.0, 0.0)   # порядок точок неважливий


def test_ospa_known_value_with_localization_error():
    # одна точка зі зсувом 30 м: OSPA = 30 (c=100, p=2, n=1)
    total, loc, card = ospa(pts((0, 0)), pts((30, 0)), c=100, p=2)
    assert total == pytest.approx(30.0) and loc == pytest.approx(30.0) and card == 0.0


def test_ospa_cardinality_error_dominates():
    # 2 треки, 1 ціль: збігаються на одній, другий хибний: ((0 + 100^2) / 2)^(1/2)
    total, loc, card = ospa(pts((0, 0), (500, 500)), pts((0, 0)), c=100, p=2)
    assert total == pytest.approx(math.sqrt(100**2 / 2)) and loc == 0.0 and card == pytest.approx(math.sqrt(100**2 / 2))


def test_ospa_error_is_capped_at_cutoff():
    assert ospa(pts((0, 0)), pts((1e6, 0)), c=100)[0] == pytest.approx(100.0)


def test_ospa_rejects_bad_parameters():
    with pytest.raises(ValueError):
        ospa(pts((0, 0)), pts((0, 0)), c=0)
    with pytest.raises(ValueError):
        ospa(pts((0, 0)), pts((0, 0)), p=0.5)


# ---------- еталон ----------
def test_truth_interpolates_and_is_inactive_outside_and_in_gaps():
    ts = TruthSet([(0.0, "a", 0, 0, 0), (10.0, "a", 100, 0, 10), (100.0, "a", 100, 0, 10)], max_gap_s=20)
    assert ts.at(5.0)["a"].tolist() == [50.0, 0.0, 5.0]
    assert "a" not in ts.at(-1.0) and "a" not in ts.at(101.0)
    assert "a" not in ts.at(50.0)   # усередині розриву 90 с > 20 с ціль неактивна
    assert "a" not in TruthSet([(0.0, "a", 0, 0, 0), (50.0, "a", 5, 0, 0)], max_gap_s=20).at(25.0)


# ---------- Scorer ----------
def test_scorer_perfect_tracking_has_zero_error():
    sc = Scorer()
    for k in range(10):
        sc.step(float(k), {1: np.array([k, 0.0, 0.0])}, {"a": np.array([k, 0.0, 0.0])})
    s = sc.summary()
    assert s["ospa_mean"] == 0 and s["rmse_m"] == 0 and s["missed_pct"] == 0 and s["false_tracks"] == 0 and s["id_switches"] == 0


def test_scorer_false_track_and_missed_target_and_switch():
    sc = Scorer(gate=20)
    far = np.array([5000.0, 5000.0, 0.0])
    for k in range(6):
        sc.step(float(k), {1: np.array([k, 0.0, 0.0]), 9: far}, {"a": np.array([k, 0.0, 0.0])})   # 9 завжди хибний
    for k in range(6, 10):
        sc.step(float(k), {2: np.array([k, 0.0, 0.0])}, {"a": np.array([k, 0.0, 0.0])})   # ту саму ціль тримає вже інший трек
    sc.step(10.0, {}, {"a": np.array([10.0, 0.0, 0.0])})   # трек зник: пропуск
    s = sc.summary()
    assert s["false_tracks"] == 1 and s["tracks_total"] == 3
    assert s["id_switches"] == 1
    assert s["missed_pct"] == pytest.approx(100 / 11)
    assert s["never_found"] == 0 and s["time_to_confirm_s"] == 0.0


def test_scorer_without_truth_reports_only_track_statistics():
    sc = Scorer(flicker_s=3)
    for k in range(10):
        sc.step(float(k), {1: np.array([0.0, 0.0, 0.0]), **({2: np.array([1.0, 1.0, 0.0])} if k < 2 else {})}, {}, truth_present=False)
    s = sc.summary()
    assert s["has_truth"] is False and s["tracks_total"] == 2 and s["flicker_tracks"] == 1
    assert "ospa_mean" not in s and "rmse_m" not in s


def test_scorer_dim3_sees_altitude_error_dim2_does_not():
    tr, tg = {1: np.array([0.0, 0.0, 40.0])}, {"a": np.array([0.0, 0.0, 0.0])}
    s2, s3 = Scorer(dim=2), Scorer(dim=3)
    s2.step(0.0, tr, tg)
    s3.step(0.0, tr, tg)
    assert s2.summary()["rmse_m"] == 0 and s3.summary()["rmse_m"] == pytest.approx(40.0)


# ---------- файли ----------
def test_read_events_reports_file_and_line(tmp_path):
    p = tmp_path / "e.jsonl"
    p.write_text('{"sensor_id":"radar1","ts":1}\nне json\n', encoding="utf-8")
    with pytest.raises(EvalError, match=r"e\.jsonl:2"):
        read_events(p)
    p.write_text('{"sensor_id":"radar1","ts":"x"}\n', encoding="utf-8")
    with pytest.raises(EvalError, match="ts"):
        read_events(p)
    p.write_text('{"ts":1}\n', encoding="utf-8")
    with pytest.raises(EvalError, match="sensor_id"):
        read_events(p)
    p.write_text("\n\n", encoding="utf-8")
    with pytest.raises(EvalError, match="жодної"):
        read_events(p)


def test_read_events_sorts_by_time_and_rejects_nan(tmp_path):
    p = tmp_path / "e.jsonl"
    p.write_text('{"sensor_id":"a","ts":5}\n{"sensor_id":"a","ts":1}\n', encoding="utf-8")
    assert [e["ts"] for e in read_events(p)] == [1, 5]
    p.write_text('{"sensor_id":"a","ts":NaN}\n', encoding="utf-8")
    with pytest.raises(EvalError):
        read_events(p)


def test_read_truth_validates_and_applies_offset(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('{"t":10,"id":"g","x":1,"y":2}\n{"t":12,"id":"g","x":3,"y":2,"z":4}\n', encoding="utf-8")
    ts = read_truth(p, offset_s=-10)
    assert ts.first_time("g") == 0.0 and ts.at(1.0)["g"].tolist() == [2.0, 2.0, 2.0]
    p.write_text('{"t":10,"id":"g","x":"1","y":2}\n', encoding="utf-8")
    with pytest.raises(EvalError, match="'x'"):
        read_truth(p)
    with pytest.raises(EvalError):
        read_truth(tmp_path / "немає.jsonl")


# ---------- прогін через справжній Engine ----------
@pytest.fixture(scope="module")
def sim_run():
    events, truth, sensors = simulate_recording(targets="base", realistic=False, duration=90.0, seed=1)
    return events, TruthSet([(r["t"], r["id"], r["x"], r["y"], r["z"]) for r in truth]), sensors


def test_replay_of_simulated_flight_tracks_targets_well(sim_run):
    events, truth, sensors = sim_run
    r = evaluate_files(events, truth, sensors=sensors, scan_dt=0.5)
    assert r["has_truth"] and r["truth_targets"] == 3
    assert r["never_found"] == 0
    assert r["rmse_m"] < 40 and r["missed_pct"] < 25 and r["false_track_pct"] < 40
    assert r["ospa_mean"] < 60
    assert r["events"]["rejected"] == 0


def test_replay_is_deterministic(sim_run):
    events, truth, sensors = sim_run
    a = evaluate_files(events, truth, sensors=sensors)
    b = evaluate_files(events, truth, sensors=sensors)
    assert a == b


def test_replay_without_truth_still_reports_tracks(sim_run):
    events, _, sensors = sim_run
    r = evaluate_files(events, None, sensors=sensors)
    assert r["has_truth"] is False and r["tracks_total"] >= 3
    assert "ospa_mean" not in r


def test_unknown_sensor_ids_are_reported_as_rejected(sim_run):
    events, truth, sensors = sim_run
    bad = [{**e, "sensor_id": "нема"} if e["sensor_id"] == "radar1" else e for e in events]
    r = evaluate_files(bad, truth, sensors=sensors)
    assert r["events"]["rejected"] + r["events"]["dropped"] > 0
    assert "УВАГА" in evaluate.format_report(r)


def test_missing_sensors_without_heartbeat_hurts_unless_synthetic(sim_run):
    events, truth, sensors = sim_run
    no_beats = [e for e in events if not e.get("heartbeat")]
    with_synth = evaluate_files(no_beats, truth, sensors=sensors, synthetic_heartbeat=True)
    assert with_synth["events"]["heartbeats"] > 0 and with_synth["never_found"] == 0


# ---------- CLI ----------
def test_cli_simulate_then_run_writes_json_report(tmp_path, capsys):
    out = tmp_path / "rec"
    assert evaluate.main(["simulate", "--out", str(out), "--scenario", "base", "--duration", "60", "--seed", "2"]) == 0
    report = tmp_path / "r.json"
    code = evaluate.main(["run", "--events", str(out / "events.jsonl"), "--truth", str(out / "truth.jsonl"), "--sim-sensors", "ideal",
                          "--json", str(report)])
    text = capsys.readouterr().out
    assert code == 0 and "OSPA" in text and "хибні треки" in text
    assert json.loads(report.read_text(encoding="utf-8"))["has_truth"] is True


def test_cli_bad_input_is_exit_2_not_traceback(tmp_path, capsys):
    p = tmp_path / "bad.jsonl"
    p.write_text("{\n", encoding="utf-8")
    assert evaluate.main(["run", "--events", str(p), "--sim-sensors", "ideal"]) == 2
    assert "bad.jsonl:1" in capsys.readouterr().err


def test_cli_run_requires_a_sensor_source(tmp_path):
    with pytest.raises(SystemExit):
        evaluate.main(["run", "--events", str(tmp_path / "x.jsonl")])
