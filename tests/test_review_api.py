"""POST /api/incidents/{id}/review і фільтр GET /api/incidents?status=."""
import pytest
from fastapi.testclient import TestClient

from dronewatch.fusion.sim import default_sensors
from dronewatch.service import app
from dronewatch.service.dashboard_auth import SessionStore
from dronewatch.service.dashboard_web import ANONYMOUS, DashboardAccess
from dronewatch.service.engine import Engine, Zone


def _make(tmp_path, *, login: bool):
    db = str(tmp_path / "i.db")
    eng = Engine(default_sensors(), [Zone("z", 100, "WARNING")], db_path=db)
    for track in (1, 2):
        eng.db.execute("INSERT INTO incidents (ts, track_id, level, zone, x, y, z, speed, sensors) "
                       "VALUES (1,?, 'WARNING','A',0,0,0,5,'radar1')", (track,))
    eng.db.commit()
    app.app.state.runtime = app.Runtime(engine=eng, db_path=db)
    sessions = SessionStore(ttl_s=3600) if login else None
    app.app.state.access = DashboardAccess(sessions=sessions)
    client = TestClient(app.app, raise_server_exceptions=False)
    return client, sessions


@pytest.fixture
def client(tmp_path):
    c, sessions = _make(tmp_path, login=True)
    c.cookies.set("dw_session", sessions.create("operator"))
    return c


def _post(c, i=1, **body):
    return c.post(f"/api/incidents/{i}/review", json=body)


def test_review_requires_login(tmp_path):
    c, _ = _make(tmp_path, login=True)
    assert _post(c, status="confirmed").status_code == 401


def test_review_ok_records_author_from_session(client):
    r = _post(client, status="confirmed", note="бачив")
    assert r.status_code == 200 and r.json()["reviewed_by"] == "operator" and r.json()["review_note"] == "бачив"
    row = next(x for x in client.get("/api/incidents").json() if x["id"] == 1)
    assert row["status"] == "confirmed"


def test_review_without_login_feature_uses_anonymous_name(tmp_path):
    c, _ = _make(tmp_path, login=False)
    assert _post(c, status="dismissed").json()["reviewed_by"] == ANONYMOUS


def test_review_404_unknown_id(client):
    assert _post(client, 999, status="confirmed").status_code == 404


def test_review_422_bad_status_and_long_note(client):
    assert _post(client, status="bogus").status_code == 422
    assert _post(client, status="confirmed", note="x" * 501).status_code == 422


def test_review_rejects_foreign_origin_and_non_json(client):
    r = client.post("/api/incidents/1/review", json={"status": "confirmed"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/incidents/1/review", content='{"status":"confirmed"}', headers={"Content-Type": "text/plain"})
    assert r.status_code == 415


def test_incidents_status_filter(client):
    _post(client, 1, status="dismissed")
    assert [x["id"] for x in client.get("/api/incidents?status=new").json()] == [2]   # NULL/нове
    assert [x["id"] for x in client.get("/api/incidents?status=dismissed").json()] == [1]
    assert client.get("/api/incidents?status=bogus").status_code == 422
