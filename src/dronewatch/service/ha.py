"""Активний рушій і гарячий резерв: оренда лідера в Redis, автомат ролей, запобіжник `epoch`, релей HTTP-подій.

Цей модуль не імпортує рушій (рушій імпортує звідси винятки): циклу імпортів нема. Redis-клієнт сюди передається готовий,
пакет redis тут не імпортується.

Принцип: пише лише той, у кого чинна оренда. Лідер сам відраховує свій термін за монотонним годинником від ПОЧАТКУ запиту на
поновлення, тож він перестає вважати себе лідером раніше, ніж ключ може перехопити інший. Старий лідер, що ожив, зупиняється
на запобіжнику: `epoch` зростає при кожному захопленні й записаний у самому файлі БД (див. Engine.activate)."""
import contextlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

log = logging.getLogger("dronewatch.ha")


class LeaseStoreError(Exception):
    """Збій сховища оренди (Redis недоступний, таймаут, помилка). Без тексту першоджерела: у ньому може бути URL із паролем."""


class NotActiveError(RuntimeError):
    """Запис чи дія оператора, коли в цієї копії немає чинної оренди."""


class FencedError(RuntimeError):
    """У файлі БД уже новіший epoch: хтось пізніший став лідером, а ми застаріли. `stored` це epoch із файлу."""

    def __init__(self, message: str, *, stored: int) -> None:
        super().__init__(message)
        self.stored = stored


class Role(StrEnum):
    STANDBY = "standby"     # резерв: рахує треки, нічого не пише й не показує людям
    LEADER = "leader"       # чинна оренда: пише й показує
    DEGRADED = "degraded"   # був лідером, оренду підтвердити не вдається: тривоги показує, змін не робить


class LeaseStore(Protocol):
    def acquire(self, owner: str, ttl_ms: int) -> int | None:
        """epoch, якщо оренду взято; None, якщо тримає інший. Збій: LeaseStoreError."""

    def renew(self, owner: str, epoch: int, ttl_ms: int) -> bool:
        """True, якщо оренда ще наша (і продовжена)."""

    def release(self, owner: str, epoch: int) -> None: ...

    def raise_epoch(self, at_least: int) -> None:
        """Лічильник epoch не нижче `at_least` (Redis могли перезапустити й обнулити)."""


PREFIX = "dw:"
# Порівняння й зміна ключа атомарно: інакше між GET і SET ключ міг би спливти й дістатися іншому, а ми б його затерли.
_RENEW_LUA = "if redis.call('get', KEYS[1]) == ARGV[1] then redis.call('pexpire', KEYS[1], ARGV[2]) return 1 else return 0 end"
_RELEASE_LUA = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
_RAISE_EPOCH_LUA = ("local cur = tonumber(redis.call('get', KEYS[1]) or '0') "
                    "if cur < tonumber(ARGV[1]) then redis.call('set', KEYS[1], ARGV[1]) end return 1")


class RedisLeaseStore:
    """Оренда в Redis: `dw:leader` = `<owner>:<epoch>`, `dw:epoch` = лічильник. Будь-який збій клієнта це LeaseStoreError
    (fail-closed, як у dashboard_redis), без тексту помилки: у ньому може бути URL із паролем."""

    def __init__(self, client: Any, *, prefix: str = PREFIX) -> None:
        self._r, self._leader, self._epoch = client, prefix + "leader", prefix + "epoch"

    def acquire(self, owner: str, ttl_ms: int) -> int | None:
        try:
            epoch = int(self._r.incr(self._epoch))   # лічильник росте і при невдалій спробі: epoch лише зростає
            return epoch if self._r.set(self._leader, f"{owner}:{epoch}", px=ttl_ms, nx=True) else None
        except Exception as e:
            raise LeaseStoreError(type(e).__name__) from None

    def renew(self, owner: str, epoch: int, ttl_ms: int) -> bool:
        try:
            return bool(self._r.eval(_RENEW_LUA, 1, self._leader, f"{owner}:{epoch}", ttl_ms))
        except Exception as e:
            raise LeaseStoreError(type(e).__name__) from None

    def release(self, owner: str, epoch: int) -> None:
        try:
            self._r.eval(_RELEASE_LUA, 1, self._leader, f"{owner}:{epoch}")
        except Exception as e:
            raise LeaseStoreError(type(e).__name__) from None

    def raise_epoch(self, at_least: int) -> None:
        try:
            self._r.eval(_RAISE_EPOCH_LUA, 1, self._epoch, at_least)
        except Exception as e:
            raise LeaseStoreError(type(e).__name__) from None


