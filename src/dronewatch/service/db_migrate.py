"""dronewatch-db-migrate: схема PostgreSQL з нуля (`--init`) і перенесення журналу з файлу SQLite (`--from`).

Запускати під роллю `dw_owner` (DDL), а не під `dw_app`: у робочої ролі сервісу права на створення таблиць немає навмисно.
Перенесення: джерело відкривається лише для читання й не змінюється; ланцюжок хешів журналу дій джерела перевіряється ДО
того, як ціль чіпається (пошкоджене джерело = код 1, ціль чиста); цільова БД має бути порожньою (інакше код 2); усе копіюється
в одній транзакції з тими самими id й хешами (без перерахунку), потім послідовності й `audit_head` виставляються за переданим,
`ha_epoch` не нижче джерела, і після commit ланцюжок перевіряється вже в PostgreSQL.

Коди виходу: 0 успіх, 1 перевірка не зійшлась, 2 помилка чи відмова."""
import argparse
import os
import threading
from pathlib import Path

from dronewatch.service.audit import AuditLog
from dronewatch.service.schema import ensure_replay_schema, ensure_schema
from dronewatch.service.store import Db, PostgresStore, SqliteStore, StoreError, check_pg_dsn

BATCH = 500
# (таблиця, ключ сортування, id призначає IDENTITY у PostgreSQL)
TABLES = (("incidents", "id", True), ("incident_reviews", "id", True), ("incident_replays", "incident_id", False),
          ("audit_log", "id", False))


def init_schema(db: Db) -> None:
    """Усі таблиці, тригери й службові рядки. Ідемпотентно."""
    ensure_schema(db)
    ensure_replay_schema(db)
    AuditLog(db, threading.Lock())   # створює audit_log, audit_head і тригери (create=True за замовчуванням)


def _count(db: Db, table: str) -> int:
    return int(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def _copy(src: Db, dst: Db, table: str, order: str, identity: bool) -> int:
    cols = sorted(src.columns(table) & dst.columns(table))
    names = ", ".join(f'"{c}"' for c in cols)
    insert = (f"INSERT INTO {table} ({names}) {'OVERRIDING SYSTEM VALUE ' if identity else ''}"
              f"VALUES ({', '.join('?' for _ in cols)})")
    cur = src.execute(f"SELECT {names} FROM {table} ORDER BY {order}")
    copied = 0
    while batch := cur.fetchmany(BATCH):
        for row in batch:
            dst.execute(insert, tuple(row))
        copied += len(batch)
    return copied


def _migrate(src: Db, dst: Db) -> dict[str, int]:
    """Усе в одній транзакції (commit робить виклик). Повертає кількість перенесених рядків за таблицями."""
    counts: dict[str, int] = {}
    for table, order, identity in TABLES:
        if src.columns(table):
            counts[table] = _copy(src, dst, table, order, identity)
    for table in ("incidents", "incident_reviews"):
        dst.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE((SELECT max(id) FROM {table}), 0) + 1, false)")
    last = dst.execute("SELECT id, hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    if last is not None:
        dst.execute("UPDATE audit_head SET last_id = ?, head_hash = ? WHERE id = 1", (int(last[0]), str(last[1])))
    if src.columns("ha_epoch"):
        row = src.execute("SELECT epoch FROM ha_epoch WHERE id = 1").fetchone()
        if row is not None:
            dst.execute("INSERT INTO ha_epoch (id, epoch) VALUES (1, ?) ON CONFLICT (id) DO UPDATE "
                        "SET epoch = GREATEST(ha_epoch.epoch, excluded.epoch)", (int(row[0]),))
    return counts


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dronewatch-db-migrate", description="Схема PostgreSQL і перенесення журналу з SQLite")
    p.add_argument("--dsn", required=True, help="DSN PostgreSQL (пароль у DRONEWATCH_PG_PASSWORD)")
    p.add_argument("--from", dest="source", default=None, help="файл incidents.db, який перенести (джерело не змінюється)")
    p.add_argument("--init", action="store_true", help="лише створити схему (без перенесення)")
    args = p.parse_args(argv)
    if not args.source and not args.init:
        p.error("потрібен --from FILE або --init")
    try:
        check_pg_dsn(args.dsn)
    except ValueError as e:
        print(e)
        return 2
    src_path = Path(args.source) if args.source else None
    if src_path is not None and not src_path.is_file():
        print(f"Файлу БД немає: {src_path}")
        return 2
    try:
        return _run(args.dsn, src_path)
    except StoreError as e:
        print(f"Помилка сховища: {e}")   # без тексту драйвера (адреса, пароль)
        return 2


def _run(dsn: str, src_path: Path | None) -> int:
    src: Db | None = None
    if src_path is not None:
        src = SqliteStore(str(src_path)).connect(readonly=True)
        if src.columns("audit_log"):
            res = AuditLog(src, threading.Lock(), create=False).verify()
            if not res.ok:
                print(f"ПОШКОДЖЕНО: джерело має зіпсований журнал дій з запису id={res.broken_at}; цільову БД не змінено")
                src.close()
                return 1
    dst = PostgresStore(dsn, os.environ.get("DRONEWATCH_PG_PASSWORD"), 5, 120).connect()
    try:
        init_schema(dst)
        if src is None:
            print("Схему створено (або вона вже була)")
            return 0
        if any(_count(dst, table) for table, _, _ in TABLES):
            dst.rollback()
            print("Цільова БД не порожня: перенесення відмовлено, нічого не змінено")
            return 2
        counts = _migrate(src, dst)
        for table, n in counts.items():
            if _count(dst, table) != n:   # у транзакції, до commit
                dst.rollback()
                print(f"Кількість рядків {table} після копіювання не збігається: перенесення скасовано")
                return 1
        dst.commit()
        res = AuditLog(dst, threading.Lock(), create=False).verify()
        if not res.ok:
            print(f"ПОШКОДЖЕНО: після перенесення ланцюжок журналу в PostgreSQL не зійшовся з запису id={res.broken_at}")
            return 1
        print("Перенесено: " + ", ".join(f"{t}={n}" for t, n in counts.items()) + f"; журнал дій OK, head={res.head}")
        return 0
    finally:
        dst.close()
        if src is not None:
            src.close()


if __name__ == "__main__":
    raise SystemExit(main())
