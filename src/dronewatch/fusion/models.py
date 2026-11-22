"""Моделі датчиків і руху. Усі координати в локальній ENU (метри)."""
import math
from typing import Literal, overload

import numpy as np
from numpy.typing import ArrayLike, NDArray

Floats = NDArray[np.float64]
Kind = Literal["radar", "bearing"]   # radar: дальність, азимут, елевація; bearing: лише азимут (RF, акустика)


@overload
def wrap(a: float) -> float: ...
@overload
def wrap(a: Floats) -> Floats: ...
def wrap(a):
    """Кут у діапазон (-pi, pi]; працює і для числа, і для масиву."""
    return (a + np.pi) % (2 * np.pi) - np.pi


class Sensor:
    """kind='radar' міряє (дальність, азимут, елевацію); kind='bearing' лише азимут (RF, акустика)."""

    def __init__(self, name: str, kind: Kind, pos: ArrayLike, sigma: ArrayLike, pd: float, max_range: float, clutter_rate: float,
                 label: str) -> None:
        self.name = name
        self.kind = kind
        self.label = label  # фізичний тип: radar / rf / acoustic
        self.type_id = label  # модель з палітри (sim.SENSOR_TYPES), напр. radar-short; за замовчуванням збігається з label
        self.pos = np.asarray(pos, float)
        self.sigma = np.asarray(sigma, float)
        self.pd = pd
        self.max_range = max_range
        self.heading = np.pi / 2    # куди дивиться датчик, рад, у системі азимута (схід = 0, проти годинникової); за замовчуванням північ
        self.fov = 2 * np.pi        # ширина сектора огляду по азимуту, рад; 2π = круговий огляд
        self.clutter_rate = clutter_rate  # середня кількість хибних детекцій за скан
        self.R = np.diag(self.sigma ** 2)
        self.dim = 3 if kind == "radar" else 1
        self.bias_est = np.zeros(self.dim)  # те, що трекер ВВАЖАЄ зсувом датчика (віднімається від вимірювань)
        self.latency_comp = 0.0             # затримка, яку трекер знає і компенсує, с
        self.bias = np.zeros(self.dim)      # справжня систематична похибка (знає лише симулятор), на відміну від bias_est
        self.latency = 0.0                  # справжня затримка датчика (знає лише симулятор), на відміну від latency_comp

    def in_fov(self, p: ArrayLike) -> bool:
        """Чи потрапляє точка (за горизонтальним напрямком) у сектор огляду датчика. Дальність тут не перевіряється."""
        if self.fov >= 2 * np.pi - 1e-9:
            return True
        d = np.asarray(p, float)[:2] - self.pos[:2]
        return bool(abs(wrap(float(np.arctan2(d[1], d[0])) - self.heading)) <= self.fov / 2)

    def h(self, x: Floats) -> Floats:
        d = x[:3] - self.latency_comp * x[3:6] - self.pos  # де ціль була в момент вимірювання
        az = np.arctan2(d[1], d[0])
        if self.kind == "radar":
            return np.array([np.linalg.norm(d), az, np.arctan2(d[2], np.hypot(d[0], d[1]))])
        return np.array([az])

    def residual(self, z: Floats, zp: Floats) -> Floats:
        y = z - zp
        y[1 if self.kind == "radar" else 0] = wrap(y[1 if self.kind == "radar" else 0])
        return y

    def residuals(self, zs: Floats, zp: Floats) -> Floats:
        """residual для пачки вимірювань (m, dim) проти одного прогнозу zp: без python-циклу по вимірюваннях."""
        Y = zs - zp
        k = 1 if self.kind == "radar" else 0
        Y[:, k] = wrap(Y[:, k])
        return Y

    def jacobian(self, x: Floats) -> Floats:
        """Аналітичний якобіан h за станом (дальність, азимут, елевація за позицією й швидкістю).

        Зі зсуву d = p - L*v - s (L = latency_comp): ∂d/∂p = I, ∂d/∂v = -L*I, тож H = [J, -L*J], де J = ∂h/∂d.
        ρ² = dx²+dy², r² = ρ²+dz²:  ∂r/∂d = d/r;  ∂az/∂d = (-dy, dx, 0)/ρ²;  ∂el/∂d = (-dz*dx/ρ, -dz*dy/ρ, ρ)/r².
        Замінює числовий (13 викликів h на виклик), що давав ~70% часу гейтингу. Звіряється з jacobian_numeric у test_models.py."""
        d = x[:3] - self.latency_comp * x[3:6] - self.pos
        rho2 = max(d[0] * d[0] + d[1] * d[1], 1e-9)   # ціль точно над датчиком: азимут вироджений, не ділимо на нуль
        daz = np.array([-d[1] / rho2, d[0] / rho2, 0.0])
        if self.kind == "radar":
            rho = math.sqrt(rho2)
            r2 = rho2 + d[2] * d[2]
            r = math.sqrt(r2)
            J = np.array([d / r, daz, [-d[2] * d[0] / (rho * r2), -d[2] * d[1] / (rho * r2), rho / r2]])
        else:
            J = daz[None, :]
        H = np.empty((self.dim, 6))
        H[:, :3] = J
        H[:, 3:] = -self.latency_comp * J
        return H

    def jacobian_numeric(self, x: Floats, eps: float = 1e-3) -> Floats:
        """Центральні різниці. Лишено лише як еталон для перевірки аналітичного якобіана."""
        H = np.zeros((self.dim, 6))
        for i in range(6):
            dx = np.zeros(6)
            dx[i] = eps
            H[:, i] = self.residual(self.h(x + dx), self.h(x - dx)) / (2 * eps)
        return H


def cv_matrices(dt: float, q: float) -> tuple[Floats, Floats]:
    """Модель постійної швидкості: F і Q (шум прискорення q, м/с²)."""
    F = np.eye(6)
    F[:3, 3:] = dt * np.eye(3)
    Q = np.zeros((6, 6))
    for i in range(3):
        Q[i, i] = dt ** 4 / 4 * q ** 2
        Q[i, i + 3] = Q[i + 3, i] = dt ** 3 / 2 * q ** 2
        Q[i + 3, i + 3] = dt ** 2 * q ** 2
    return F, Q
