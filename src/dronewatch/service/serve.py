#!/usr/bin/env python3
"""Запуск сервісу по HTTPS (точка входу Docker-образу): python -m dronewatch.service.serve (або dronewatch-serve)

    TLS_CERT_FILE=/tls/http.crt TLS_KEY_FILE=/tls/http.key python -m dronewatch.service.serve

HTTPS обслуговує все: дашборд, REST API, WebSocket (wss). Відкритого слухача немає. Ключі датчиків (X-API-Key), стан
і тривоги ходять лише зашифрованим каналом.

Fail-closed: без TLS_CERT_FILE і TLS_KEY_FILE сервіс НЕ стартує, щоб випадково не віддати ключі відкритим HTTP. Свідомо
запустити без шифрування (лише локальна розробка, довірена мережа за власним TLS-проксі) можна змінною
ALLOW_INSECURE_HTTP=1; про це буде попередження.

Інші змінні: HOST (0.0.0.0), PORT (8000), CONFIG, DB_PATH. Для швидкого локального запуску в демо-режимі, як і раніше,
працює `python -m uvicorn dronewatch.service.app:app --port 8000` (відкритий HTTP, лише для localhost)."""
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import uvicorn


@dataclass(frozen=True, slots=True)
class ServerSettings:
    host: str
    port: int
    ssl_certfile: str | None = None   # обидва None = відкритий HTTP (лише з ALLOW_INSECURE_HTTP=1)
    ssl_keyfile: str | None = None


def settings(env: Mapping[str, str] = os.environ) -> ServerSettings:
    """Параметри запуску або SystemExit зі зрозумілою причиною. Окремо від main(), щоб перевіряти без запуску сервера."""
    host, port = env.get("HOST", "0.0.0.0"), int(env.get("PORT", "8000"))
    cert, key = env.get("TLS_CERT_FILE"), env.get("TLS_KEY_FILE")
    if cert or key:
        if not (cert and key):
            raise SystemExit("TLS_CERT_FILE і TLS_KEY_FILE треба задавати разом")
        for name, path in (("TLS_CERT_FILE", cert), ("TLS_KEY_FILE", key)):
            if not Path(path).is_file():
                raise SystemExit(f"{name}: файл не знайдено: {path}")
        return ServerSettings(host, port, cert, key)
    if env.get("ALLOW_INSECURE_HTTP") == "1":
        print("УВАГА: HTTPS вимкнено (ALLOW_INSECURE_HTTP=1). Ключі датчиків, стан і тривоги ідуть відкритим текстом: "
              "використовуйте лише на localhost або за власним TLS-проксі.", file=sys.stderr, flush=True)
        return ServerSettings(host, port)
    raise SystemExit("HTTPS не налаштовано: задайте TLS_CERT_FILE і TLS_KEY_FILE (сертифікат і ключ сервісу). "
                     "Свідомо без шифрування: ALLOW_INSECURE_HTTP=1.")


def main() -> None:
    cfg = settings()
    # X-Forwarded-For довіряємо лише коли оператор свідомо задав FORWARDED_ALLOW_IPS (довірені проксі); інакше client.host це сам сокет
    uvicorn.run("dronewatch.service.app:app", host=cfg.host, port=cfg.port, ssl_certfile=cfg.ssl_certfile, ssl_keyfile=cfg.ssl_keyfile,
                proxy_headers=bool(os.environ.get("FORWARDED_ALLOW_IPS")))


if __name__ == "__main__":
    main()
