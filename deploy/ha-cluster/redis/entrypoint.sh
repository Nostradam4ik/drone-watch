#!/bin/sh
# Redis кластера: лише TLS (звичайного порту нема), пароль обов'язковий, реплікація теж по TLS. Конфіг пишеться ОДИН раз у /data
# (том): далі його переписує Sentinel при перемиканні master, і після перезапуску вузол піднімається з уже актуальною роллю.
set -eu

for v in NODE_IP REDIS_PASSWORD; do
    eval "[ -n \"\${$v:-}\" ]" || { echo "redis: змінна $v не задана" >&2; exit 1; }
done
case "$REDIS_PASSWORD" in
    *[!A-Za-z0-9_.-]*) echo "redis: пароль лише з символів A-Za-z0-9_.-" >&2; exit 1 ;;
esac

install -d -m 0700 -o redis -g redis /tmp/tls /data
for f in redis.crt redis.key ca.crt; do
    install -m 0600 -o redis -g redis "/certs/$f" "/tmp/tls/$f"
done

if [ ! -s /data/redis.conf ]; then
    umask 077
    cat > /data/redis.conf <<EOF
port 0
tls-port 6379
bind 0.0.0.0
protected-mode yes
tls-cert-file /tmp/tls/redis.crt
tls-key-file /tmp/tls/redis.key
tls-ca-cert-file /tmp/tls/ca.crt
tls-auth-clients no
tls-replication yes
requirepass "$REDIS_PASSWORD"
masterauth "$REDIS_PASSWORD"
replica-announce-ip $NODE_IP
replica-announce-port 6379
save ""
appendonly no
dir /data
EOF
    # REDIS_REPLICAOF: адреса початкового master (лише для другого вузла); далі роль веде Sentinel
    [ -z "${REDIS_REPLICAOF:-}" ] || echo "replicaof $REDIS_REPLICAOF 6379" >> /data/redis.conf
    chown redis:redis /data/redis.conf
fi

exec gosu redis redis-server /data/redis.conf
