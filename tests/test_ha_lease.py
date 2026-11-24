"""Оренда лідера: автомат ролей Elector на керованому «часі» й фейковому сховищі."""
import pytest

from dronewatch.service import ha
from dronewatch.service.ha import Elector, FencedError, LeaseStoreError, Role


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


class FakeStore:
    """Оренда в пам'яті: ключ живе до `expires` за спільним годинником; `down` імітує недоступний Redis."""

    def __init__(self, clock: Clock) -> None:
        self.clock, self.holder, self.expires, self.counter, self.down = clock, None, 0.0, 0, False
        self.raised: list[int] = []
        self.released: list[tuple[str, int]] = []

    def _alive(self) -> bool:
        return self.holder is not None and self.clock() < self.expires

    def _check(self) -> None:
        if self.down:
            raise LeaseStoreError("redis недоступний")

    def acquire(self, owner: str, ttl_ms: int) -> int | None:
        self._check()
        self.counter += 1
        if self._alive():
            return None
        self.holder, self.expires = (owner, self.counter), self.clock() + ttl_ms / 1000
        return self.counter

    def renew(self, owner: str, epoch: int, ttl_ms: int) -> bool:
        self._check()
        if self._alive() and self.holder == (owner, epoch):
            self.expires = self.clock() + ttl_ms / 1000
            return True
        return False

    def release(self, owner: str, epoch: int) -> None:
        self._check()
        self.released.append((owner, epoch))
        if self.holder == (owner, epoch):
            self.holder = None

    def raise_epoch(self, at_least: int) -> None:
        self._check()
        self.raised.append(at_least)
        self.counter = max(self.counter, at_least)


def make(store, clock, owner="a", ttl=6.0, promoted=None, demoted=None, on_promote=None):
    promoted = [] if promoted is None else promoted
    demoted = [] if demoted is None else demoted
    return Elector(store, owner, ttl, clock=clock, on_promote=on_promote or promoted.append, on_demote=demoted.append)


def test_standby_acquires_when_free():
    c = Clock()
    promoted: list[int] = []
    e = make(FakeStore(c), c, promoted=promoted)
    e.step()
    assert e.role is Role.LEADER and e.epoch == 1 and promoted == [1] and e.is_valid()


def test_second_instance_stays_standby_while_held():
    c = Clock()
    s = FakeStore(c)
    a, b = make(s, c, "a"), make(s, c, "b")
    a.step()
    b.step()
    assert a.role is Role.LEADER and b.role is Role.STANDBY and b.epoch is None


def test_leader_renews_and_extends_deadline():
    c = Clock()
    e = make(FakeStore(c), c)
    e.step()
    c.t += 5.0
    e.step()
    c.t += 5.0   # без поновлення було б уже 10 с від старту (> ttl)
    assert e.role is Role.LEADER and e.is_valid()


def test_standby_takes_over_after_ttl():
    c = Clock()
    s = FakeStore(c)
    a, b = make(s, c, "a"), make(s, c, "b")
    a.step()
    c.t += 7.0   # a мовчить довше за ttl
    b.step()
    assert b.role is Role.LEADER and b.epoch == 2


def test_store_error_before_deadline_keeps_leader_then_degrades_after_deadline():
    c = Clock()
    s = FakeStore(c)
    demoted: list[Role] = []
    e = make(s, c, demoted=demoted)
    e.step()
    s.down = True
    c.t += 2.0
    e.step()
    assert e.role is Role.LEADER and e.is_valid() and demoted == []
    c.t += 5.0
    assert not e.is_valid()   # термін минув навіть без step()
    e.step()
    assert e.role is Role.DEGRADED and demoted == [Role.DEGRADED]


def test_degraded_regains_same_epoch_when_store_returns():
    c = Clock()
    s = FakeStore(c)
    promoted: list[int] = []
    e = make(s, c, promoted=promoted)
    e.step()
    s.down = True
    c.t += 7.0
    e.step()
    s.down = False
    s.expires = c.t + 3.0   # ключ ще живий і лишається за нами
    e.step()
    assert e.role is Role.LEADER and e.epoch == 1 and promoted == [1, 1] and e.is_valid()


def test_degraded_becomes_standby_if_other_holds():
    c = Clock()
    s = FakeStore(c)
    demoted: list[Role] = []
    a, b = make(s, c, "a", demoted=demoted), make(s, c, "b")
    a.step()
    s.down = True
    c.t += 7.0
    a.step()
    s.down = False
    b.step()   # a мовчав, оренда вільна
    a.step()
    assert b.role is Role.LEADER and a.role is Role.STANDBY and a.epoch is None
    assert demoted == [Role.DEGRADED, Role.STANDBY]


def test_is_valid_false_past_deadline_even_without_step():
    c = Clock()
    e = make(FakeStore(c), c)
    e.step()
    c.t += 6.01
    assert e.role is Role.LEADER and not e.is_valid()


def test_fenced_on_promote_releases_raises_epoch_and_retries():
    c = Clock()
    s = FakeStore(c)
    calls: list[int] = []

    def promote(epoch: int) -> None:
        calls.append(epoch)
        if len(calls) == 1:
            raise FencedError("файл має новіший epoch", stored=50)

    e = make(s, c, on_promote=promote)
    e.step()
    assert e.role is Role.STANDBY and s.raised == [50] and s.released == [("a", 1)]
    e.step()
    assert e.role is Role.LEADER and e.epoch > 50