CHANNEL = "dw:events"
MAX_RELAY_EVENTS = 2000   # пакет понад це підписник відкидає (HTTP-пакет лідера не більший за 1000)
LOG_EVERY_S = 30.0


class RedisEventRelay:
    """Дзеркалення HTTP-подій лідера в резерв (best-effort, як MQTT QoS 0: застарілі виміри нікому не потрібні). Події йдуть БЕЗ
    ключів датчиків і вже автентифіковані лідером; Redis має бути закритою внутрішньою мережею (як і для сесій)."""

    def __init__(self, client: Any, *, channel: str = CHANNEL, clock: Callable[[], float] = time.monotonic) -> None:
        self._r, self.channel, self._clock, self._last_log = client, channel, clock, float("-inf")

    def _warn(self, what: str) -> None:
        if self._clock() - self._last_log >= LOG_EVERY_S:   # не засмічуємо лог на кожен пакет під час аварії Redis
            self._last_log = self._clock()
            log.warning("HA релей: %s", what)

    def publish(self, events: list[dict[str, Any]]) -> None:
        try:
            self._r.publish(self.channel, json.dumps(events, separators=(",", ":")))
        except Exception as e:   # релей лише допомога резерву: збій не повинен ламати прийом подій
            self._warn(f"не вдалося опублікувати ({type(e).__name__})")

    def subscribe(self, handler: Callable[[list[dict[str, Any]]], None], stop: threading.Event) -> None:
        """Цикл підписника до `stop`. Сміття (не JSON, не список, не обʼєкти) мовчки відкидається, збій Redis перечікується."""
        ps = None
        try:
            while not stop.is_set():
                try:
                    if ps is None:
                        ps = self._r.pubsub(ignore_subscribe_messages=True)
                        ps.subscribe(self.channel)
                    msg = ps.get_message(timeout=1.0)
                except Exception as e:
                    self._warn(f"підписка перервана ({type(e).__name__})")
                    ps = None
                    stop.wait(1.0)
                    continue
                if msg is None:
                    stop.wait(0.01)
                    continue
                events = self._decode(msg.get("data"))
                if events:
                    try:
                        handler(events)
                    except Exception:
                        log.exception("HA релей: обробник події впав")
        finally:
            if ps is not None:
                with contextlib.suppress(Exception):
                    ps.close()

    @staticmethod
    def _decode(data: Any) -> list[dict[str, Any]]:
        try:
            parsed = json.loads(data)
        except (TypeError, ValueError):
            return []
        if not isinstance(parsed, list) or len(parsed) > MAX_RELAY_EVENTS:
            return []
        return [e for e in parsed if isinstance(e, dict)]


