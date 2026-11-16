#!/usr/bin/env python3
"""Перевірка здоровʼя для Docker (HEALTHCHECK): код виходу 0, якщо /api/health відповідає 200.

З TLS_CERT_FILE іде по HTTPS і перевіряє сертифікат сервісу проти CA з TLS_CA_FILE (інакше проти системних), так само
суворо, як клієнти. Ім'я localhost має бути в SAN сертифіката сервісу. Без TLS_CERT_FILE іде по відкритому HTTP."""
import os
import ssl
import sys
import urllib.request

port = os.environ.get("PORT", "8000")
try:
    if os.environ.get("TLS_CERT_FILE"):
        ctx = ssl.create_default_context(cafile=os.environ.get("TLS_CA_FILE") or None)
        status = urllib.request.urlopen(f"https://localhost:{port}/api/health", timeout=2, context=ctx).status
    else:
        status = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2).status
except Exception as e:   # 503 з /api/health теж сюди: HTTPError. Причину не друкуємо: Docker її показує лише в inspect
    print(f"healthcheck: {type(e).__name__}", file=sys.stderr)
    sys.exit(1)
sys.exit(0 if status == 200 else 1)
