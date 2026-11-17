#!/usr/bin/env python3
"""Емулятор датчиків Drone Watch: шле події на POST /api/events так, як це робив би справжній пристрій.

Лише стандартна бібліотека Python 3.10+. Датчики (позиції, дальності) береться з GET /api/config сервісу,
тож емулятор завжди збігається з його конфігурацією.

Приклади:
    python emulate.py --scenario cross                       # один дрон перетинає об'єкт
    python emulate.py --scenario swarm --count 8             # рій із 8 дронів
    python emulate.py --scenario hover --only radar1,rf1     # без акустики (імітація відмови датчика)
    python emulate.py --scenario cross --bias rf1=3          # RF «довертає» на 3° (перевірка калібрування)
    python emulate.py --scenario cross --dry-run             # лише показати події, нічого не слати
    python emulate.py --key radar1=КЛЮЧ --key rf1=КЛЮЧ ...   # ключі датчиків (сервіс у live вимагає ключ на кожен датчик)
    DRONEWATCH_KEYS="radar1=К1,rf1=К2,ac1=К3" python emulate.py   # те саме через оточення (не потрапляє в історію команд)

Ключі: HTTP шле їх у заголовку X-API-Key (окремий запит на кожен датчик: ключ належить одному датчику),
MQTT кладе в поле "key" кожної події і підключається до брокера з логіном = id датчика та паролем = ключ датчика
(по з'єднанню на датчик: ACL брокера дозволяє датчику писати лише у власний топік). --mqtt-user/--mqtt-password
задають один спільний логін на всіх датчиків (шлюз). TLS: --mqtt-ca CA.PEM (довіряти лише цьому CA) або --mqtt-tls
(системні CA); ім'я хоста з --mqtt має бути в SAN сертифіката брокера.
HTTPS до сервісу: --url https://ІМ'Я:8000 --ca CA.PEM (ім'я має бути в SAN сертифіката сервісу); сертифікат перевіряється
завжди. Сервіс у режимі розробки без TLS: --url http://127.0.0.1:8000 (ключі підуть відкритим текстом).
Видати ключі: python service/keygen.py radar1 rf1 ac1.

Вхід у дашборд: GET /api/config і /api/state за паролем, тож емулятор входить сам: --dashboard-user ІМ'Я і пароль у змінній
DRONEWATCH_DASHBOARD_PASSWORD (не в аргументах: їх видно в списку процесів). Справжнім датчикам вхід у дашборд не потрібен:
вони лише шлють події (ключ датчика). Сервіс без входу (demo, dashboard.auth: disabled) цього не вимагає.

Heartbeat: кожен скан кожен справний датчик шле {"sensor_id": ..., "heartbeat": true}, навіть коли нічого не бачить.
Так сервіс відрізняє «небо порожнє» від «датчик відмовив». Вимкнути: --no-heartbeat.

Формат події (азимут компасний: від півночі за годинниковою стрілкою):
    {"sensor_id": "radar1", "ts": 1760000000.5, "azimuth_deg": 212.0, "range_m": 640.0, "elevation_deg": 4.5}
"""
import argparse
import json
import math
import os
import random
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http.cookiejar import CookieJar
from itertools import pairwise
from typing import Any

SensorInfo = dict[str, Any]     # {"id", "kind", "label", "pos", "max_range"} з /api/config
Event = dict[str, Any]
Point = tuple[float, float, float]
Noise = tuple[float, float, float]   # дальність м, азимут °, елевація °; для пеленгових датчиків дальність і елевація не використовуються
Bias = dict[str, tuple[float, float, float]]

