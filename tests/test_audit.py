"""Журнал дій: append-only, ланцюжок хешів, очищення вводу, збої запису."""
import sqlite3
import threading

import pytest

from dronewatch.service.audit import GENESIS, AuditEntry, AuditError, AuditLog, VerifyResult


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
    assert log.verify().broken_at == 3


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