class Elector:
    """Автомат ролей. `step()` викликається кожні ttl/3 з окремого потоку; усі рішення про час приймаються тут."""

    def __init__(self, store: LeaseStore, owner: str, ttl_s: float, *, clock: Callable[[], float] = time.monotonic,
                 on_promote: Callable[[int], None], on_demote: Callable[[Role], None]) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl має бути додатним")
        self.store, self.owner, self.ttl_s, self.clock = store, owner, ttl_s, clock
        self.on_promote, self.on_demote = on_promote, on_demote
        self.role = Role.STANDBY
        self.epoch: int | None = None
        self._valid_until = 0.0

    @property
    def _ttl_ms(self) -> int:
        return int(self.ttl_s * 1000)

    def is_valid(self) -> bool:
        """Чинна оренда саме зараз за нашим годинником (не за останнім step): працює й тоді, коли потік поновлення завис."""
        return self.role is Role.LEADER and self.clock() < self._valid_until

    def step(self) -> None:
        if self.role is Role.LEADER:
            self._renew_leader()
        elif self.role is Role.DEGRADED:
            self._retry_degraded()
        else:
            self._try_acquire()

    def _renew_leader(self) -> None:
        t0 = self.clock()
        assert self.epoch is not None
        try:
            ok = self.store.renew(self.owner, self.epoch, self._ttl_ms)
        except LeaseStoreError:
            if self.clock() >= self._valid_until:   # термін сплив, підтвердити не можемо: більше не лідер
                log.error("HA: оренду підтвердити не вдалося, термін минув: DEGRADED (тривоги показую, змін не роблю)")
                self.role = Role.DEGRADED
                self.on_demote(Role.DEGRADED)
            else:
                log.warning("HA: Redis недоступний, оренда ще чинна %.1f с", self._valid_until - self.clock())
            return
        if ok:
            self._valid_until = t0 + self.ttl_s
        else:
            log.error("HA: оренду перехопив інший (epoch %s): стаю резервом", self.epoch)
            self.epoch, self.role = None, Role.STANDBY
            self.on_demote(Role.STANDBY)

    def _retry_degraded(self) -> None:
        t0 = self.clock()
        assert self.epoch is not None
        try:
            ok = self.store.renew(self.owner, self.epoch, self._ttl_ms)
        except LeaseStoreError:
            return
        if not ok:
            log.error("HA: поки Redis не відповідав, лідером став інший: стаю резервом")
            self.epoch, self.role = None, Role.STANDBY
            self.on_demote(Role.STANDBY)
            return
        self._promote(self.epoch, t0)

    def _try_acquire(self) -> None:
        t0 = self.clock()
        try:
            epoch = self.store.acquire(self.owner, self._ttl_ms)
        except LeaseStoreError:
            return
        if epoch is not None:
            self._promote(epoch, t0)

    def _promote(self, epoch: int, t0: float) -> None:
        try:
            self.on_promote(epoch)
        except FencedError as e:
            # Файл пам'ятає новіший epoch (Redis обнулили?). Віддаємо оренду: інакше NX-захоплення блокувала б наша ж оренда
            # до кінця ttl, і піднімаємо лічильник, щоб наступне захоплення дало більший epoch.
            log.error("HA: у файлі БД epoch %d новіший за мій %d: піднімаю лічильник", e.stored, epoch)
            self.epoch, self.role = None, Role.STANDBY
            try:
                self.store.release(self.owner, epoch)
                self.store.raise_epoch(e.stored)
            except LeaseStoreError:
                log.exception("HA: не вдалося підняти лічильник epoch")
            return
        self.epoch, self.role, self._valid_until = epoch, Role.LEADER, t0 + self.ttl_s
        log.warning("HA: я лідер, epoch %d", epoch)

    def stop(self) -> None:
        """Чистий вихід: звільнити оренду, щоб резерв не чекав ttl."""
        if self.epoch is not None:
            try:
                self.store.release(self.owner, self.epoch)
            except LeaseStoreError:
                log.warning("HA: не вдалося звільнити оренду при виході, резерв чекатиме ttl")
        self.epoch, self.role = None, Role.STANDBY


def run_forever(elector: Elector, stop: threading.Event) -> None:
    """Тіло потоку поновлення: step кожні ttl/3 (не рідше за 0.5 с). Збій одного кроку не вбиває потік."""
    interval = max(0.5, elector.ttl_s / 3)
    while not stop.is_set():
        try:
            elector.step()
        except Exception:
            log.exception("HA: збій кроку оренди")
        stop.wait(interval)


@dataclass
class HaState:
    """Усе, що сервіс тримає для HA: автомат ролей, потік поновлення й ім'я цієї копії."""
    elector: Elector
    instance_id: str
    relay: RedisEventRelay | None = None
    on_relay: Callable[[list[dict[str, Any]]], None] | None = None   # куди резерв віддає події з релея
    stop: threading.Event = field(default_factory=threading.Event)
    threads: list[threading.Thread] = field(default_factory=list)

    def start(self) -> None:
        self.threads = [threading.Thread(target=run_forever, args=(self.elector, self.stop), name="dw-ha", daemon=True)]
        if self.relay is not None and self.on_relay is not None:
            self.threads.append(threading.Thread(target=self.relay.subscribe, args=(self.on_relay, self.stop), name="dw-ha-relay",
                                                 daemon=True))
        for t in self.threads:
            t.start()

    def shutdown(self) -> None:
        self.stop.set()
        for t in self.threads:
            t.join(timeout=5)
        self.elector.stop()
