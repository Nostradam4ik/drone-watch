#!/usr/bin/env python3
"""Емулятор датчиків Drone Watch: шле події на POST /api/events так, як це робив би справжній пристрій.

Лише стандартна бібліотека Python 3.8+. Датчики (позиції, дальності) береться з GET /api/config сервісу,
тож емулятор завжди збігається з його конфігурацією.

Приклади:
    python emulate.py --scenario cross                       # один дрон перетинає об'єкт
    python emulate.py --scenario swarm --count 8             # рій із 8 дронів
    python emulate.py --scenario hover --only radar1,rf1     # без акустики (імітація відмови датчика)
    python emulate.py --scenario cross --bias rf1=3          # RF «довертає» на 3° (перевірка калібрування)
    python emulate.py --scenario cross --dry-run             # лише показати події, нічого не слати

Heartbeat: кожен скан кожен справний датчик шле {"sensor_id": ..., "heartbeat": true}, навіть коли нічого не бачить.
Так сервіс відрізняє «небо порожнє» від «датчик відмовив». Вимкнути: --no-heartbeat.

Формат події (азимут компасний: від півночі за годинниковою стрілкою):
    {"sensor_id": "radar1", "ts": 1760000000.5, "azimuth_deg": 212.0, "range_m": 640.0, "elevation_deg": 4.5}
"""
import argparse
import json
import math
import random
import sys
import time
import urllib.error
import urllib.request

DEFAULT_NOISE = {"radar": (5.0, 0.7, 1.5), "rf": (None, 5.0, None), "acoustic": (None, 10.0, None)}  # м, °, °
FALLBACK_SENSORS = [  # якщо сервіс недоступний (--dry-run без сервера)
    {"id": "radar1", "kind": "radar", "label": "radar", "pos": [0, 0], "max_range": 1500},
    {"id": "rf1", "kind": "bearing", "label": "rf", "pos": [400, -300], "max_range": 1800},
    {"id": "ac1", "kind": "bearing", "label": "acoustic", "pos": [-300, 200], "max_range": 700},
]


