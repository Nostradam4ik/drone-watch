#!/usr/bin/env python3
"""Генератор ключів датчиків і рядків для паролів Mosquitto.

    python keygen.py radar1 rf1 ac1        # новий випадковий ключ для кожного датчика
    python keygen.py --hash <ключ>         # SHA-256 уже наявного ключа (наприклад, після ротації вручну)
    python keygen.py --mosquitto ІМ'Я ПАРОЛЬ   # рядок для mosquitto.passwd з наявного пароля (сервісний користувач тощо)

Для кожного датчика друкує ТРИ речі:
  1. ключ: покажіть його лише цьому датчику (HTTP: заголовок X-API-Key; MQTT: пароль користувача в брокері
     з іменем датчика та поле "key" у події). Сервер ключ не зберігає й відновити його не зможе: загубили, видайте новий.
  2. рядок key_sha256 для config.yaml: його не треба ховати, за ним ключа не відновити.
  3. рядок для файлу паролів Mosquitto (deploy/mosquitto.passwd): користувач = id датчика, пароль = ключ.

Ротація: згенеруйте новий ключ, замініть key_sha256 у конфігу й рядок у mosquitto.passwd, перезапустіть сервіс,
перезапустіть брокер (docker compose restart mosquitto), оновіть датчик.
"""
import argparse
import base64
import hashlib
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from auth import hash_key  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")


def mosquitto_hash(password, salt=None):
    """Рядок хеша для файлу паролів Mosquitto 2.x: $7$101$<сіль base64>$<PBKDF2-HMAC-SHA512 base64>.
    Формат і число ітерацій (101) зафіксовані брокером; для випадкового 256-бітного ключа цього досить, але для паролів,
    які вигадала людина, це слабкий хеш: такі паролі в цей файл не кладіть."""
    salt = salt if salt is not None else secrets.token_bytes(12)
    digest = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, 101)
    return "$7$101$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def main():
    ap = argparse.ArgumentParser(description="Генератор ключів датчиків", epilog=__doc__.split("\n\n", 1)[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sensors", nargs="*", help="id датчиків з config.yaml")
    ap.add_argument("--hash", metavar="KEY", help="лише порахувати key_sha256 для наявного ключа")
    ap.add_argument("--mosquitto", nargs=2, metavar=("ІМ'Я", "ПАРОЛЬ"), help="лише порахувати рядок для mosquitto.passwd")
    a = ap.parse_args()
    if a.mosquitto:
        print(f"{a.mosquitto[0]}:{mosquitto_hash(a.mosquitto[1])}")
        return 0
    if a.hash:
        print(hash_key(a.hash))
        return 0
    if not a.sensors:
        ap.error("вкажіть id датчиків, --hash або --mosquitto")
    for sid in a.sensors:
        key = secrets.token_urlsafe(32)   # 256 біт
        print(f"{sid}:\n  ключ датчика (покажіть лише йому, сервер його не зберігає): {key}\n"
              f"  у config.yaml:      key_sha256: \"{hash_key(key)}\"\n"
              f"  у mosquitto.passwd: {sid}:{mosquitto_hash(key)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
