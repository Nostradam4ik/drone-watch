#!/usr/bin/env python3
"""Генератор ключів датчиків, рядків для паролів Mosquitto і користувачів дашборда.

    dronewatch-keygen radar1 rf1 ac1        # новий випадковий ключ для кожного датчика
    dronewatch-keygen --hash <ключ>         # SHA-256 уже наявного ключа (наприклад, після ротації вручну)
    dronewatch-keygen --mosquitto ІМ'Я ПАРОЛЬ   # рядок для mosquitto.passwd з наявного пароля (сервісний користувач тощо)
    dronewatch-keygen --dashboard-user ІМ'Я     # користувач дашборда: пароль питає двічі (getpass), друкує рядок для config.yaml
    dronewatch-keygen --dashboard-user ІМ'Я --generate          # то саме з випадковим паролем (показується один раз)
    DASH_PW=... dronewatch-keygen --dashboard-user ІМ'Я --password-env DASH_PW   # для автоматизації: пароль зі змінної, не з аргументів

Для кожного датчика друкує ТРИ речі:
  1. ключ: покажіть його лише цьому датчику (HTTP: заголовок X-API-Key; MQTT: пароль користувача в брокері
     з іменем датчика та поле "key" у події). Сервер ключ не зберігає й відновити його не зможе: загубили, видайте новий.
  2. рядок key_sha256 для config.yaml: його не треба ховати, за ним ключа не відновити.
  3. рядок для файлу паролів Mosquitto (deploy/mosquitto.passwd): користувач = id датчика, пароль = ключ.

Користувач дашборда: у config.yaml зберігається лише scrypt-хеш пароля (dashboard.users). Пароль не коротший за 12 символів.

Ротація: згенеруйте новий ключ, замініть key_sha256 у конфігу й рядок у mosquitto.passwd, перезапустіть сервіс,
перезапустіть брокер (docker compose restart mosquitto), оновіть датчик.
"""
import argparse
import base64
import getpass
import hashlib
import io
import os
import re
import secrets
import sys

from dronewatch.service.auth import hash_key
from dronewatch.service.dashboard_auth import MIN_PASSWORD_LEN, hash_password

if isinstance(sys.stdout, io.TextIOWrapper):   # у тестах (pytest) stdout підмінено і reconfigure немає
    sys.stdout.reconfigure(encoding="utf-8")


def mosquitto_hash(password: str, salt: bytes | None = None) -> str:
    """Рядок хеша для файлу паролів Mosquitto 2.x: $7$101$<сіль base64>$<PBKDF2-HMAC-SHA512 base64>.
    Формат і число ітерацій (101) зафіксовані брокером; для випадкового 256-бітного ключа цього досить, але для паролів,
    які вигадала людина, це слабкий хеш: такі паролі в цей файл не кладіть."""
    salt = salt if salt is not None else secrets.token_bytes(12)
    digest = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, 101)
    return "$7$101$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def new_secret(nbytes: int) -> str:
    """Випадковий секрет (base64url). Не починається з «-»: інакше argparse прийняв би його за прапорець, і видані ключі
    не можна було б передати в `--hash КЛЮЧ` чи `--mosquitto ІМ'Я КЛЮЧ` (це траплялося в ~1 із 64 випадків)."""
    while (secret := secrets.token_urlsafe(nbytes)).startswith("-"):
        pass
    return secret


def dashboard_user(name: str, *, generate: bool, password_env: str | None) -> int:
    """Рядок для dashboard.users у config.yaml. Пароль не друкується, крім режиму --generate (там інакше його ніхто не дізнається)."""
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", name):
        print("Ім'я: 1-64 символи з літер, цифр і _ . @ -", file=sys.stderr)
        return 2
    if generate:
        password = new_secret(18)
    elif password_env:
        if not (password := os.environ.get(password_env, "")):
            print(f"Змінна {password_env} порожня або не задана", file=sys.stderr)
            return 2
    else:
        password = getpass.getpass("Пароль: ")
        if password != getpass.getpass("Ще раз: "):
            print("Паролі не збігаються", file=sys.stderr)
            return 2
    if len(password) < MIN_PASSWORD_LEN:
        print(f"Пароль закороткий: потрібно щонайменше {MIN_PASSWORD_LEN} символів", file=sys.stderr)
        return 2
    if generate:
        print(f"пароль (покажіть лише користувачу, сервер його не зберігає): {password}")
    print("у config.yaml, dashboard.users:")
    print(f'  - {{name: {name}, password_hash: "{hash_password(password)}"}}')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Генератор ключів датчиків", epilog=__doc__.split("\n\n", 1)[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sensors", nargs="*", help="id датчиків з config.yaml")
    ap.add_argument("--hash", metavar="KEY", help="лише порахувати key_sha256 для наявного ключа (якщо він починається з «-»: --hash=КЛЮЧ)")
    ap.add_argument("--mosquitto", nargs=2, metavar=("ІМ'Я", "ПАРОЛЬ"), help="лише порахувати рядок для mosquitto.passwd")
    ap.add_argument("--dashboard-user", metavar="ІМ'Я", help="згенерувати рядок для користувача дашборда")
    ap.add_argument("--generate", action="store_true", help="з --dashboard-user: випадковий пароль")
    ap.add_argument("--password-env", metavar="ЗМІННА", help="з --dashboard-user: взяти пароль зі змінної оточення")
    a = ap.parse_args()
    if a.dashboard_user:
        return dashboard_user(a.dashboard_user, generate=a.generate, password_env=a.password_env)
    if a.mosquitto:
        print(f"{a.mosquitto[0]}:{mosquitto_hash(a.mosquitto[1])}")
        return 0
    if a.hash:
        print(hash_key(a.hash))
        return 0
    if not a.sensors:
        ap.error("вкажіть id датчиків, --hash або --mosquitto")
    for sid in a.sensors:
        key = new_secret(32)   # 256 біт
        print(f"{sid}:\n  ключ датчика (покажіть лише йому, сервер його не зберігає): {key}\n"
              f"  у config.yaml:      key_sha256: \"{hash_key(key)}\"\n"
              f"  у mosquitto.passwd: {sid}:{mosquitto_hash(key)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