DEFAULT_NOISE: dict[str, Noise] = {"radar": (5.0, 0.7, 1.5), "rf": (0.0, 5.0, 0.0), "acoustic": (0.0, 10.0, 0.0)}
FALLBACK_SENSORS: list[SensorInfo] = [  # якщо сервіс недоступний (--dry-run без сервера)
    {"id": "radar1", "kind": "radar", "label": "radar", "pos": [0, 0], "max_range": 1500},
    {"id": "rf1", "kind": "bearing", "label": "rf", "pos": [400, -300], "max_range": 1800},
    {"id": "ac1", "kind": "bearing", "label": "acoustic", "pos": [-300, 200], "max_range": 700},
]


# ---------- цілі ----------
class Target:
    """Рух по вершинах [(t, x, y, z), ...] з лінійною інтерполяцією."""

    def __init__(self, name: str, wp: Sequence[Sequence[float]]) -> None:
        self.name, self.wp = name, wp

    def active(self, t: float) -> bool:
        return self.wp[0][0] <= t <= self.wp[-1][0]

    def pos(self, t: float) -> Point:
        for a, b in pairwise(self.wp):
            if a[0] <= t <= b[0]:
                f = (t - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0.0
                return (a[1] + f * (b[1] - a[1]), a[2] + f * (b[2] - a[2]), a[3] + f * (b[3] - a[3]))
        last = self.wp[-1]
        return (last[1], last[2], last[3])


def scenario_cross(rng: random.Random, count: int) -> list[Target]:
    return [Target("cross", [(0, -900, 100, 70), (120, 900, 100, 70)])]


def scenario_hover(rng: random.Random, count: int) -> list[Target]:
    return [Target("hover", [(0, 600, 700, 60), (30, 250, 250, 60), (60, 250, 250, 60), (80, 800, 600, 120)])]


def scenario_circle(rng: random.Random, count: int) -> list[Target]:
    r, v, n = 400.0, 12.0, 36
    period = 2 * math.pi * r / v
    return [Target("circle", [(period * k / n, r * math.cos(2 * math.pi * k / n), r * math.sin(2 * math.pi * k / n), 80.0)
                              for k in range(n + 1)])]


def scenario_swarm(rng: random.Random, count: int) -> list[Target]:
    """count дронів із різних боків через центр; шляхи перетинаються."""
    out = []
    for k in range(count):
        ang = 2 * math.pi * k / count + rng.uniform(-0.2, 0.2)
        sx, sy = 800 * math.cos(ang), 800 * math.sin(ang)
        ax, ay = rng.uniform(-150, 150), rng.uniform(-150, 150)
        f = rng.uniform(0.8, 1.1)
        ex, ey = ax + (ax - sx) * f, ay + (ay - sy) * f
        alt, speed, t0 = rng.uniform(40, 120), rng.uniform(14, 20), 2.0 * k
        t1 = t0 + math.hypot(ax - sx, ay - sy) / speed
        t2 = t1 + math.hypot(ex - ax, ey - ay) / speed
        out.append(Target(f"S{k}", [(t0, sx, sy, alt), (t1, ax, ay, alt), (t2, ex, ey, alt + rng.uniform(-20, 20))]))
    return out


SCENARIOS: dict[str, Callable[[random.Random, int], list[Target]]] = {
    "cross": scenario_cross, "hover": scenario_hover, "circle": scenario_circle, "swarm": scenario_swarm}


# ---------- датчики ----------
def compass(dx: float, dy: float) -> float:
    return math.degrees(math.atan2(dx, dy)) % 360.0


def poisson(rng: random.Random, lam: float) -> int:
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def measure(rng: random.Random, sensor: SensorInfo, pos: Point, bias: Bias, noise: Noise) -> Event:
    """Одне вимірювання цілі датчиком: істина + bias + шум."""
    dx, dy, dz = pos[0] - sensor["pos"][0], pos[1] - sensor["pos"][1], pos[2]
    rng_h = math.hypot(dx, dy)
    sr, sa, se = noise
    b = bias.get(sensor["id"], (0.0, 0.0, 0.0))
    e: Event = {"azimuth_deg": (compass(dx, dy) + b[1] + rng.gauss(0, sa)) % 360.0}
    if sensor["kind"] == "radar":
        e["range_m"] = math.hypot(rng_h, dz) + b[0] + rng.gauss(0, sr)
        e["elevation_deg"] = math.degrees(math.atan2(dz, rng_h)) + b[2] + rng.gauss(0, se)
    return e


def clutter(rng: random.Random, sensor: SensorInfo) -> Event:
    e: Event = {"azimuth_deg": rng.uniform(0, 360)}
    if sensor["kind"] == "radar":
        e["range_m"] = rng.uniform(50, sensor["max_range"])
        e["elevation_deg"] = rng.uniform(0, 30)
    return e


# ---------- мережа ----------
def tls_context(ca: str | None = None) -> ssl.SSLContext:
    """TLS для MQTT: сертифікат брокера й ім'я хоста перевіряються обов'язково, не нижче TLS 1.2. З ca довіряємо лише цьому CA
    (PEM), без нього системному сховищу. Режиму «не перевіряти сертифікат» тут свідомо немає."""
    ctx = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class MqttPublisher:
    """Мінімальний клієнт MQTT 3.1.1 (лише CONNECT і PUBLISH QoS 0), щоб емулятор лишався без залежностей."""

    def __init__(self, host: str, port: int = 1883, username: str | None = None, password: str | None = None,
                 client_id: str = "dronewatch-emulator", tls: ssl.SSLContext | None = None) -> None:
        """tls: ssl.SSLContext (див. tls_context) або None для відкритого зʼєднання."""
        self.args = (host, port, username, password, client_id)
        self.tls = tls
        self.sock: socket.socket | None = None
        self._connect()

    def _connect(self) -> None:
        host, port, username, password, client_id = self.args
        if self.sock:
            self.close()
        sock = socket.create_connection((host, port), timeout=5)
        if self.tls:   # server_hostname вмикає перевірку імені хоста проти SAN сертифіката
            try:
                sock = self.tls.wrap_socket(sock, server_hostname=host)
            except OSError:
                sock.close()
                raise
        self.sock = sock
        flags = 0x02  # clean session
        payload = self._str(client_id)
        if username:
            flags |= 0x80 | (0x40 if password else 0)
            payload += self._str(username) + (self._str(password) if password else b"")
        var = self._str("MQTT") + bytes([4, flags]) + (60).to_bytes(2, "big")
        self._send(0x10, var + payload)
        ack = sock.recv(4)
        if len(ack) < 4 or ack[0] != 0x20 or ack[3] != 0:
            hint = {4: "хибні логін чи пароль", 5: "користувача немає або немає прав"}.get(ack[3]) if len(ack) == 4 else None
            if not ack:   # брокер закрив зʼєднання, нічого не відповівши
                hint = "брокер закрив зʼєднання без відповіді"
                if not self.tls:
                    hint += "; найчастіше це порт з TLS, а клієнт без нього (--mqtt-ca CA.PEM)"
            raise OSError(f"брокер відхилив підключення (CONNACK {ack.hex() or 'порожній'}{': ' + hint if hint else ''})")

    @staticmethod
    def _str(s: str) -> bytes:
        b = s.encode("utf-8")
        return len(b).to_bytes(2, "big") + b

    def _send(self, first: int, body: bytes) -> None:
        if self.sock is None:
            raise OSError("немає зʼєднання з брокером")
        n, enc = len(body), bytearray()
        while True:  # довжина тіла змінної довжини (по 7 біт)
            byte, n = n % 128, n // 128
            enc.append(byte | (0x80 if n else 0))
            if not n:
                break
        self.sock.sendall(bytes([first]) + bytes(enc) + body)

    def publish(self, topic: str, obj: Any) -> None:
        packet = self._str(topic) + json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self._send(0x30, packet)
        except OSError:
            self._connect()   # брокер розірвав з'єднання (перезапуск, витіснення): одна спроба перепідключитися
            self._send(0x30, packet)

    def close(self) -> None:
        try:
            self._send(0xE0, b"")
            if self.sock:
                self.sock.close()
        except OSError:
            pass


HTTPS_CONTEXT: ssl.SSLContext | None = None   # для https://-адрес (встановлює main); для http:// не використовується
_cache: tuple[ssl.SSLContext | None, urllib.request.OpenerDirector | None] = (None, None)   # (контекст, opener із cookie сесії)


def opener() -> urllib.request.OpenerDirector:
    """Один opener на контекст TLS: cookie сесії зберігається між запитами, а зміна контексту дає новий opener."""
    global _cache
    if _cache[1] is None or _cache[0] is not HTTPS_CONTEXT:
        handlers: list[urllib.request.BaseHandler] = [urllib.request.HTTPCookieProcessor(CookieJar())]
        if HTTPS_CONTEXT:
            handlers.append(urllib.request.HTTPSHandler(context=HTTPS_CONTEXT))
        _cache = (HTTPS_CONTEXT, urllib.request.build_opener(*handlers))
    assert _cache[1] is not None
    return _cache[1]


def http(url: str, data: Any = None, timeout: float = 5, key: str | None = None) -> Any:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None, headers=headers)
    with opener().open(req, timeout=timeout) as r:
        return json.load(r)


