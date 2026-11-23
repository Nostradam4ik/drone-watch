"""Перемикання лідера наскрізь: два рушії, спільна оренда й файл БД, ті самі події в обох."""
import random
import sqlite3
from pathlib import Path

import pytest

from dronewatch.service.engine import Engine, Zone
from dronewatch.service.ha import Elector, FencedError, Role
from test_ha_lease import Clock as LeaseClock
from test_ha_lease import FakeStore
from test_identification import Clock, events_for, make_engine


class Node:
    """Копія сервісу без HTTP: рушій у режимі резерву + автомат ролей зі склейкою, як у app.build_ha."""

    def __init__(self, name: str, store: FakeStore, eclock: LeaseClock, db: str) -> None:
        self.clock = Clock()
        self.engine = Engine(make_engine(self.clock).sensors.values(), [Zone("z", 600, "WARNING")], clock=self.clock,
                             min_age_s=0.0, db_path=db, standby=True)
        self.elector = Elector(store, name, 6.0, clock=eclock, on_promote=lambda ep: self.engine.activate(db, ep),
                               on_demote=lambda role: self.engine.deactivate())
        self.rng = random.Random(3)
        self.i = 0

    def scan(self) -> None:
        self.engine.ingest(events_for(self.clock, [(-100 + 2.5 * self.i, 100, 50)], self.rng, ["geran-2"]))
        self.engine.tick()
        self.clock.now += 0.5
        self.i += 1


def incidents(db) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT id, zone, level, failover FROM incidents ORDER BY id").fetchall()
    finally:
        con.close()


@pytest.fixture
def cluster(tmp_path):
    eclock = LeaseClock()
    store = FakeStore(eclock)
    db = str(tmp_path / "ha.db")
    return Node("a", store, eclock, db), Node("b", store, eclock, db), eclock, store, db


def test_leader_dies_standby_takes_over_without_new_alerts(cluster):
    a, b, eclock, _, db = cluster
    a.elector.step()
    b.elector.step()
    assert a.elector.role is Role.LEADER and b.elector.role is Role.STANDBY
    for _ in range(20):   # обидва бачать ті самі події; тривога піднімається в обох, пише лише лідер
        a.scan()
        b.scan()
    before = incidents(db)
    assert before and all(f is None for *_, f in before) and b.engine.pending
    eclock.t += 7.0   # a мовчить довше за ttl
    b.elector.step()
    assert b.elector.role is Role.LEADER and b.elector.epoch > a.elector.epoch and b.engine.tracker.confirmed()
    assert len(incidents(db)) == len(before)   # дублів від резерву нема
    for _ in range(10):
        b.scan()
    assert len(incidents(db)) == len(before)   # нових тривог теж нема: стан max_level уже гарячий


def test_alert_raised_during_gap_is_written_as_failover(cluster):
    _, b, _, _, db = cluster
    for _ in range(16):   # лідера ще нема, резерв уже бачить тривогу
        b.scan()
    assert not Path(db).exists()   # резерв нічого не пише у файл
    b.elector.step()
    got = incidents(db)
    assert got and all(f == 1 for *_, f in got)


def test_old_leader_is_fenced_after_takeover(cluster):
    a, b, eclock, _, db = cluster
    a.elector.step()
    for _ in range(16):
        a.scan()
        b.scan()
    first = incidents(db)[0][0]
    eclock.t += 7.0
    b.elector.step()   # b став лідером (новіший epoch), a про це ще не знає
    assert a.engine.active is True
    with pytest.raises(FencedError):
        a.engine.review_incident(first, "confirmed", "op")
    assert a.engine.active is False
    assert b.engine.review_incident(first, "confirmed", "op")["status"] == "confirmed"
    assert b.engine.audit.verify().ok


def test_redis_restart_resets_epoch_but_cluster_recovers(cluster):
    a, _, _, store, db = cluster
    Engine(make_engine(Clock()).sensors.values(), [], db_path=":memory:", standby=True).activate(db, 7)   # у файлі вже 7
    store.counter = 0   # Redis перезапустили, лічильник обнулився
    for _ in range(4):
        a.elector.step()
        if a.elector.role is Role.LEADER:
            break
    assert a.elector.role is Role.LEADER and a.elector.epoch > 7 and a.engine.active
