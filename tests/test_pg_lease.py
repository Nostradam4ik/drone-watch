"""Оренда лідера й запобіжник epoch у PostgreSQL: один виграє, термін за годинником БД, старий лідер не пише."""
import threading
import time

import pytest

from dronewatch.service.engine import Engine, Zone
from dronewatch.service.ha import FencedError, LeaseStore
from dronewatch.service.pg_lease import PgLeaseStore
from dronewatch.service.schema import ensure_schema
from test_identification import Clock, make_engine


@pytest.fixture
def store(pg_store):
    db = pg_store.connect()
    ensure_schema(db)
    db.close()
    return pg_store


def expire_lease(store) -> None:
    db = store.connect()
    db.execute("UPDATE ha_lease SET expires_at = to_timestamp(0)")
    db.commit()
    db.close()


def make_standby() -> Engine:
    clock = Clock()
    return Engine(make_engine(clock).sensors.values(), [Zone("z", 600, "WARNING")], clock=clock, min_age_s=0.0,
                  db_path=":memory:", standby=True)


def test_is_a_lease_store(store):
    lease: LeaseStore = PgLeaseStore(store)
    assert lease.acquire("a", 1000) == 1


def test_acquire_gives_epoch_above_ha_epoch(store):
    db = store.connect()
    db.execute("INSERT INTO ha_epoch (id, epoch) VALUES (1, 7)")
    db.commit()
    assert PgLeaseStore(store).acquire("a", 5000) == 8


def test_acquire_bumps_ha_epoch_atomically(store):
    PgLeaseStore(store).acquire("a", 5000)
    db = store.connect()
    assert db.execute("SELECT epoch FROM ha_epoch").fetchone()[0] == 1


def test_second_acquire_while_valid_returns_none(store):
    lease = PgLeaseStore(store)
    assert lease.acquire("a", 5000) == 1
    assert PgLeaseStore(store).acquire("b", 5000) is None


def test_failed_acquire_does_not_burn_epoch(store):
    lease = PgLeaseStore(store)
    lease.acquire("a", 5000)
    for _ in range(5):
        PgLeaseStore(store).acquire("b", 5000)
    expire_lease(store)
    assert PgLeaseStore(store).acquire("b", 5000) == 2