def parse_bias(items: list[str] | None) -> Bias:
    """--bias rf1=3  або  --bias radar1=8,1,0.5  (дальність м, азимут °, елевація °)."""
    out: Bias = {}
    for item in items or []:
        sid, _, vals = item.partition("=")
        v = [float(x) for x in vals.split(",")]
        if len(v) not in (1, 3):
            sys.exit(f"--bias {item}: потрібно одне число (азимут, °) або три (дальність м, азимут °, елевація °)")
        out[sid] = (0.0, v[0], 0.0) if len(v) == 1 else (v[0], v[1], v[2])
    return out


def parse_keys(items: list[str] | None, env: str | None) -> dict[str, str]:
    """--key radar1=КЛЮЧ (можна кілька) і/або DRONEWATCH_KEYS="radar1=К1,rf1=К2". Прапорці перекривають оточення."""
    out: dict[str, str] = {}
    for item in [x for x in (env or "").split(",") if x.strip()] + list(items or []):
        sid, sep, key = item.strip().partition("=")
        if not sep or not sid or not key:
            sys.exit("ключ датчика: очікується ІМ'Я=КЛЮЧ (повідомлення не містить введеного значення: це міг бути сам ключ)")
        out[sid] = key
    return out


# ---------- запуск ----------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Емулятор датчиків Drone Watch", formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=(__doc__ or "").split("Приклади:")[1])
    ap.add_argument("--url", default="https://127.0.0.1:8000", help="адреса сервісу; https:// з перевіркою сертифіката й імені хоста")
    ap.add_argument("--ca", metavar="CA.PEM", help="для https://: довіряти лише цьому CA (інакше системним); для MQTT див. --mqtt-ca")
    ap.add_argument("--dashboard-user", metavar="ІМ'Я", help="логін у дашборд (пароль у змінній DRONEWATCH_DASHBOARD_PASSWORD)")
    ap.add_argument("--scenario", choices=SCENARIOS, default="cross")
    ap.add_argument("--count", type=int, default=8, help="кількість дронів (для swarm)")
    ap.add_argument("--duration", type=float, default=0, help="тривалість, с (0 = до кінця сценарію)")
    ap.add_argument("--period", type=float, default=0.5, help="як часто датчики видають вимірювання, с")
    ap.add_argument("--pd", type=float, default=0.85, help="ймовірність, що датчик побачить ціль за скан")
    ap.add_argument("--clutter", type=float, default=0.3, help="середня кількість хибних детекцій за скан на датчик")
    ap.add_argument("--only", help="емулювати лише ці датчики (через кому), решта «відмовила»")
    ap.add_argument("--bias", action="append", help="систематична похибка: rf1=3 або radar1=8,1,0.5")
    ap.add_argument("--key", action="append", metavar="ДАТЧИК=КЛЮЧ",
                    help="ключ датчика (повторювати для кожного); див. також DRONEWATCH_KEYS")
    ap.add_argument("--no-heartbeat", action="store_true", help="не слати heartbeat (поведінка старих датчиків)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--loop", action="store_true", help="повторювати сценарій безкінечно")
    ap.add_argument("--mqtt", metavar="HOST[:PORT]", help="слати події в MQTT-брокер замість HTTP")
    ap.add_argument("--topic", default="dronewatch/events", help="префікс топіка MQTT; події йдуть у <префікс>/<sensor_id>")
    ap.add_argument("--mqtt-ca", metavar="CA.PEM", help="шифрувати MQTT (TLS) і довіряти лише цьому CA; ім'я хоста перевіряється")
    ap.add_argument("--mqtt-tls", action="store_true", help="шифрувати MQTT (TLS) і довіряти системним CA")
    ap.add_argument("--mqtt-user")
    ap.add_argument("--mqtt-password")
    ap.add_argument("--dry-run", action="store_true", help="друкувати події, не надсилаючи")
    return ap.parse_args(argv)


