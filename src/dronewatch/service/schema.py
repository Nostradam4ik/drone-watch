"""Створення й міграція таблиць рушія для обох діалектів (SQLite, PostgreSQL). Ідемпотентно: безпечно викликати при кожному старті.

Типи: REAL у PostgreSQL це float4 і не втримає unix-час із долями секунди, тож там DOUBLE PRECISION. Журнал дій (`audit_log`)
створює сам `AuditLog`. Таблицю відтворення (`incident_replays`) створює лише ввімкнений replay (`ensure_replay_schema`)."""
from dronewatch.service.store import Db


def _t(db: Db) -> tuple[str, str, str]:
    """(автоінкрементний ключ, дійсне число, двійкові дані) для діалекту."""
    if db.dialect == "postgres":
        return "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY", "DOUBLE PRECISION", "BYTEA"
    return "INTEGER PRIMARY KEY AUTOINCREMENT", "REAL", "BLOB"


def ensure_schema(db: Db) -> None:
    pk, real, _ = _t(db)
    db.execute(f"""CREATE TABLE IF NOT EXISTS incidents (
        id {pk}, ts {real}, track_id INTEGER, level TEXT, zone TEXT,
        x {real}, y {real}, z {real}, speed {real}, sensors TEXT)""")
    have = db.columns("incidents")
    for col, typ in (("model", "TEXT"), ("model_name", "TEXT"), ("side", "TEXT"), ("status", "TEXT"),
                     ("reviewed_by", "TEXT"), ("reviewed_at", real), ("review_note", "TEXT"), ("failover", "INTEGER")):
        if col not in have:   # міграція старих журналів: нові колонки лишаються NULL у старих рядках (NULL status = new)
            db.execute(f"ALTER TABLE incidents ADD COLUMN {col} {typ}")
    db.execute(f"""CREATE TABLE IF NOT EXISTS incident_reviews (
        id {pk}, incident_id BIGINT, status TEXT, by TEXT, ts {real}, note TEXT)""")
    db.execute("CREATE TABLE IF NOT EXISTS ha_epoch (id INTEGER PRIMARY KEY CHECK (id = 1), epoch BIGINT NOT NULL)")
    db.commit()


def ensure_replay_schema(db: Db) -> None:
    _, real, blob = _t(db)
    db.execute(f"""CREATE TABLE IF NOT EXISTS incident_replays (
        incident_id BIGINT PRIMARY KEY, ts {real}, complete INTEGER, data {blob})""")
    db.commit()
