"""Датчик публікує в обидва брокери (по одному на вузол): рушій отримує кожну подію двічі. Дубль не повинен давати зайві треки,
тривоги чи рядки журналу, а резерв і лідер мають сходитись після підвищення."""
import random

from dronewatch.service.engine import Engine, Zone
from test_ha_engine import make
from test_identification import Clock, events_for, make_engine


def run(duplicates: int, scans: int = 18):
    clock = Clock()
    eng = Engine(make_engine(clock).sensors.values(), [Zone("z", 600, "WARNING")], clock=clock, min_age_s=0.0, db_path=":memory:")
    rng = random.Random(3)
    for i in range(scans):
        batch = events_for(clock, [(-100 + 2.5 * i, 100, 50)], rng, ["geran-2"])
        for _ in range(duplicates):
            eng.ingest([dict(e) for e in batch])   # ті самі події з двох брокерів (різні пакети)
        eng.tick()
        clock.now += 0.5
    return eng


def test_duplicate_events_do_not_double_tracks_or_alerts():
    single, double = run(1), run(2)
    assert len(double.tracker.confirmed()) == len(single.tracker.confirmed()) >= 1
    assert len(double.alerts) == len(single.alerts) >= 1
    assert double.db.execute("SELECT count(*) FROM incidents").fetchone() == single.db.execute("SELECT count(*) FROM incidents").fetchone()


def test_duplicate_events_in_one_packet_are_harmless():
    clock = Clock()
    eng = Engine(make_engine(clock).sensors.values(), [Zone("z", 600, "WARNING")], clock=clock, min_age_s=0.0, db_path=":memory:")
    rng = random.Random(3)
    for i in range(18):
        batch = events_for(clock, [(-100 + 2.5 * i, 100, 50)], rng, ["geran-2"])
        eng.ingest(batch + [dict(e) for e in batch])   # обидва брокери доставили в один пакет
        eng.tick()
        clock.now += 0.5
    assert len(eng.tracker.confirmed()) == 1


def test_standby_and_leader_agree_with_duplicates_after_promotion(tmp_path):
    """Лідер і резерв отримують подвоєний потік; після підвищення резерву дублів тривог у журналі нема."""
    leader = make(tmp_path, standby=False, name="dup.db")
    standby = make(tmp_path, standby=True, name="dup.db")
    rng_a, rng_b = random.Random(3), random.Random(3)
    for i in range(18):
        for eng, rng in ((leader, rng_a), (standby, rng_b)):
            batch = events_for(eng.clock, [(-100 + 2.5 * i, 100, 50)], rng, ["geran-2"])
            eng.ingest([dict(e) for e in batch])
            eng.ingest([dict(e) for e in batch])
            eng.tick()
            eng.clock.now += 0.5
    stored = leader.db.execute("SELECT count(*) FROM incidents").fetchone()[0]
    assert stored >= 1 and standby.pending
    leader.db.close()
    standby.activate(str(tmp_path / "dup.db"), 1)
    assert standby.db.execute("SELECT count(*) FROM incidents").fetchone()[0] == stored   # дописування без дублів