def configure_https(a: argparse.Namespace) -> None:
    """Контекст TLS для https://-адреси: сертифікат і ім'я хоста перевіряються завжди, режиму «не перевіряти» немає."""
    global HTTPS_CONTEXT
    if not a.url.startswith("https://"):
        return
    try:
        HTTPS_CONTEXT = tls_context(a.ca)
    except (OSError, ssl.SSLError) as e:
        sys.exit(f"Не вдалося прочитати CA «{a.ca}»: {e}")


def dashboard_login(a: argparse.Namespace) -> None:
    if not a.dashboard_user or a.dry_run:
        return
    password = os.environ.get("DRONEWATCH_DASHBOARD_PASSWORD")
    if not password:
        sys.exit("Для --dashboard-user потрібна змінна DRONEWATCH_DASHBOARD_PASSWORD (пароль не береться з аргументів)")
    try:
        http(a.url + "/api/login", {"username": a.dashboard_user, "password": password})
    except urllib.error.HTTPError as e:
        sys.exit(f"Вхід у дашборд не вдався: HTTP {e.code}" + (" (хибний логін або пароль)" if e.code == 401 else ""))
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"Сервіс недоступний ({a.url}): {e}")


def connection_hint(url: str, e: OSError) -> str:
    """Підказка, що робити, коли сервіс недоступний: найтиповіші помилки схеми (http/https) і входу."""
    if isinstance(e, urllib.error.HTTPError) and e.code == 401:
        return "\nСервіс вимагає вхід у дашборд: --dashboard-user ІМ'Я і змінна DRONEWATCH_DASHBOARD_PASSWORD"
    if url.startswith("https://") and "WRONG_VERSION_NUMBER" in str(e):
        return "\nСхоже, сервіс слухає відкритий HTTP (режим розробки): --url http://..."
    if url.startswith("http://") and "Connection" in type(getattr(e, "reason", e)).__name__:
        return "\nЯкщо сервіс працює по HTTPS: --url https://... --ca CA.PEM"
    return ""