# ---------- цілі ----------
class Target:
    """Рух по вершинах [(t, x, y, z), ...] з лінійною інтерполяцією."""

    def __init__(self, name, wp):
        self.name, self.wp = name, wp

    def active(self, t):
        return self.wp[0][0] <= t <= self.wp[-1][0]

    def pos(self, t):
        for a, b in zip(self.wp, self.wp[1:]):
            if a[0] <= t <= b[0]:
                f = (t - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0.0
                return tuple(a[k] + f * (b[k] - a[k]) for k in (1, 2, 3))
        return tuple(self.wp[-1][1:])


def scenario_cross(rng, count):
    return [Target("cross", [(0, -900, 100, 70), (120, 900, 100, 70)])]


def scenario_hover(rng, count):
    return [Target("hover", [(0, 600, 700, 60), (30, 250, 250, 60), (60, 250, 250, 60), (80, 800, 600, 120)])]


def scenario_circle(rng, count):
    r, v, n = 400.0, 12.0, 36
    period = 2 * math.pi * r / v
    return [Target("circle", [(period * k / n, r * math.cos(2 * math.pi * k / n), r * math.sin(2 * math.pi * k / n), 80.0)
                              for k in range(n + 1)])]


def scenario_swarm(rng, count):
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


SCENARIOS = {"cross": scenario_cross, "hover": scenario_hover, "circle": scenario_circle, "swarm": scenario_swarm}


# ---------- датчики ----------
def compass(dx, dy):
    return math.degrees(math.atan2(dx, dy)) % 360.0


def poisson(rng, lam):
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def measure(rng, sensor, pos, bias, noise):
    """Одне вимірювання цілі датчиком: істина + bias + шум."""
    dx, dy, dz = pos[0] - sensor["pos"][0], pos[1] - sensor["pos"][1], pos[2]
    rng_h = math.hypot(dx, dy)
    sr, sa, se = noise
    b = bias.get(sensor["id"], (0.0, 0.0, 0.0))
    e = {"azimuth_deg": (compass(dx, dy) + b[1] + rng.gauss(0, sa)) % 360.0}
    if sensor["kind"] == "radar":
        e["range_m"] = math.hypot(rng_h, dz) + b[0] + rng.gauss(0, sr)
        e["elevation_deg"] = math.degrees(math.atan2(dz, rng_h)) + b[2] + rng.gauss(0, se)
    return e


def clutter(rng, sensor):
    e = {"azimuth_deg": rng.uniform(0, 360)}
    if sensor["kind"] == "radar":
        e["range_m"] = rng.uniform(50, sensor["max_range"])
        e["elevation_deg"] = rng.uniform(0, 30)
    return e


# ---------- мережа ----------
class MqttPublisher:
    """Мінімальний клієнт MQTT 3.1.1 (лише CONNECT і PUBLISH QoS 0), щоб емулятор лишався без залежностей."""

    def __init__(self, host, port=1883, username=None, password=None, client_id="dronewatch-emulator"):
        import socket
        self.sock = socket.create_connection((host, port), timeout=5)
        flags = 0x02  # clean session
        payload = self._str(client_id)
        if username:
            flags |= 0x80 | (0x40 if password else 0)
            payload += self._str(username) + (self._str(password) if password else b"")
        var = self._str("MQTT") + bytes([4, flags]) + (60).to_bytes(2, "big")
        self._send(0x10, var + payload)
        ack = self.sock.recv(4)
        if len(ack) < 4 or ack[0] != 0x20 or ack[3] != 0:
            raise OSError(f"брокер відхилив підключення (CONNACK {ack.hex()})")

    @staticmethod
    def _str(s):
        b = s.encode("utf-8")
        return len(b).to_bytes(2, "big") + b

    def _send(self, first, body):
        n, enc = len(body), bytearray()
        while True:  # довжина тіла змінної довжини (по 7 біт)
            byte, n = n % 128, n // 128
            enc.append(byte | (0x80 if n else 0))
            if not n:
                break
        self.sock.sendall(bytes([first]) + bytes(enc) + body)

    def publish(self, topic, obj):
        self._send(0x30, self._str(topic) + json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def close(self):
        try:
            self._send(0xE0, b"")
            self.sock.close()
        except OSError:
            pass


def http(url, data=None, timeout=5):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def parse_bias(items):
    """--bias rf1=3  або  --bias radar1=8,1,0.5  (дальність м, азимут °, елевація °)."""
    out = {}
    for item in items or []:
        sid, _, vals = item.partition("=")
        v = [float(x) for x in vals.split(",")]
        if len(v) not in (1, 3):
            sys.exit(f"--bias {item}: потрібно одне число (азимут, °) або три (дальність м, азимут °, елевація °)")
        out[sid] = (0.0, v[0], 0.0) if len(v) == 1 else tuple(v)
    return out


def main():
    ap = argparse.ArgumentParser(description="Емулятор датчиків Drone Watch", formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("Приклади:")[1])
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--scenario", choices=SCENARIOS, default="cross")
    ap.add_argument("--count", type=int, default=8, help="кількість дронів (для swarm)")
    ap.add_argument("--duration", type=float, default=0, help="тривалість, с (0 = до кінця сценарію)")
    ap.add_argument("--period", type=float, default=0.5, help="як часто датчики видають вимірювання, с")
    ap.add_argument("--pd", type=float, default=0.85, help="ймовірність, що датчик побачить ціль за скан")
    ap.add_argument("--clutter", type=float, default=0.3, help="середня кількість хибних детекцій за скан на датчик")
    ap.add_argument("--only", help="емулювати лише ці датчики (через кому), решта «відмовила»")
    ap.add_argument("--bias", action="append", help="систематична похибка: rf1=3 або radar1=8,1,0.5")
    ap.add_argument("--no-heartbeat", action="store_true", help="не слати heartbeat (поведінка старих датчиків)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--loop", action="store_true", help="повторювати сценарій безкінечно")
    ap.add_argument("--mqtt", metavar="HOST[:PORT]", help="слати події в MQTT-брокер замість HTTP")
    ap.add_argument("--topic", default="dronewatch/events", help="префікс топіка MQTT; події йдуть у <префікс>/<sensor_id>")
    ap.add_argument("--mqtt-user")
    ap.add_argument("--mqtt-password")
    ap.add_argument("--dry-run", action="store_true", help="друкувати події, не надсилаючи")
    a = ap.parse_args()

    rng = random.Random(a.seed)
    try:
        sensors = http(a.url + "/api/config")["sensors"]
        print(f"Підключено до {a.url}, датчиків: {[s['id'] for s in sensors]}")
    except (urllib.error.URLError, OSError) as e:
        if not a.dry_run:
            sys.exit(f"Сервіс недоступний ({a.url}): {e}\nЗапустіть його або використайте --dry-run.")
        sensors = FALLBACK_SENSORS
        print("Сервіс недоступний, використовую вбудовані датчики (dry-run).")
    if a.only:
        keep = set(a.only.split(","))
        unknown = keep - {s["id"] for s in sensors}
        if unknown:
            sys.exit(f"Невідомі датчики в --only: {sorted(unknown)}")
        sensors = [s for s in sensors if s["id"] in keep]
    bias = parse_bias(a.bias)
    pub = None
    if a.mqtt and not a.dry_run:
        host, _, port = a.mqtt.partition(":")
        try:
            pub = MqttPublisher(host, int(port or 1883), a.mqtt_user, a.mqtt_password)
        except OSError as e:
            sys.exit(f"MQTT-брокер недоступний ({a.mqtt}): {e}")
        print(f"Події йдуть у MQTT {a.mqtt}, топіки {a.topic}/<sensor_id>")

    while True:
        targets = SCENARIOS[a.scenario](rng, a.count)
        end = a.duration or max(t.wp[-1][0] for t in targets)
        print(f"Сценарій «{a.scenario}»: цілей {len(targets)}, {end:.0f} с")
        t0, sent, rejected, last_report = time.time(), 0, 0, 0.0
        while (el := time.time() - t0) <= end:
            events = []
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
            if a.dry_run:
                for e in events:
                    print(json.dumps(e, ensure_ascii=False))
            elif events:
                try:
                    if pub:
                        for s in sensors:  # одна публікація на датчик за скан (масив подій)
                            batch = [e for e in events if e["sensor_id"] == s["id"]]
                            if batch:
                                pub.publish(f"{a.topic}/{s['id']}", batch)
                        sent += len(events)
                    else:
                        r = http(a.url + "/api/events", events)
                        sent += r["accepted"]
                        rejected += r["rejected"]
                except (urllib.error.URLError, OSError) as e:
                    print(f"  помилка відправки: {e}")
            if not a.dry_run and el - last_report >= 10:
                last_report = el
                try:
                    st = http(a.url + "/api/state")
                    print(f"  t={el:5.0f}с  надіслано={sent}  відхилено={rejected}  треків у сервісі={len([t for t in st['tracks'] if not t['coasting']])}")
                except (urllib.error.URLError, OSError):
                    pass
            time.sleep(max(0.0, a.period - (time.time() - t0 - el)))
        print(f"Сценарій завершено: надіслано {sent}, відхилено {rejected}")
        if not a.loop:
            break
    if pub:
        pub.close()


if __name__ == "__main__":
    main()
