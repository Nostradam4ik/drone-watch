#!/bin/sh
# Sentinel: три екземпляри (вузол A, вузол B, свідок), кворум 2. Лише TLS, пароль і для доступу до Sentinel, і для master.
# Конфіг пишеться один раз у /data: Sentinel сам дописує в нього відкриті репліки й поточного master.
set -eu

for v in NODE_IP A_IP REDIS_PASSWORD; do
    eval "[ -n \"\${$v:-}\" ]" || { echo "sentinel: змінна $v не задана" >&2; exit 1; }
done
case "$REDIS_PASSWORD" in
    *[!A-Za-z0-9_.-]*) echo "sentinel: пароль лише з символів A-Za-z0-9_.-" >&2; exit 1 ;;
esac
MASTER="${REDIS_MASTER_NAME:-dw}"

install -d -m 0700 -o redis -g redis /tmp/tls /data
for f in redis.crt redis.key ca.crt; do
    install -m 0600 -o redis -g redis "/certs/$f" "/tmp/tls/$f"
done

if [ ! -s /data/sentinel.conf ]; then
    umask 077
    cat > /data/sentinel.conf <<EOF
port 0
tls-port 26379
tls-cert-file /tmp/tls/redis.crt
tls-key-file /tmp/tls/redis.key
tls-ca-cert-file /tmp/tls/ca.crt
tls-auth-clients no
tls-replication yes
requirepass "$REDIS_PASSWORD"
dir /data
sentinel monitor $MASTER $A_IP 6379 2
sentinel auth-pass $MASTER $REDIS_PASSWORD
sentinel sentinel-pass $REDIS_PASSWORD
sentinel down-after-milliseconds $MASTER 5000
sentinel failover-timeout $MASTER 15000
sentinel parallel-syncs $MASTER 1
sentinel announce-ip $NODE_IP
sentinel announce-port 26379
EOF
    chown redis:redis /data/sentinel.conf
fi

exec gosu redis redis-sentinel /data/sentinel.conf