def fetch_sensors(a: argparse.Namespace) -> list[SensorInfo]:
    """Датчики з GET /api/config; без сервера лише з --dry-run беремо вбудовані."""
    try:
        sensors: list[SensorInfo] = http(a.url + "/api/config")["sensors"]
        print(f"Підключено до {a.url}, датчиків: {[s['id'] for s in sensors]}")
        return sensors
    except (urllib.error.URLError, OSError) as e:
        if not a.dry_run:
            sys.exit(f"Сервіс недоступний ({a.url}): {e}{connection_hint(a.url, e)}\nЗапустіть його або використайте --dry-run.")
        print("Сервіс недоступний, використовую вбудовані датчики (dry-run).")
        return FALLBACK_SENSORS


def select_sensors(sensors: list[SensorInfo], only: str | None) -> list[SensorInfo]:
    if not only:
        return sensors
    keep = set(only.split(","))
    unknown = keep - {s["id"] for s in sensors}
    if unknown:
        sys.exit(f"Невідомі датчики в --only: {sorted(unknown)}")
    return [s for s in sensors if s["id"] in keep]


def warn_about_keys(a: argparse.Namespace, keys: dict[str, str], sensors: list[SensorInfo]) -> None:
    if keys and a.url.startswith("http://") and not a.dry_run:
        print("Увага: сервіс по відкритому HTTP, ключі датчиків (X-API-Key) ідуть відкритим текстом (https://... і --ca CA.PEM)")
    unknown_keys = set(keys) - {s["id"] for s in sensors}
    if unknown_keys:
        print(f"Увага: ключі для невідомих датчиків ігноруються: {sorted(unknown_keys)}")
    if not keys and not a.dry_run:
        print("Увага: ключів датчиків не задано; сервіс у live-режимі з автентифікацією відхилить усі події "
              "(--key ДАТЧИК=КЛЮЧ або DRONEWATCH_KEYS)")


