#!/usr/bin/env bash
# Наскрізна перевірка піднятого стенда: docker compose --profile demo-sensors up --build -d --wait
# Запуск з кореня репозиторію: bash deploy/e2e-check.sh   (потрібні docker compose, curl і python3 або python)
# Пароль демо-користувача береться з docker-compose.yml і ніде не друкується.
set -euo pipefail
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8   # кирилиця у виводі на Windows (cp1252) інакше кидає виняток

PY=$(command -v python3 || command -v python)
URL=${URL:-https://localhost:8000}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# Git Bash на Windows інакше підміняє шляхи контейнера (/mosquitto/...) на свої; для curl і tmp-шляхів підміна, навпаки, потрібна
dc() { MSYS_NO_PATHCONV=1 docker compose "$@"; }
fail() { echo "ПРОВАЛ: $*" >&2; exit 1; }
ok() { echo "ok: $*"; }

dc exec -T service cat /certs/ca.crt > "$tmp/ca.crt"
curl_tls() { curl -s --cacert "$tmp/ca.crt" --ssl-no-revoke "$@"; }

# 1. HTTPS з перевіркою CA; без входу стан закритий, відкритий HTTP на TLS-порту не працює
[ "$(curl_tls -o /dev/null -w '%{http_code}' "$URL/api/state")" = 401 ] || fail "/api/state без входу має давати 401"
ok "без входу /api/state: 401"
if curl -s -m 5 -o /dev/null "http://localhost:8000/api/health"; then fail "відкритий HTTP на TLS-порту відповів"; fi
ok "відкритий HTTP на TLS-порту не працює"

# 2. Хибний пароль відхиляється, правильний пускає (пароль із compose-файлу, не з аргументів командного рядка)
PW=$(grep -oE 'DRONEWATCH_DASHBOARD_PASSWORD: *"[^"]+"' docker-compose.yml | sed -E 's/.*: *"//; s/"$//')
[ -n "$PW" ] || fail "не знайдено демо-пароль у docker-compose.yml"
login() { printf '{"username":"demo","password":"%s"}' "$1" | curl_tls -c "$tmp/cookies" -o /dev/null -w '%{http_code}' \
    -H 'Content-Type: application/json' -H "Origin: $URL" --data-binary @- "$URL/api/login"; }
[ "$(login 'wrong-password-123')" = 401 ] || fail "хибний пароль мав дати 401"
[ "$(login "$PW")" = 200 ] || fail "вхід за демо-паролем не вдався"
ok "вхід: хибний пароль 401, правильний 200"

# 3. Події від емулятора (HTTP-вхід + MQTT по TLS) дійшли: є треки, усі датчики OK, брокер підключений по TLS
deadline=$((SECONDS + 90))
until curl_tls -b "$tmp/cookies" "$URL/api/state" > "$tmp/state.json" && curl_tls -b "$tmp/cookies" "$URL/api/health" > "$tmp/health.json" \
      && "$PY" - "$tmp/state.json" "$tmp/health.json" 2>"$tmp/check.err" <<'PYEOF'
import json, sys
state, health = (json.load(open(p, encoding="utf-8")) for p in sys.argv[1:3])
sensors = state.get("sensors", [])
mqtt = health.get("mqtt") or {}
assert state.get("tracks"), "немає треків"
assert sensors and all(s["state"] == "OK" for s in sensors), [(s["id"], s["state"]) for s in sensors]
assert health["ok"] and not health["degraded"], health
assert mqtt.get("connected") and mqtt.get("tls") and mqtt["idle_s"] is not None and mqtt["idle_s"] < 10, mqtt
print(f"ok: треків {len(state['tracks'])}, датчики {[s['id'] for s in sensors]} усі OK, MQTT по TLS підключений (idle {mqtt['idle_s']} с)")
PYEOF
do
  [ $SECONDS -lt $deadline ] || fail "за 90 с стан не став здоровим: $(tail -n 3 "$tmp/check.err" 2>/dev/null); health: $(head -c 400 "$tmp/health.json" 2>/dev/null)"
  sleep 3
done

# 4. Брокер закритий: з логіном датчика пускає (код 0), без логіна й із хибним паролем відмовляє «not authorised» (код 5).
#    Позитивний випадок обов'язковий: без нього будь-яка помилка самої команди виглядала б як «брокер закритий».
RF1_KEY=$(grep -oE 'DRONEWATCH_KEYS: *"[^"]+"' docker-compose.yml | grep -oE 'rf1=[^,"]+' | cut -d= -f2)
[ -n "$RF1_KEY" ] || fail "не знайдено демо-ключ rf1 у docker-compose.yml"
pub() { dc exec -T mosquitto mosquitto_pub -h 127.0.0.1 -p 8883 --cafile /mosquitto/certs-ca/ca.crt \
          -t dronewatch/events/rf1 -m '{}' "$@" >/dev/null 2>&1; }
rc=0; pub -u rf1 -P "$RF1_KEY" || rc=$?
[ "$rc" = 0 ] || fail "брокер не пустив датчика rf1 з правильним ключем (код $rc)"
rc=0; pub || rc=$?
[ "$rc" = 5 ] || fail "анонімне з'єднання з брокером мало дати відмову «not authorised» (код 5), а дало $rc"
rc=0; pub -u rf1 -P wrong-password || rc=$?
[ "$rc" = 5 ] || fail "хибний пароль мав дати код 5, а дав $rc"
ok "брокер: із ключем пускає, без логіна й із хибним паролем відмовляє"

echo "Стенд працює."
