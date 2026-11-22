"""Номери треків: свої в кожного трекера (не спільний лічильник класу), а рушій після скидання не починає знову з #1."""
import numpy as np

from dronewatch.fusion.models import Sensor
from dronewatch.fusion.tracker import Tracker
from dronewatch.service.engine import Engine, Zone


def radar() -> Sensor:
    return Sensor("radar1", "radar", (0, 0, 0), (5.0, 0.012, 0.026), 0.9, 1500, 0.0, "radar")


def feed(tracker: Tracker, scans: int = 3, t0: float = 0.0) -> None:
    for k in range(scans):
        tracker.step(t0 + k * 0.5, [("radar1", np.array([500.0, 0.3, 0.1])), ("radar1", np.array([900.0, -1.2, 0.05]))])


def test_each_tracker_numbers_its_own_tracks_from_one():
    a, b = Tracker([radar()]), Tracker([radar()])
    feed(a)
    feed(b)
    assert sorted(tr.id for tr in a.tracks) == sorted(tr.id for tr in b.tracks) == [1, 2]


def test_tracker_can_continue_numbering():
    tr = Tracker([radar()], first_id=40)
    feed(tr)
    assert sorted(t.id for t in tr.tracks) == [40, 41]
    assert tr.next_id == 42


def test_engine_reset_does_not_reuse_track_ids(tmp_path):
    """Тривоги й журнал посилаються на номер цілі: нова ціль #1 після скидання злилася б зі старою тривогою #1."""
    eng = Engine([radar()], [Zone("z", 600, "WARNING")], db_path=str(tmp_path / "t.db"))
    feed(eng.tracker)
    before = {tr.id for tr in eng.tracker.tracks}
    eng.reset()
    feed(eng.tracker, t0=10.0)
    after = {tr.id for tr in eng.tracker.tracks}
    assert before and after and min(after) > max(before)