def open_publishers(a: argparse.Namespace, sensors: list[SensorInfo], keys: dict[str, str]) -> dict[str, MqttPublisher]:
    """MQTT-з'єднання по датчиках (id -> з'єднання); порожньо, якщо --mqtt не задано чи це dry-run."""
    if not a.mqtt or a.dry_run:
        return {}
    host, _, port_text = a.mqtt.partition(":")
    port = int(port_text or 1883)
    try:
        tls = tls_context(a.mqtt_ca) if (a.mqtt_ca or a.mqtt_tls) else None
    except (OSError, ssl.SSLError) as e:
        sys.exit(f"Не вдалося прочитати CA «{a.mqtt_ca}»: {e}")
    if not tls and (keys or a.mqtt_user):
        print("Увага: MQTT без TLS, ключі й паролі йдуть відкритим текстом (--mqtt-ca CA.PEM або --mqtt-tls)")
    pubs: dict[str, MqttPublisher] = {}
    try:
        if a.mqtt_user:   # один спільний логін на всіх (шлюз), як раніше
            shared = MqttPublisher(host, port, a.mqtt_user, a.mqtt_password, tls=tls)
            pubs = {s["id"]: shared for s in sensors}
        else:             # брокер з ACL: кожен датчик це окремий користувач (ім'я = id датчика, пароль = його ключ)
            for s in sensors:
                k = keys.get(s["id"])
                pubs[s["id"]] = MqttPublisher(host, port, s["id"] if k else None, k,
                                              client_id=s["id"] if k else "emulator-" + s["id"], tls=tls)
    except OSError as e:
        sys.exit(f"MQTT-брокер недоступний ({a.mqtt}): {e}")
    print(f"Події йдуть у MQTT {a.mqtt}{' (TLS)' if tls else ''}, топіки {a.topic}/<sensor_id>")
    return pubs


@dataclass(slots=True)
class Totals:
    sent: int = 0
    rejected: int = 0


def scan_events(a: argparse.Namespace, rng: random.Random, sensors: list[SensorInfo], targets: list[Target], bias: Bias,
                el: float) -> list[Event]:
    """Усі події одного скану: вимірювання цілей і хибні детекції (перемішані), далі heartbeat кожного датчика."""
    events: list[Event] = []
    for s in sensors:
        noise = DEFAULT_NOISE[s["label"]]
        for tg in targets:
            if not tg.active(el):
                continue
            p = tg.pos(el)
            if math.hypot(p[0] - s["pos"][0], p[1] - s["pos"][1]) > s["max_range"] or rng.random() > a.pd:
                continue
            events.append({"sensor_id": s["id"], "ts": time.time(), **measure(rng, s, p, bias, noise)})
        for _ in range(poisson(rng, a.clutter)):
            events.append({"sensor_id": s["id"], "ts": time.time(), **clutter(rng, s)})
    rng.shuffle(events)
    if not a.no_heartbeat:
        events += [{"sensor_id": s["id"], "heartbeat": True} for s in sensors]
    return events