def test_promote_failure_releases_lease_and_stays_standby():
    """Оренду взято, а відкрити сховище не вдалось (збій БД між acquire і activate): віддаємо оренду, щоб не чекати ttl."""
    c = Clock()
    s = FakeStore(c)
    attempts: list[int] = []

    def promote(epoch: int) -> None:
        attempts.append(epoch)
        if len(attempts) == 1:
            raise RuntimeError("сховище недоступне")

    e = make(s, c, on_promote=promote)
    e.step()
    assert e.role is Role.STANDBY and e.epoch is None and s.released == [("a", 1)]
    e.step()
    assert e.role is Role.LEADER and e.epoch == 2


def test_stop_releases_lease():
    c = Clock()
    s = FakeStore(c)
    e = make(s, c)
    e.step()
    e.stop()
    assert s.released == [("a", 1)] and e.role is Role.STANDBY


def test_fenced_error_carries_stored():
    assert FencedError("x", stored=7).stored == 7


def test_rejects_nonpositive_ttl():
    with pytest.raises(ValueError, match="ttl"):
        Elector(FakeStore(Clock()), "a", 0, on_promote=lambda e: None, on_demote=lambda r: None)


# ---------- Redis-реалізація ----------
class FakePubSub:
    def __init__(self) -> None:
        self.queue: list[dict] = []
        self.closed = False

    def subscribe(self, channel):
        self.channel = channel

    def get_message(self, timeout=0.0):
        return self.queue.pop(0) if self.queue else None

    def close(self):
        self.closed = True


class FakeRedis:
    """Мінімум Redis: set(nx, px), incr, get, eval за тотожністю скрипта з константою модуля; down імітує обрив."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.down = False
        self.published: list[tuple[str, str]] = []
        self.subscribers: list[list[dict]] = []

    def _check(self) -> None:
        if self.down:
            raise ConnectionError("redis://:secret@host down")

    def set(self, k, v, px=None, nx=False):
        self._check()
        if nx and k in self.data:
            return None
        self.data[k], self.ttl[k] = str(v), px
        return True

    def get(self, k):
        self._check()
        return self.data.get(k)

    def incr(self, k):
        self._check()
        self.data[k] = str(int(self.data.get(k, "0")) + 1)
        return int(self.data[k])

    def publish(self, channel, message):
        self._check()
        self.published.append((channel, message))
        for q in self.subscribers:
            q.append({"type": "message", "channel": channel, "data": message})
        return len(self.subscribers)

    def pubsub(self, ignore_subscribe_messages=False):
        self._check()
        ps = FakePubSub()
        self.subscribers.append(ps.queue)
        return ps

    def eval(self, script, numkeys, *args):
        self._check()
        key, rest = args[0], args[numkeys:]
        if script is ha._ACQUIRE_LUA:
            if key in self.data:
                return 0
            epoch = int(self.data.get(args[1], "0")) + 1
            self.data[args[1]] = str(epoch)
            self.data[key], self.ttl[key] = f"{rest[0]}:{epoch}", int(rest[1])
            return epoch
        if script is ha._RENEW_LUA:
            if self.data.get(key) == rest[0]:
                self.ttl[key] = int(rest[1])
                return 1
            return 0
        if script is ha._RELEASE_LUA:
            if self.data.get(key) == rest[0]:
                del self.data[key]
                return 1
            return 0
        if script is ha._RAISE_EPOCH_LUA:
            if int(self.data.get(key, "0")) < int(rest[0]):
                self.data[key] = str(rest[0])
            return 1
        raise AssertionError("невідомий скрипт")


def test_acquire_sets_value_and_epoch():
    r = FakeRedis()
    s = ha.RedisLeaseStore(r)
    assert s.acquire("a", 6000) == 1
    assert r.data["dw:leader"] == "a:1" and r.ttl["dw:leader"] == 6000 and r.data["dw:epoch"] == "1"


def test_failed_acquire_does_not_burn_epoch():
    r = FakeRedis()
    s = ha.RedisLeaseStore(r)
    assert s.acquire("a", 6000) == 1
    for _ in range(5):   # резерв пробує кожні ttl/3: лічильник не має рости
        assert s.acquire("b", 6000) is None
    assert r.data["dw:epoch"] == "1"


def test_renew_only_if_value_matches():
    r = FakeRedis()
    s = ha.RedisLeaseStore(r)
    s.acquire("a", 6000)
    assert s.renew("a", 1, 9000) is True and r.ttl["dw:leader"] == 9000
    assert s.renew("b", 1, 9000) is False
    assert s.renew("a", 2, 9000) is False


def test_release_only_if_value_matches():
    r = FakeRedis()
    s = ha.RedisLeaseStore(r)
    s.acquire("a", 6000)
    s.release("b", 1)
    assert "dw:leader" in r.data
    s.release("a", 1)
    assert "dw:leader" not in r.data


def test_raise_epoch_never_lowers():
    r = FakeRedis()
    s = ha.RedisLeaseStore(r)
    s.raise_epoch(50)
    assert r.data["dw:epoch"] == "50"
    s.raise_epoch(10)
    assert r.data["dw:epoch"] == "50"
    assert s.acquire("a", 6000) == 51


def test_redis_error_becomes_lease_store_error_without_password():
    r = FakeRedis()
    s = ha.RedisLeaseStore(r)
    r.down = True
    for call in (lambda: s.acquire("a", 1), lambda: s.renew("a", 1, 1), lambda: s.release("a", 1), lambda: s.raise_epoch(1)):
        with pytest.raises(LeaseStoreError) as e:
            call()
        assert "secret" not in str(e.value)
