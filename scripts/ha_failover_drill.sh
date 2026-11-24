#!/bin/sh
# Репетиція відмов на стенді deploy/ha-cluster/sim (спершу: sh deploy/ha-cluster/sim/sim.sh up). Використання:
#   sh scripts/ha_failover_drill.sh SCENARIO
# Сценарії:
#   kill-leader        kill -9 копії сервісу, що тримає оренду: друга має стати лідером
#   kill-pg-master     kill -9 master PostgreSQL: Patroni обирає нового, лідер сервісу пише далі
#   kill-redis-master  kill -9 master Redis: Sentinel обирає нового, вхід у дашборд (сесії в Redis) відновлюється, лідер не змінюється
#   isolate-node-a     вузол A цілком відрізано від мережі, потім повернуто: жодного моменту двох лідерів, A повертається резервом
#   kill-witness       свідок зупинено: кворум 2 з 3 лишається, усе працює без перемикань
# Мірило відновлення: сервіс-лідер готовий (/readyz) і ПОВНИЙ шлях запису працює (успішний вхід адміна пише рядок журналу дій у
# PostgreSQL, 200). Ліміт: lease_ttl_s * 4 + 30 с. Після сценарію: журнал дій у PostgreSQL перевіряється, дублів тривог нема.
# Код виходу 0: відновилось у межах ліміту, split-brain не помічено, журнал цілий.
set -eu
export MSYS_NO_PATHCONV=1   # Git Bash на Windows інакше перетворює /opt/... у шлях Git
SCENARIO="${1:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/deploy/ha-cluster/sim"
[ -s .env ] || { echo "стенд не піднято: sh deploy/ha-cluster/sim/sim.sh up" >&2; exit 2; }
set -a; . ./.env; set +a
CA=certs/ca/ca.crt
LIMIT=54          # lease_ttl_s (6) * 4 + 30
SPLIT=0           # скільки разів обидві копії одночасно назвалися лідерами
dc() { docker compose "$@"; }
now() { date +%s; }

role_of() { curl --ssl-no-revoke -s --max-time 2 --cacert "$CA" "https://127.0.0.1:$1/readyz" 2>/dev/null | sed -n 's/.*"role":"\([a-z]*\)".*/\1/p'; }
leader_port() { for p in 8101 8102; do [ "$(role_of "$p")" = leader ] && { echo "$p"; return 0; }; done; return 1; }
letter_of() { [ "$1" = 8101 ] && echo a || echo b; }
other_port() { [ "$1" = 8101 ] && echo 8102 || echo 8101; }

# Повний шлях запису: успішний вхід адміна (спершу рядок журналу дій у PostgreSQL, потім сесія).
write_ok() {
    code="$(curl --ssl-no-revoke -s --max-time 5 -o /dev/null -w '%{http_code}' --cacert "$CA" -H 'Content-Type: application/json' \
        -d "{\"username\":\"admin\",\"password\":\"$SIM_DASH_PASSWORD\"}" "https://127.0.0.1:$1/api/login" 2>/dev/null || true)"
    [ "$code" = 200 ]
}

# SQL на вузлі PostgreSQL, що зараз приймає запис (не репліка).
pg_exec() {
    for n in pg-a pg-b; do
        if dc exec -T "$n" psql -U postgres -h /var/run/postgresql -d dronewatch -tAc "SELECT NOT pg_is_in_recovery()" 2>/dev/null | grep -q t; then
            dc exec -T "$n" psql -U postgres -h /var/run/postgresql -d dronewatch -tAc "$1"
            return 0
        fi
    done
    return 1
}
pg_master() {
    for n in a b; do
        dc exec -T "pg-$n" psql -U postgres -h /var/run/postgresql -d dronewatch -tAc "SELECT NOT pg_is_in_recovery()" 2>/dev/null | grep -q t && { echo "$n"; return 0; }
    done
    return 1
}
redis_master_ip() {
    dc exec -T sentinel-w redis-cli --tls --cacert /tmp/tls/ca.crt -p 26379 -a "$REDIS_PASSWORD" --no-auth-warning \
        sentinel get-master-addr-by-name dw 2>/dev/null | head -1 | tr -d '\r'
}

sample_split() {   # викликається в циклі очікування: обидві копії лідери одночасно = split-brain
    a="$(role_of 8101)"; b="$(role_of 8102)"
    if [ "$a" = leader ] && [ "$b" = leader ]; then SPLIT=$((SPLIT + 1)); fi
}

# wait_until ОПИС КОМАНДА...: чекає успіху, міряє секунди від START; провал, якщо довше за LIMIT
wait_until() {
    what="$1"; shift
    while ! "$@" >/dev/null 2>&1; do
        sample_split
        el=$(($(now) - START))
        [ "$el" -le "$LIMIT" ] || { echo "ПРОВАЛ: за $LIMIT с не дочекались: $what" >&2; exit 1; }
        sleep 1
    done
    echo "recovery[$what]=$(($(now) - START))s"
}

leader_writes() { p="$(leader_port)" && write_ok "$p"; }

final_checks() {
    echo "== перевірка після сценарію"
    if [ "$SPLIT" -gt 0 ]; then echo "ПРОВАЛ: split-brain: обидві копії одночасно лідери ($SPLIT вибірок)" >&2; exit 1; fi
    dsn="postgresql://dw_app@10.77.0.11:5432,10.77.0.12:5432/dronewatch?sslmode=verify-full&target_session_attrs=read-write"
    ok=0
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if dc run --rm --no-deps -T -e PGSSLROOTCERT=/certs/ca.crt -e "DRONEWATCH_PG_PASSWORD=$DW_APP_PASSWORD" \
            --entrypoint dronewatch-audit service-a --verify --dsn "$dsn"; then ok=1; break; fi
        sleep 3
    done
    [ "$ok" = 1 ] || { echo "ПРОВАЛ: ланцюжок журналу дій не пройшов перевірку" >&2; exit 1; }
    dups="$(pg_exec "SELECT count(*) FROM (SELECT zone, level, round(ts::numeric) FROM incidents GROUP BY 1,2,3 HAVING count(*) > 1) d" | tr -d '[:space:]')"
    [ "${dups:-0}" = 0 ] || { echo "ПРОВАЛ: дублі тривог у журналі: $dups" >&2; exit 1; }
    echo "OK: журнал дій цілий, дублів тривог нема, split-brain не помічено"
}

