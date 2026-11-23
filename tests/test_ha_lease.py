"""Оренда лідера: автомат ролей Elector на керованому «часі» й фейковому сховищі."""
import pytest

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
