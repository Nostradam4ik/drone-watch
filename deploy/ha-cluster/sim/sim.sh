#!/bin/sh
# Репетиційний стенд кластера на одній машині. Використання:
#   sh sim.sh up       згенерувати сертифікати, змінні й секрети, підняти etcd/Patroni/Redis/Sentinel/брокери, створити схему, запустити сервіси
#   sh sim.sh status   хто лідер PostgreSQL, master Redis, ролі копій сервісу
#   sh sim.sh down     зупинити й видалити контейнери й томи (сертифікати й секрети лишаються в sim/)
# Адреси «машин»: A 10.77.0.11, B 10.77.0.12, свідок 10.77.0.13. Дашборд копій: https://127.0.0.1:8101 (A) і :8102 (B).
set -eu
export MSYS_NO_PATHCONV=1   # Git Bash на Windows інакше перетворює /opt/... у шлях Git
cd "$(dirname "$0")"
HERE="$(pwd)"
dc() { docker compose "$@"; }

gen_env() {
    [ -s .env ] && return 0
    pw() { head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n'; }   # лише шістнадцяткові символи: підходять під обмеження паролів
    cat > .env <<EOF
CLUSTER_SCOPE=dronewatch
A_IP=10.77.0.11
B_IP=10.77.0.12
W_IP=10.77.0.13
PG_SUPERUSER_PASSWORD=$(pw)
PG_REPLICATION_PASSWORD=$(pw)
DW_OWNER_PASSWORD=$(pw)
DW_APP_PASSWORD=$(pw)
REDIS_PASSWORD=$(pw)
SIM_MQTT_PW_A=$(pw)
SIM_MQTT_PW_B=$(pw)
EOF
    chmod 600 .env
}

gen_certs() {
    [ -s certs/node-a/etcd.crt ] && return 0
    mkdir -p certs
    # openssl береться з контейнера: на хості (Windows) його може не бути; MSYS_NO_PATHCONV не дає Git Bash псувати шляхи
    MSYS_NO_PATHCONV=1 docker run --rm -e A_IP=10.77.0.11 -e B_IP=10.77.0.12 -e W_IP=10.77.0.13 \
        -v "$HERE/../gen-certs.sh:/gen-certs.sh:ro" -v "$HERE/certs:/out" --entrypoint sh alpine/openssl /gen-certs.sh /out
}

# Секрети стенда: ключі датчиків, користувач дашборда й логіни брокера генеруються випадково (dronewatch-keygen з образу сервісу).
# Публічних демо-секретів тут нема: кластерний режим відмовив би на них. Ключі датчиків і пароль дашборда лишаються в .env.
keygen() { docker run --rm --entrypoint dronewatch-keygen dw-service:ha "$@" | tr -d '\r'; }

gen_secrets() {
    [ -s config.sim.yaml ] && [ -s mosquitto.passwd ] && return 0
    set -a; . ./.env; set +a
    dc build service-a
    : > mosquitto.passwd
    sed -e 's/10\.10\.0\.1/10.77.0.11/g' -e 's/10\.10\.0\.2/10.77.0.12/g' -e 's/10\.10\.0\.3/10.77.0.13/g' \n        ../config.cluster.yaml > config.sim.yaml.tmp
    for sid in radar1 rf1 ac1; do
        out="$(keygen "$sid")"
        key="$(printf '%s\n' "$out" | sed -n 's/.*ключ датчика[^:]*: //p')"
        sha="$(printf '%s\n' "$out" | sed -n 's/.*key_sha256: "\(.*\)"/\1/p')"
        printf '%s\n' "$out" | sed -n 's/.*mosquitto.passwd: //p' >> mosquitto.passwd
        sed -i "s|REPLACE_ME_$sid|$sha|" config.sim.yaml.tmp
        printf 'SIM_KEY_%s=%s\n' "$sid" "$key" >> .env
    done
    out="$(keygen --dashboard-user admin --role admin --generate)"
    printf 'SIM_DASH_PASSWORD=%s\n' "$(printf '%s\n' "$out" | sed -n 's/^пароль[^:]*: //p')" >> .env
    hash="$(printf '%s\n' "$out" | sed -n 's/.*password_hash: "\(.*\)".*/\1/p')"
    sed -i "s|REPLACE_ME_dashboard_hash|$hash|" config.sim.yaml.tmp
    keygen --mosquitto dronewatch-service "$SIM_MQTT_PW_A" >> mosquitto.passwd
    keygen --mosquitto dronewatch-service-2 "$SIM_MQTT_PW_B" >> mosquitto.passwd
    if grep -q 'REPLACE_ME_[A-Za-z]' config.sim.yaml.tmp; then
        echo "sim: у конфігу лишились плейсхолдери" >&2
        return 1
    fi
    mv config.sim.yaml.tmp config.sim.yaml
}

patroni_list() {
    dc exec -T pg-a /opt/patroni/bin/patronictl -c /tmp/patroni.yml list 2>/dev/null \
        || dc exec -T pg-b /opt/patroni/bin/patronictl -c /tmp/patroni.yml list 2>/dev/null
}

wait_for() {   # wait_for ОПИС СЕКУНД КОМАНДА...
    what="$1"; limit="$2"; shift 2
    t=0
    until "$@" >/dev/null 2>&1; do
        t=$((t + 2))
        [ "$t" -le "$limit" ] || { echo "sim: не дочекались: $what" >&2; return 1; }
        sleep 2
    done
    echo "sim: $what (${t}s)"
}

has_leader() { patroni_list | grep -q "Leader"; }
has_two_members() { [ "$(patroni_list | grep -c -E 'Leader|Replica|Sync Standby')" -ge 2 ]; }
any_ready() { curl --ssl-no-revoke -sf --cacert certs/ca/ca.crt https://127.0.0.1:8101/readyz || curl --ssl-no-revoke -sf --cacert certs/ca/ca.crt https://127.0.0.1:8102/readyz; }

as_leader_psql() {   # as_leader_psql ФАЙЛ_SQL: виконати на тому вузлі, де запис дозволений
    for n in pg-a pg-b; do
        if dc exec -T "$n" psql -U postgres -h /var/run/postgresql -d dronewatch -tAc "SELECT NOT pg_is_in_recovery()" 2>/dev/null | grep -q t; then
            dc exec -T "$n" psql -U postgres -h /var/run/postgresql -d dronewatch -v ON_ERROR_STOP=1 < "$1"
            return 0
        fi
    done
    echo "sim: лідера PostgreSQL не знайдено" >&2
    return 1
}

up() {
    gen_env; gen_certs; gen_secrets
    set -a; . ./.env; set +a
    dc up -d --build node-a node-b witness etcd-a etcd-b etcd-w pg-a pg-b redis-a redis-b sentinel-a sentinel-b sentinel-w mosquitto-a mosquitto-b
    wait_for "лідер PostgreSQL (Patroni)" 240 has_leader
    wait_for "обидва вузли PostgreSQL у кластері" 240 has_two_members
    # схема під dw_owner, права dw_app: сервіс (образ із драйвером) у ролі одноразового інструмента
    DSN="postgresql://dw_owner@10.77.0.11:5432,10.77.0.12:5432/dronewatch?sslmode=verify-full&target_session_attrs=read-write"
    dc run --rm --no-deps -e PGSSLROOTCERT=/certs/ca.crt -e "DRONEWATCH_PG_PASSWORD=$DW_OWNER_PASSWORD" \
        --entrypoint dronewatch-db-migrate service-a --dsn "$DSN" --init
    as_leader_psql ../postgres-roles.sql
    dc up -d service-a service-b
    wait_for "хоча б одна копія сервісу готова (/readyz 200)" 120 any_ready
    status
}

status() {
    echo "== PostgreSQL (Patroni)"; patroni_list || true
    echo "== Redis (Sentinel): поточний master"
    set -a; . ./.env; set +a
    dc exec -T sentinel-a redis-cli --tls --cacert /tmp/tls/ca.crt -p 26379 -a "$REDIS_PASSWORD" --no-auth-warning \
        sentinel get-master-addr-by-name dw 2>/dev/null || echo "(немає відповіді)"
    echo "== Копії сервісу"
    for p in 8101 8102; do
        printf 'порт %s: ' "$p"
        curl --ssl-no-revoke -s --cacert certs/ca/ca.crt "https://127.0.0.1:$p/readyz" || printf '(недоступна)'
        echo
    done
}

down() { dc down -v --remove-orphans; }

case "${1:-status}" in
    up) up ;;
    status) status ;;
    down) down ;;
    *) echo "використання: sh sim.sh up|status|down" >&2; exit 2 ;;
esac
