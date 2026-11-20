"""Read-only AI-тули над Engine/Runtime і винесений read_incidents (той самий запит, що /api/incidents)."""
import pytest

from dronewatch.fusion.sim import default_sensors
from dronewatch.service import app
from dronewatch.service.ai import tools
from dronewatch.service.ai.errors import AiUnavailable
from dronewatch.service.engine import Engine, Zone


def test_read_incidents_matches_existing_query_shape(tmp_path):
    db_path = str(tmp_path / "incidents.db")
    eng = Engine(default_sensors(), [], db_path=db_path)
    eng.db.execute("INSERT INTO incidents (ts, track_id, level, zone, x, y, z, speed, sensors) VALUES (1,2,'WARNING','A',0,0,0,5,'radar1')")
    eng.db.commit()
    rows = app.read_incidents(db_path, limit=10)
    assert rows[0]["track_id"] == 2 and rows[0]["zone"] == "A"


class _FakeIndex:
    def __init__(self, hits=None, fail=False):
        self.hits, self.fail = hits or [], fail

    def search(self, query, k=3):
        if self.fail:
            raise AiUnavailable("down")
        return self.hits


def _runtime():
    return app.Runtime(engine=Engine(default_sensors(), [Zone("z", 100, "WARNING")]))


def test_get_state_reflects_engine():
    rt = _runtime()
    assert tools.get_state(rt) == rt.engine.state()


def test_get_sensors_reflects_engine():
    rt = _runtime()
    assert tools.get_sensors(rt) == list(rt.engine.sensor_status().values())


def test_get_incidents_uses_read_incidents(tmp_path):
    db_path = str(tmp_path / "incidents.db")
    rt = app.Runtime(engine=Engine(default_sensors(), [], db_path=db_path), db_path=db_path)
    rt.engine.db.execute("INSERT INTO incidents (ts, track_id, level, zone, x, y, z, speed, sensors) VALUES (1,2,'WARNING','A',0,0,0,5,'radar1')")
    rt.engine.db.commit()
    assert tools.get_incidents(rt, limit=5) == app.read_incidents(db_path, limit=5)


def test_search_similar_incidents_returns_hits():
    assert tools.search_similar_incidents(_FakeIndex(hits=["x"]), "q") == ["x"]


def test_search_similar_incidents_degrades_on_ai_unavailable():
    assert "недоступний" in tools.search_similar_incidents(_FakeIndex(fail=True), "q")


def test_call_tool_dispatches_by_name():
    rt = _runtime()
    assert tools.call_tool("get_state", {}, rt=rt, index=_FakeIndex()) == rt.engine.state()


def test_call_tool_unknown_name_raises_keyerror():
    rt = _runtime()
    with pytest.raises(KeyError):
        tools.call_tool("delete_everything", {}, rt=rt, index=_FakeIndex())


def test_get_incident_stats_counts_by_zone_and_status(tmp_path):
    db_path = str(tmp_path / "incidents.db")
    rt = app.Runtime(engine=Engine(default_sensors(), [], db_path=db_path), db_path=db_path)
    ids = []
    for zone in ("A", "A", "B"):
        cur = rt.engine.db.execute("INSERT INTO incidents (ts, track_id, level, zone, x, y, z, speed, sensors) "
                                   "VALUES (?,1,'WARNING',?,0,0,0,5,'r')", (rt.engine.clock(), zone))
        ids.append(cur.lastrowid)
    rt.engine.db.commit()
    rt.engine.review_incident(ids[1], "dismissed", "op")
    stats = {(r["zone"], r["status"]): r["count"] for r in tools.get_incident_stats(rt, days=7)}
    assert stats == {("A", "new"): 1, ("A", "dismissed"): 1, ("B", "new"): 1}


@pytest.mark.parametrize("days", ["x", None, -5, 10**9])
def test_get_incident_stats_bad_days_does_not_crash(days):
    assert tools.call_tool("get_incident_stats", {"days": days}, rt=_runtime(), index=_FakeIndex()) == []


def test_tool_schemas_include_stats_and_stay_read_only(tmp_path):
    assert "get_incident_stats" in {t["function"]["name"] for t in tools.TOOL_SCHEMAS}
    db_path = str(tmp_path / "incidents.db")
    rt = app.Runtime(engine=Engine(default_sensors(), [], db_path=db_path), db_path=db_path)
    before = rt.engine.db.total_changes
    tools.call_tool("get_incident_stats", {}, rt=rt, index=_FakeIndex())
    assert rt.engine.db.total_changes == before