def _race(a: PgLeaseStore, b: PgLeaseStore) -> dict[str, int | None]:
    barrier, out = threading.Barrier(2), {}

    def run(name: str, lease: PgLeaseStore) -> None:
        barrier.wait()
        out[name] = lease.acquire(name, 5000)

    ts = [threading.Thread(target=run, args=("a", a)), threading.Thread(target=run, args=("b", b))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out


def test_two_concurrent_acquires_one_wins(store):
    a, b = PgLeaseStore(store), PgLeaseStore(store)
    for _ in range(20):
        expire_lease(store)
        out = _race(a, b)
        assert sorted(v is None for v in out.values()) == [False, True], out


def test_expiry_uses_db_clock(store):
    lease = PgLeaseStore(store)
    assert lease.acquire("a", 300) == 1
    assert PgLeaseStore(store).acquire("b", 300) is None
    time.sleep(0.5)
    assert PgLeaseStore(store).acquire("b", 300) == 2


def test_renew_rules(store):
    lease = PgLeaseStore(store)
    epoch = lease.acquire("a", 5000)
    assert epoch is not None
    assert lease.renew("a", epoch, 5000) is True
    assert lease.renew("b", epoch, 5000) is False
    assert lease.renew("a", epoch + 1, 5000) is False
    expire_lease(store)
    assert lease.renew("a", epoch, 5000) is False   # прострочену не продовжуємо


def test_release_then_acquire_increments_epoch(store):
    lease = PgLeaseStore(store)
    e1 = lease.acquire("a", 5000)
    assert e1 is not None
    lease.release("a", e1)
    assert PgLeaseStore(store).acquire("b", 5000) == e1 + 1


def test_release_wrong_owner_keeps_lease(store):
    lease = PgLeaseStore(store)
    e1 = lease.acquire("a", 5000)
    assert e1 is not None
    lease.release("b", e1)
    assert PgLeaseStore(store).acquire("c", 5000) is None


def test_raise_epoch_never_lowers(store):
    lease = PgLeaseStore(store)
    lease.raise_epoch(10)
    lease.raise_epoch(4)
    assert lease.acquire("a", 5000) == 11


def test_lease_error_has_no_secret():
    from dronewatch.service.ha import LeaseStoreError
    from dronewatch.service.store import PostgresStore
    lease = PgLeaseStore(PostgresStore("postgresql://u@127.0.0.1:1/secretdb", "s3cretpw", 1, 1))
    with pytest.raises(LeaseStoreError) as e:
        lease.acquire("a", 1000)
    assert "s3cretpw" not in repr(e.value) and "secretdb" not in repr(e.value) and "127.0.0.1" not in repr(e.value)


def _incident(eng: Engine) -> int:
    iid = eng.db.insert_id("INSERT INTO incidents (ts, track_id, level, zone, x, y, z, speed, sensors) "
                           "VALUES (1, 2, 'WARNING', 'z', 0, 0, 0, 5, 'r')")
    eng.db.commit()
    return iid


def test_old_leader_fenced_on_write(store):
    a_lease, b_lease = PgLeaseStore(store), PgLeaseStore(store)
    eng_a, eng_b = make_standby(), make_standby()
    e1 = a_lease.acquire("a", 100)
    assert e1 == 1
    eng_a.activate(store, e1)
    time.sleep(0.25)
    e2 = b_lease.acquire("b", 5000)
    assert e2 == 2
    eng_b.activate(store, e2)
    iid = _incident(eng_b)
    with pytest.raises(FencedError):
        eng_a.review_incident(iid, "confirmed", "mallory")
    assert eng_b.db.execute("SELECT count(*) FROM incident_reviews").fetchone()[0] == 0
    assert eng_b.db.execute("SELECT status FROM incidents WHERE id = ?", (iid,)).fetchone()[0] in (None, "new")
    assert eng_a.active is False


def test_new_leader_waits_for_inflight_write(store):
    a_lease = PgLeaseStore(store)
    eng_a = make_standby()
    e1 = a_lease.acquire("a", 100)
    assert e1 is not None
    eng_a.activate(store, e1)
    time.sleep(0.25)
    with eng_a.lock:
        eng_a._fence_locked()   # транзакція лідера A тримає FOR SHARE на ha_epoch до commit
        out: dict[str, int | None] = {}
        t = threading.Thread(target=lambda: out.setdefault("e", PgLeaseStore(store).acquire("b", 5000)))
        t.start()
        t.join(0.4)
        assert t.is_alive(), "перехоплення оренди мало чекати завершення запису старого лідера"
        eng_a.db.commit()
    t.join(3)
    assert out["e"] == 2


def test_public_fence_releases_lock(store):
    a_lease = PgLeaseStore(store)
    eng_a = make_standby()
    e1 = a_lease.acquire("a", 100)
    assert e1 is not None
    eng_a.activate(store, e1)
    eng_a.fence()   # самостійна перевірка не повинна лишати блокування
    time.sleep(0.25)
    started = time.monotonic()
    assert PgLeaseStore(store).acquire("b", 5000) == 2
    assert time.monotonic() - started < 1.5


def test_review_missing_incident_releases_fence_lock(store):
    """Регресія: вихід `return None` після перевірки epoch не повинен лишати FOR SHARE (перехоплення оренди зависло б)."""
    a_lease = PgLeaseStore(store)
    eng_a = make_standby()
    e1 = a_lease.acquire("a", 100)
    assert e1 is not None
    eng_a.activate(store, e1)
    assert eng_a.review_incident(999, "confirmed", "op") is None
    time.sleep(0.25)
    started = time.monotonic()
    assert PgLeaseStore(store).acquire("b", 5000) == 2
    assert time.monotonic() - started < 1.5


def test_review_error_releases_fence_lock(store, monkeypatch):
    a_lease = PgLeaseStore(store)
    eng_a = make_standby()
    e1 = a_lease.acquire("a", 100)
    assert e1 is not None
    eng_a.activate(store, e1)
    iid = _incident(eng_a)
    orig = eng_a.db.execute

    def boom(sql, params=()):
        if sql.startswith("INSERT INTO incident_reviews"):
            raise RuntimeError("boom")
        return orig(sql, params)

    monkeypatch.setattr(eng_a.db, "execute", boom)
    with pytest.raises(RuntimeError):
        eng_a.review_incident(iid, "confirmed", "op")
    monkeypatch.undo()
    time.sleep(0.25)
    started = time.monotonic()
    assert PgLeaseStore(store).acquire("b", 5000) == 2
    assert time.monotonic() - started < 1.5


def test_no_connection_is_left_idle_in_transaction_after_activation_and_a_scan(store):
    """idle_in_transaction_session_timeout (60 с) убив би таке з'єднання й дав зайву аварію: читання під час активації мають закриватись."""
    lease = PgLeaseStore(store)
    eng = make_standby()
    epoch = lease.acquire("a", 5000)
    assert epoch is not None
    eng.activate(store, epoch)
    eng.tick()
    eng.incident_stats()
    eng.audit.read(5)
    eng.audit.verify()
    probe = store.connect()
    idle = probe.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                         "AND state = 'idle in transaction' AND pid <> pg_backend_pid()").fetchone()[0]
    assert idle == 0
