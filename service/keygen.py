#!/usr/bin/env python3
"""Генератор ключів датчиків.

    python keygen.py radar1 rf1 ac1        # новий випадковий ключ для кожного датчика
    python keygen.py --hash <ключ>         # SHA-256 уже наявного ключа (наприклад, після ротації вручну)

Для кожного датчика друкує ДВІ речі:
  1. ключ: покажіть його лише цьому датчику (HTTP: заголовок X-API-Key, MQTT: поле "key" у події).
     Сервер ключ не зберігає й відновити його не зможе: загубили, видайте новий.
  2. рядок key_sha256 для config.yaml: його не треба ховати, за ним ключа не відновити.

Ротація: згенеруйте новий ключ, замініть key_sha256 у конфігу, перезапустіть сервіс, оновіть датчик.
"""
import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from auth import hash_key  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Генератор ключів датчиків", epilog=__doc__.split("\n\n", 1)[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sensors", nargs="*", help="id датчиків з config.yaml")
    ap.add_argument("--hash", metavar="KEY", help="лише порахувати key_sha256 для наявного ключа")
    a = ap.parse_args()
    if a.hash:
        print(hash_key(a.hash))
        return 0
    if not a.sensors:
        ap.error("вкажіть id датчиків або --hash")
    for sid in a.sensors:
        key = secrets.token_urlsafe(32)   # 256 біт
        print(f"{sid}:\n  ключ датчика (покажіть лише йому, сервер його не зберігає): {key}\n"
              f"  у config.yaml:    key_sha256: \"{hash_key(key)}\"\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
