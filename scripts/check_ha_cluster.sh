#!/bin/sh
# Перевірка файлів розгортання кластера (deploy/ha-cluster) без запуску сервісів: compose-файли розбираються docker compose,
# обов'язкові змінні справді обов'язкові (інакше порт опублікувався б на всіх інтерфейсах), скрипти проходять синтаксичну перевірку.
# Статичні перевірки безпеки (TLS, pg_hba, synchronous_mode_strict: false, порти) живуть у tests/test_ha_cluster_files.py.
set -eu
cd "$(dirname "$0")/../deploy/ha-cluster"

check() {   # check ФАЙЛ ІМ'Я IP
    NODE_NAME="$2" NODE_IP="$3" docker compose -f "$1" --env-file env.example config -q
    echo "ok: $1 ($2)"
}
check docker-compose.node.yml node-a 10.10.0.1
check docker-compose.node.yml node-b 10.10.0.2
check docker-compose.witness.yml witness 10.10.0.3

# Без NODE_IP чи PUBLIC_BIND compose має відмовити, а не опублікувати порти на всіх інтерфейсах.
printf 'NODE_NAME=node-a\nCLUSTER_SCOPE=x\nA_IP=1\nB_IP=2\nW_IP=3\nPG_SUPERUSER_PASSWORD=x\nPG_REPLICATION_PASSWORD=x\nDW_OWNER_PASSWORD=x\nDW_APP_PASSWORD=x\nREDIS_PASSWORD=x\nMQTT_USER=x\nMQTT_PASSWORD=x\n' > /tmp/dw-env-missing
if env -u NODE_IP -u PUBLIC_BIND docker compose -f docker-compose.node.yml --env-file /tmp/dw-env-missing config -q 2>/dev/null; then
    echo "ПОМИЛКА: compose прийняв конфіг без NODE_IP і PUBLIC_BIND" >&2
    exit 1
fi
rm -f /tmp/dw-env-missing
echo "ok: без NODE_IP/PUBLIC_BIND compose відмовляє"

for f in patroni/entrypoint.sh patroni/post_bootstrap.sh redis/entrypoint.sh sentinel/entrypoint.sh gen-certs.sh; do
    sh -n "$f"
    echo "ok: sh -n $f"
done
if command -v shellcheck >/dev/null 2>&1; then
    shellcheck -s sh patroni/entrypoint.sh patroni/post_bootstrap.sh redis/entrypoint.sh sentinel/entrypoint.sh gen-certs.sh
    echo "ok: shellcheck"
fi