if ! lp="$(leader_port)"; then echo "стенд не готовий: жодна копія не лідер (sh deploy/ha-cluster/sim/sim.sh status)" >&2; exit 2; fi
write_ok "$lp" || { echo "стенд не готовий: лідер не пише (вхід адміна не 200)" >&2; exit 2; }
epoch_before="$(pg_exec "SELECT epoch FROM ha_epoch" | tr -d '[:space:]')"
echo "== сценарій $SCENARIO; лідер сервісу: node-$(letter_of "$lp") (порт $lp), epoch $epoch_before, ліміт ${LIMIT}s"

case "$SCENARIO" in
    kill-leader)
        START="$(now)"
        docker kill "dwsim-service-$(letter_of "$lp")-1" >/dev/null
        wait_until "інша копія стала лідером і пише" leader_writes
        newp="$(leader_port)"
        [ "$newp" != "$lp" ] || { echo "ПРОВАЛ: лідер не змінився" >&2; exit 1; }
        epoch_after="$(pg_exec "SELECT epoch FROM ha_epoch" | tr -d '[:space:]')"
        [ "$epoch_after" -gt "$epoch_before" ] || { echo "ПРОВАЛ: epoch не зріс ($epoch_before -> $epoch_after)" >&2; exit 1; }
        ;;
    kill-pg-master)
        m="$(pg_master)"
        echo "master PostgreSQL: pg-$m"
        START="$(now)"
        docker kill "dwsim-pg-$m-1" >/dev/null
        other=a; [ "$m" = a ] && other=b
        wait_until "новий master PostgreSQL ($other)" sh -c "docker compose exec -T pg-$other psql -U postgres -h /var/run/postgresql -d dronewatch -tAc 'SELECT NOT pg_is_in_recovery()' | grep -q t"
        wait_until "лідер сервісу пише в новий master" leader_writes
        ;;
    kill-redis-master)
        mip="$(redis_master_ip)"
        m=a; [ "$mip" = 10.77.0.12 ] && m=b
        echo "master Redis: redis-$m ($mip)"
        START="$(now)"
        docker kill "dwsim-redis-$m-1" >/dev/null
        wait_until "Sentinel обрав нового master" sh -c "[ \"\$(docker compose exec -T sentinel-w redis-cli --tls --cacert /tmp/tls/ca.crt -p 26379 -a '$REDIS_PASSWORD' --no-auth-warning sentinel get-master-addr-by-name dw 2>/dev/null | head -1 | tr -d '\\r')\" != '$mip' ]"
        wait_until "вхід у дашборд (сесії в Redis) знову працює" leader_writes
        [ "$(leader_port)" = "$lp" ] || { echo "ПРОВАЛ: лідер сервісу змінився через Redis" >&2; exit 1; }   # Redis поза шляхом запису
        ;;
    isolate-node-a)
        heal() { docker network connect --ip 10.77.0.11 dwsim_dwsim dwsim-node-a-1 >/dev/null 2>&1 || true; }
        trap heal EXIT   # що б не сталось, вузол A повертається в мережу (інакше стенд лишився б розрізаним)
        START="$(now)"
        docker network disconnect dwsim_dwsim dwsim-node-a-1
        wait_until "B стала лідером і пише" sh -c "[ \"\$(curl --ssl-no-revoke -s --max-time 2 --cacert $CA https://127.0.0.1:8102/readyz | sed -n 's/.*\"role\":\"\\([a-z]*\\)\".*/\\1/p')\" = leader ]"
        wait_until "запис працює" leader_writes
        echo "тримаємо розрив ще 15 с (стежимо за split-brain)"
        end=$(($(now) + 15))
        while [ "$(now)" -lt "$end" ]; do sample_split; sleep 1; done
        START="$(now)"
        docker network connect --ip 10.77.0.11 dwsim_dwsim dwsim-node-a-1
        wait_until "A повернулась і лишається не лідером" sh -c "[ \"\$(curl --ssl-no-revoke -s --max-time 2 --cacert $CA https://127.0.0.1:8101/readyz | sed -n 's/.*\"role\":\"\\([a-z]*\\)\".*/\\1/p')\" = standby ]"
        wait_until "запис працює після повернення A" leader_writes
        ;;
    kill-witness)
        START="$(now)"
        docker stop dwsim-etcd-w-1 dwsim-sentinel-w-1 >/dev/null
        end=$(($(now) + 40))
        while [ "$(now)" -lt "$end" ]; do
            sample_split
            leader_writes || { echo "ПРОВАЛ: без свідка запис зупинився (кворум 2 з 3 мав вистачити)" >&2; exit 1; }
            sleep 2
        done
        [ "$(leader_port)" = "$lp" ] || { echo "ПРОВАЛ: втрата свідка не повинна міняти лідера" >&2; exit 1; }
        echo "40 с без свідка: лідер той самий, запис працює"
        docker start dwsim-etcd-w-1 dwsim-sentinel-w-1 >/dev/null
        ;;
    *)
        echo "невідомий сценарій '$SCENARIO'. Дивіться шапку цього файлу." >&2
        exit 2
        ;;
esac
final_checks
