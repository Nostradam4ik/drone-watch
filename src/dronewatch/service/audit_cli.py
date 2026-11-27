"""dronewatch-audit --verify: перевірка ланцюжка хешів журналу дій. БД відкривається лише для читання."""
import argparse
import os
import sqlite3
import threading
from pathlib import Path

from dronewatch.service.app import default_db_path
from dronewatch.service.audit import AuditLog, VerifyResult
from dronewatch.service.store import PostgresStore, StoreError, check_pg_dsn


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dronewatch-audit", description="Перевірка цілісності журналу дій")
    p.add_argument("--verify", action="store_true", required=True, help="перевірити ланцюжок хешів")
    p.add_argument("--db", default=None, help="шлях до incidents.db (за замовчуванням DB_PATH або ./data/incidents.db)")
    p.add_argument("--dsn", default=None, help="DSN PostgreSQL (пароль у DRONEWATCH_PG_PASSWORD) замість файлу")
    args = p.parse_args(argv)
    if args.dsn:
        return _verify_postgres(args.dsn)
    path = Path(args.db) if args.db else Path(default_db_path())
    if not path.is_file():
        print(f"Файлу БД немає: {path}")
        return 2
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        try:
            res = AuditLog(db, threading.Lock(), create=False).verify()
        except sqlite3.Error as e:   # напр. немає таблиці audit_log
            print(f"Не вдалося прочитати журнал: {e}")
            return 2
    finally:
        db.close()
    return _report(res)


def _report(res: VerifyResult) -> int:
    if res.ok:
        print(f"OK: {res.rows} записів, head={res.head}")
        return 0
    print(f"ПОШКОДЖЕНО: перший невідповідний запис id={res.broken_at}")
    return 1


def _verify_postgres(dsn: str) -> int:
    try:
        check_pg_dsn(dsn)
    except ValueError as e:
        print(e)
        return 2
    try:
        db = PostgresStore(dsn, os.environ.get("DRONEWATCH_PG_PASSWORD"), 5, 30).connect(readonly=True)
    except StoreError as e:
        print(f"Не вдалося підключитись до PostgreSQL: {e}")
        return 2
    try:
        try:
            res = AuditLog(db, threading.Lock(), create=False).verify()
        except StoreError as e:   # напр. немає таблиці audit_log
            print(f"Не вдалося прочитати журнал: {e}")
            return 2
    finally:
        db.close()
    return _report(res)


if __name__ == "__main__":
    raise SystemExit(main())
