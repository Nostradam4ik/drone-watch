"""Аналітичний якобіан і векторизований гейтинг проти числових еталонів. Запуск: python test_models.py (або pytest)."""
import time

import numpy as np

from dronewatch.fusion.models import Sensor
from dronewatch.fusion.sim import default_sensors, simulate, swarm_targets
from dronewatch.fusion.tracker import Tracker


def sensors():
    radar = Sensor("radar1", "radar", (0, 0, 0), (5.0, 0.012, 0.026), 0.9, 1500, 0.0, "radar")
    rf = Sensor("rf1", "bearing", (400, -300, 0), (0.09,), 0.7, 1800, 0.0, "rf")
    ac = Sensor("ac1", "bearing", (-300, 200, 5), (0.17,), 0.6, 700, 0.0, "acoustic")
    return radar, rf, ac


def random_states(n, seed=0):
    rng = np.random.default_rng(seed)
    for _ in range(n):
        ang, rng_h = rng.uniform(-np.pi, np.pi), rng.uniform(30, 1500)
        yield np.array([rng_h * np.cos(ang), rng_h * np.sin(ang), rng.uniform(0, 150), *rng.uniform(-25, 25, 3)])


def test_analytic_matches_numeric():
    worst = 0.0
    for s in sensors():
        for latency in (0.0, 0.1, 0.5):
            s.latency_comp = latency
            for x in random_states(300, seed=int(latency * 10)):
                Ha, Hn = s.jacobian(x), s.jacobian_numeric(x)
                assert Ha.shape == Hn.shape == (s.dim, 6)
                assert np.allclose(Ha, Hn, rtol=1e-5, atol=1e-8), (s.name, latency, x, np.abs(Ha - Hn).max())
                worst = max(worst, float(np.abs(Ha - Hn).max()))
    print(f"   найбільше розходження з числовим: {worst:.2e}")


def test_azimuth_wraparound_is_not_a_problem():
    """Азимут біля ±π: числовий якобіан тут рахує різницю через wrap, аналітичний просто гладка похідна."""
    radar = sensors()[0]
    x = np.array([-800.0, 1e-6, 60.0, 0.0, 0.0, 0.0])    # прямо на «захід»: az = π
    assert np.allclose(radar.jacobian(x), radar.jacobian_numeric(x), rtol=1e-4, atol=1e-8)


def test_target_directly_above_sensor_is_finite():
    for s in sensors():
        x = np.array([s.pos[0], s.pos[1], 80.0, 3.0, -2.0, 0.0])
        H = s.jacobian(x)
        assert np.isfinite(H).all(), "над датчиком азимут вироджений, але NaN/inf в фільтр потрапляти не повинні"


def test_speed():
    """Не жорстка перевірка (залежить від машини), а щоб регрес до числового не пройшов непоміченим."""
    radar = sensors()[0]
    radar.latency_comp = 0.1
    xs = list(random_states(2000))
    t0 = time.perf_counter()
    for x in xs:
        radar.jacobian(x)
    t_an = time.perf_counter() - t0
    t0 = time.perf_counter()
    for x in xs:
        radar.jacobian_numeric(x)
    t_num = time.perf_counter() - t0
    print(f"   аналітичний {t_an / len(xs) * 1e6:.0f} мкс, числовий {t_num / len(xs) * 1e6:.0f} мкс: у {t_num / t_an:.0f} раз(ів) швидше")
    assert t_an < t_num / 2


def old_gate_cost(tr, sensor, z):
    """Еталон: гейтинг так, як він рахувався до оптимізації (числовий якобіан, solve і det для кожного вимірювання)."""
    H = sensor.jacobian_numeric(tr.x)
    y = sensor.residual(z, sensor.h(tr.x))
    S = H @ tr.P @ H.T + sensor.R
    d2 = float(y @ np.linalg.solve(S, y))
    return d2, d2 + float(np.log(np.linalg.det(S)))


def _live_tracker(steps=40):
    """Трекер після кількох сканів рою з bias і затримками: справжні IMM-коваріації, а не синтетичні матриці."""
    sens = default_sensors(realistic=True)
    for s in sens:
        s.latency_comp = s.latency
    tr = Tracker(sens)
    last = []
    for i, (t, dets, _truth) in enumerate(simulate(sens, swarm_targets(), duration=60, seed=2)):
        tr.step(t, dets)
        if i >= steps:
            last = dets
            break
    return tr, sens, last


def test_vectorized_gating_matches_per_measurement_reference():
    tr, sens, dets = _live_tracker()
    assert len(tr.tracks) >= 3, "для перевірки потрібно кілька треків"
    rng = np.random.default_rng(5)
    checked = 0
    for s in sens:
        real = [z for name, z in dets if name == s.name]
        junk = []
        for _ in range(40):   # довільні вимірювання з усього діапазону, зокрема далекі від усіх треків і з азимутом біля ±π
            z = [rng.uniform(50, 1500), rng.uniform(-np.pi, np.pi), rng.uniform(0, 0.5)] if s.kind == "radar" else [rng.uniform(-np.pi, np.pi)]
            junk.append(np.array(z))
        zs = real + junk
        Z = np.array(zs)
        for t in tr.tracks:
            d2, cost = t.gate_costs(s, Z)
            assert d2.shape == cost.shape == (len(zs),)
            for j, z in enumerate(zs):
                e2, ec = old_gate_cost(t, s, z)
                assert np.isclose(d2[j], e2, rtol=1e-6, atol=1e-6), (s.name, j, d2[j], e2)
                assert np.isclose(cost[j], ec, rtol=1e-6, atol=1e-6), (s.name, j, cost[j], ec)
                checked += 1
    print(f"   звірено {checked} пар «трек x вимірювання» зі старою формулою")


def test_gating_with_single_measurement_and_input_untouched():
    """m=1 (типово для пеленгів) і гейтинг не псує вхідний масив вимірювань (residuals працює на копії)."""
    tr, sens, _ = _live_tracker()
    s = sens[1]
    Z = np.array([[3.0]])           # азимут за межами (-π, π) має загортатися лише в копії
    before = Z.copy()
    d2, cost = tr.tracks[0].gate_costs(s, Z)
    assert d2.shape == (1,) and np.isfinite(d2).all() and np.isfinite(cost).all()
    assert (before == Z).all()


def test_gating_speed():
    tr, sens, _ = _live_tracker()
    radar = sens[0]
    Z = np.array([[rng_r, 0.1 * k, 0.2] for k, rng_r in enumerate(np.linspace(100, 1400, 100))])
    t0 = time.perf_counter()
    for t in tr.tracks:
        t.gate_costs(radar, Z)
    t_new = time.perf_counter() - t0
    t0 = time.perf_counter()
    for t in tr.tracks:
        for z in Z:
            old_gate_cost(t, radar, z)
    t_old = time.perf_counter() - t0
    print(f"   {len(tr.tracks)} треків x {len(Z)} вимірювань: {t_new * 1e3:.1f} мс проти {t_old * 1e3:.0f} мс: у {t_old / t_new:.0f} раз(ів) швидше")
    assert t_new < t_old / 5


