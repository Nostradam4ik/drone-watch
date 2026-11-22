"""Журнал дій: append-only, ланцюжок хешів, очищення вводу, збої запису."""
import sqlite3
import threading
from unittest.mock import Mock

import pytest

from dronewatch.service.audit import GENESIS, AuditEntry, AuditError, AuditLog, VerifyResult, clean


def make_log():
    db = sqlite3.connect(":memory:", check_same_thread=False)
    return db, AuditLog(db, threading.Lock(), clock=iter(range(1000, 2000)).__next__)


E = AuditEntry(user="alice", role="admin", action="layout_save", target="sensors=3", result="started", ip="127.0.0.1")


def test_append_chains_hashes_and_verify_passes():
    _db, log = make_log()
    log.append(E)
    log.append(E)
    rows = log.read()
    assert rows[1]["prev_hash"] == GENESIS and rows[0]["prev_hash"] == rows[1]["hash"]
    assert log.verify() == VerifyResult(ok=True, rows=2, broken_at=None, head=rows[0]["hash"])


def test_update_and_delete_are_rejected_by_the_database():
    db, log = make_log()
    log.append(E)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE audit_log SET user='mallory'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM audit_log")


def test_tampering_with_triggers_dropped_is_detected():
    db, log = make_log()
    log.append(E)
    log.append(E)
    log.append(E)
    db.execute("DROP TRIGGER audit_no_update")
    db.execute("UPDATE audit_log SET user='mallory' WHERE id=2")
    db.commit()
    assert log.verify().ok is False and log.verify().broken_at == 2


def test_deleted_middle_row_is_detected():
    db, log = make_log()
    for _ in range(3):
        log.append(E)
    db.execute("DROP TRIGGER audit_no_delete")
    db.execute("DELETE FROM audit_log WHERE id=2")
    db.commit()
    assert log.verify().broken_at == 2   # перший відсутній id


def test_control_chars_are_stripped():
    _db, log = make_log()
    log.append(AuditEntry("a\nb\x1b[31m\u202e", "admin", "login_failed", "x" * 500, "failed", "1.2.3.4"))
    row = log.read()[0]
    assert row["user"] == "ab[31m" and len(row["target"]) == 200
    assert log.verify().ok is True


def test_append_failure_raises_audit_error():
    db, log = make_log()
    db.close()
    with pytest.raises(AuditError):
        log.append(E)


def test_read_limit_is_clamped():
    _db, log = make_log()
    for _ in range(3):
        log.append(E)
    assert len(log.read(0)) == 1 and len(log.read(10_000)) == 3


def test_blocked_record_once_per_window():
    clock = iter([0, 1, 400]).__next__
    log = AuditLog(sqlite3.connect(":memory:"), threading.Lock(), clock=clock)
    assert log.allow_blocked_record("1.2.3.4", 300) is True
    assert log.allow_blocked_record("1.2.3.4", 300) is False
    assert log.allow_blocked_record("1.2.3.4", 300) is True


def test_surrogates_are_stripped_and_chain_stays_valid():
    assert clean("a\ud800b", 10) == "ab"
    _db, log = make_log()
    log.append(AuditEntry("a\ud800", "admin", "login_failed", "t\udfff", "failed", "1.2.3.4"))
    assert log.read()[0]["user"] == "a" and log.verify().ok is True


def test_append_wraps_non_sqlite_errors_into_audit_error():
    _db, log = make_log()
    log._clock = Mock(side_effect=RuntimeError("boom"))
    with pytest.raises(AuditError):
        log.append(E)


# ---------- dronewatch-audit --verify ----------
def _db_file(tmp_path, n=2):
    path = tmp_path / "i.db"
    db = sqlite3.connect(path)
    log = AuditLog(db, threading.Lock())
    for _ in range(n):
        log.append(AuditEntry("alice", "admin", "login", "", "ok", "1.2.3.4"))
    db.close()
    return path


def test_cli_verify_ok_and_broken(tmp_path, capsys):
    from dronewatch.service import audit_cli
    path = _db_file(tmp_path)
    assert audit_cli.main(["--verify", "--db", str(path)]) == 0 and "OK: 2" in capsys.readouterr().out
    db = sqlite3.connect(path)
    db.execute("DROP TRIGGER audit_no_update")
    db.execute("UPDATE audit_log SET ip='x' WHERE id=1")
    db.commit()
    db.close()
    assert audit_cli.main(["--verify", "--db", str(path)]) == 1 and "id=1" in capsys.readouterr().out


def test_cli_missing_db(tmp_path):
    from dronewatch.service import audit_cli
    assert audit_cli.main(["--verify", "--db", str(tmp_path / "none.db")]) == 2


def test_cli_db_without_audit_table_is_exit_2(tmp_path, capsys):
    from dronewatch.service import audit_cli
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    assert audit_cli.main(["--verify", "--db", str(path)]) == 2
    assert path.stat().st_size == 0, "CLI не створює таблиць"


def test_cli_does_not_modify_db(tmp_path):
    from dronewatch.service import audit_cli
    path = _db_file(tmp_path)
    before = path.read_bytes()
    audit_cli.main(["--verify", "--db", str(path)])
    assert path.read_bytes() == before


def test_create_false_makes_no_tables():
    db = sqlite3.connect(":memory:")
    AuditLog(db, threading.Lock(), create=False)
    assert db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0


# ---------- видалення з кінця й очищення ----------
def _drop_triggers(db):
    db.execute("DROP TRIGGER audit_no_delete")


def test_deleted_tail_row_is_detected():
    db, log = make_log()
    for _ in range(3):
        log.append(E)
    _drop_triggers(db)
    db.execute("DELETE FROM audit_log WHERE id = 3")
    db.commit()
    res = log.verify()
    assert not res.ok and res.broken_at == 3


def test_full_wipe_is_detected():
    db, log = make_log()
    log.append(E)
    log.append(E)
    _drop_triggers(db)
    db.execute("DELETE FROM audit_log")
    db.commit()
    res = log.verify()
    assert not res.ok and res.rows == 0 and res.broken_at == 1


def test_empty_never_used_log_is_ok():
    assert make_log()[1].verify().ok


def test_rate_budget_per_window():
    t = [100.0]
    log = AuditLog(sqlite3.connect(":memory:"), threading.Lock(), clock=lambda: t[0])
    assert [log.take("k", 2, 60.0) for _ in range(3)] == [True, True, False]
    assert log.take("other", 2, 60.0)
    t[0] += 61
    assert log.take("k", 2, 60.0)
