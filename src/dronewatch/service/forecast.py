"""Прогноз руху цілі та розбір зникнення треку. Чиста математика без стану: engine.py лише викликає її.

Прогноз це оцінка за поточним станом фільтра, а не обіцянка: вона припускає, що ціль зберігає швидкість, а розкид росте від
двох причин: похибка самого оцінювання (коваріація треку, що розповсюджується через модель постійної швидкості) і
можливий маневр (випадкове прискорення FORECAST_ACCEL, 1 sigma). Дрон, що різко повернув, у коло може й не потрапити."""
import math
from collections.abc import Sequence
from typing import Any

import numpy as np

from dronewatch.fusion.models import Floats, cv_matrices

FORECAST_HORIZONS = (5.0, 10.0, 15.0)   # через скільки секунд показуємо, де ціль буде
FORECAST_ACCEL = 1.0                    # м/с² (1 sigma): помірний маневр квадрокоптера; більший давав би кола завбільшки з усю карту
CHI2_68_2D = 2.296                      # межа 68 % (1 sigma) для двовимірного гаусового розкиду: у коло потрапляє ~2 з 3 дронів
LOST_KEEP_S = 90.0                      # скільки секунд зниклу ціль ще показуємо
SEARCH_MAX_M = 500.0                    # стеля радіуса зони пошуку: далі оцінка вже нічого не каже
SEARCH_COAST_S = 60.0                   # далі цього часу «куди могла полетіти» не екстраполюємо
EDGE_FRACTION = 0.85                    # частка дальності датчика, далі якої ціль вважаємо «на краю»
LOW_ALT_M = 25.0                        # нижче цієї висоти зникнення схоже на посадку чи схованку за забудовою


def forecast(x: Floats, P: Floats, horizons: Sequence[float] = FORECAST_HORIZONS, accel: float = FORECAST_ACCEL) -> list[dict[str, float]]:
    """Де ціль буде через кожен горизонт і радіус r (м) розкиду 1 sigma (~68 %): якщо ціль не маневруватиме сильніше за accel,
    вона потрапить у коло з ймовірністю близько двох до трьох. Для 95 % коло було б у 1,6 раза більшим."""
    out = []
    for tau in horizons:
        F, Q = cv_matrices(tau, accel)
        Pt = F @ P @ F.T + Q
        lam = float(np.linalg.eigvalsh(Pt[:2, :2]).max())
        out.append({"t": tau, "x": round(float(x[0] + x[3] * tau), 1), "y": round(float(x[1] + x[4] * tau), 1),
                    "r": round(math.sqrt(max(lam, 0.0) * CHI2_68_2D), 1)})
    return out


def search_area(snap: dict[str, Any], age: float, accel: float = FORECAST_ACCEL) -> dict[str, float]:
    """Де шукати зниклу ціль через age секунд після останнього бачення. Центр: куди вона долетіла б по прямій (екстраполяція
    швидкості, не довше SEARCH_COAST_S), радіус: похибка останньої позиції плюс можливий маневр. Ціль могла й зависнути чи сісти
    на місці, тому клієнт малює ще й відрізок від останньої позиції до центру."""
    age = max(age, 0.0)
    a = min(age, SEARCH_COAST_S)
    r = math.hypot(float(snap["err"]), accel * age ** 2 / 2)
    return {"fx": round(snap["x"] + snap["vx"] * a, 1), "fy": round(snap["y"] + snap["vy"] * a, 1), "r": round(min(r, SEARCH_MAX_M), 1)}


def loss_reason(snap: dict[str, Any], *, sensors_down: bool, edge: bool) -> str:
    """Чому трек міг зникнути (код): dive, radar_silent, sensor_down, low, edge, unknown. Найімовірніше пояснення першим.
    Це евристика за останнім відомим станом, а не знання."""
    if snap["diving"]:
        return "dive"          # різко йшов униз: імовірно впав
    if snap["radar_miss"] >= snap["radar_miss_limit"] - 1 and snap["misses"] < snap["max_misses"] - 1:
        return "radar_silent"  # пеленги ще були, а радар у своїй зоні не підтверджував: трек міг бути хибним
    if sensors_down:
        return "sensor_down"   # у цей момент хтось із датчиків мовчав: зникнення може бути наслідком відмови
    if snap["alt_known"] and snap["z"] < LOW_ALT_M:
        return "low"           # зник низько: посадка або схований за забудовою чи рельєфом
    if edge:
        return "edge"          # був біля краю дальності всіх датчиків: імовірно вилетів
    return "unknown"
