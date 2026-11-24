"""Схема таблиць рушія: створення, ідемпотентність, міграція старого журналу (обидва бекенди)."""
from dronewatch.service.schema import ensure_replay_schema, ensure_schema

NEW_COLS = {"model", "model_name", "side", "status", "reviewed_by", "reviewed_at", "review_note", "failover"}


def test_ensure_schema_idempotent(db_backend):
    db = db_backend.connect()
    ensure_schema(db)
    ensure_schema(db)
    assert db.columns("incidents") >= NEW_COLS
    assert {"incident_id", "status", "by", "ts", "note"} <= db.columns("incident_reviews")
    assert db.columns("ha_epoch") == {"id", "epoch"}
    db.close()


def test_replay_table_separate_and_idempotent(db_backend):
    db = db_backend.connect()
    ensure_schema(db)
    assert db.columns("incident_replays") == set()   # без replay таблиці нема
    ensure_replay_schema(db)
    ensure_replay_schema(db)
    assert db.columns("incident_replays") == {"incident_id", "ts", "complete", "data"}
    db.close()


def test_ensure_schema_migrates_old_incidents(db_backend):
    db = db_backend.connect()
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if db.dialect == "sqlite" else "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY"
    dt = "REAL" if db.dialect == "sqlite" else "DOUBLE PRECISION"
    db.execute(f"CREATE TABLE incidents (id {pk}, ts {dt}, track_id INTEGER, level TEXT, zone TEXT, "
               f"x {dt}, y {dt}, z {dt}, speed {dt}, sensors TEXT)")
    db.execute("INSERT INTO incidents (ts, track_id, level, zone, x, y, z, speed, sensors) VALUES (1,2,'WARNING','A',0,0,0,5,'r')")
    db.commit()
    ensure_schema(db)
    assert db.columns("incidents") >= NEW_COLS
    row = db.execute("SELECT zone, status, failover FROM incidents").fetchone()
    assert tuple(row) == ("A", None, None)
    db.close()


def test_incident_ts_keeps_subsecond_precision(db_backend):
    """REAL у PostgreSQL це float4 і з'їв би точність unix-часу; колонка має бути подвійної точності."""
    db = db_backend.connect()
    ensure_schema(db)
    ts = 1_790_000_000.123456
    iid = db.insert_id("INSERT INTO incidents (ts, track_id, level, zone, x, y, z, speed, sensors) VALUES (?,?,?,?,?,?,?,?,?)",
                       (ts, 1, "WARNING", "A", 0.0, 0.0, 0.0, 1.0, "r"))
    db.commit()
    assert db.execute("SELECT ts FROM incidents WHERE id = ?", (iid,)).fetchone()[0] == ts
    db.close()