def send_scan(a: argparse.Namespace, sensors: list[SensorInfo], keys: dict[str, str], pubs: dict[str, MqttPublisher],
              events: list[Event], totals: Totals) -> None:
    """Надсилає події скану по датчиках: ключ належить одному датчику, тож окрема публікація чи запит на кожен.
    Ключ додається лише в момент відправки, тому в dry-run і виводі його немає."""
    try:
        for s in sensors:
            batch = [e for e in events if e["sensor_id"] == s["id"]]
            if not batch:
                continue
            key = keys.get(s["id"])
            if pubs:
                pubs[s["id"]].publish(f"{a.topic}/{s['id']}", [{**e, "key": key} for e in batch] if key else batch)
                totals.sent += len(batch)
            else:
                r = http(a.url + "/api/events", batch, key=key)
                totals.sent += r["accepted"]
                totals.rejected += r["rejected"]
    except urllib.error.HTTPError as e:
        if e.code == 401:
            sys.exit("Сервіс відхилив ключ датчика (401). Перевірте --key / DRONEWATCH_KEYS і key_sha256 у конфігу сервісу.")
        print(f"  помилка відправки: {e}")
    except (urllib.error.URLError, OSError) as e:
        print(f"  помилка відправки: {e}")


def report_progress(a: argparse.Namespace, el: float, totals: Totals) -> None:
    try:
        st = http(a.url + "/api/state")
        live = len([t for t in st["tracks"] if not t["coasting"]])
        print(f"  t={el:5.0f}с  надіслано={totals.sent}  відхилено={totals.rejected}  треків у сервісі={live}")
    except (urllib.error.URLError, OSError):
        pass


def run_scenario(a: argparse.Namespace, rng: random.Random, sensors: list[SensorInfo], keys: dict[str, str],
                 pubs: dict[str, MqttPublisher], bias: Bias) -> None:
    targets = SCENARIOS[a.scenario](rng, a.count)
    end = a.duration or max(t.wp[-1][0] for t in targets)
    print(f"Сценарій «{a.scenario}»: цілей {len(targets)}, {end:.0f} с")
    t0, totals, last_report = time.time(), Totals(), 0.0
    while (el := time.time() - t0) <= end:
        events = scan_events(a, rng, sensors, targets, bias, el)
        if a.dry_run:
            for e in events:
                print(json.dumps(e, ensure_ascii=False))
        elif events:
            send_scan(a, sensors, keys, pubs, events, totals)
        if not a.dry_run and el - last_report >= 10:
            last_report = el
            report_progress(a, el, totals)
        time.sleep(max(0.0, a.period - (time.time() - t0 - el)))
    print(f"Сценарій завершено: надіслано {totals.sent}, відхилено {totals.rejected}")


def main() -> None:
    a = parse_args()
    rng = random.Random(a.seed)
    configure_https(a)
    dashboard_login(a)
    sensors = select_sensors(fetch_sensors(a), a.only)
    bias = parse_bias(a.bias)
    keys = parse_keys(a.key, os.environ.get("DRONEWATCH_KEYS"))
    warn_about_keys(a, keys, sensors)
    pubs = open_publishers(a, sensors, keys)
    while True:
        run_scenario(a, rng, sensors, keys, pubs, bias)
        if not a.loop:
            break
    for conn in {id(c): c for c in pubs.values()}.values():
        conn.close()


if __name__ == "__main__":
    main()
