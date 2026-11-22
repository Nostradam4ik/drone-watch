"""dronewatch-audit --verify: перевірка ланцюжка хешів журналу дій. БД відкривається лише для читання."""
import argparse
import sqlite3
import threading
from pathlib import Path

from dronewatch.service.app import default_db_path
from dronewatch.service.audit import AuditLog


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dronewatch-audit", description="Перевірка цілісності журналу дій")
    p.add_argument("--verify", action="store_true", required=True, help="перевірити ланцюжок хешів")
    p.add_argument("--db", default=None, help="шлях до incidents.db (за замовчуванням DB_PATH або ./data/incidents.db)")
    args = p.parse_args(argv)
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
    if res.ok:
        print(f"OK: {res.rows} записів, head={res.head}")
        return 0
    print(f"ПОШКОДЖЕНО: перший невідповідний запис id={res.broken_at}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
