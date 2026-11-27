#!/bin/sh
# Старт Patroni: приватні ключі копіюються з read-only монтування в каталог, який читає лише postgres (ключі на хості лишаються
# 0600 власника-оператора), конфіг збирається з шаблону змінними оточення (паролі в образ і в compose-файл не потрапляють).
set -eu

for v in CLUSTER_SCOPE NODE_NAME NODE_IP A_IP B_IP W_IP PG_SUPERUSER_PASSWORD PG_REPLICATION_PASSWORD DW_OWNER_PASSWORD DW_APP_PASSWORD; do
    eval "[ -n \"\${$v:-}\" ]" || { echo "patroni: змінна $v не задана" >&2; exit 1; }
done
case "$PG_SUPERUSER_PASSWORD$PG_REPLICATION_PASSWORD" in
    *[!A-Za-z0-9_.-]*) echo "patroni: паролі лише з символів A-Za-z0-9_.- (вони йдуть у YAML без лапок)" >&2; exit 1 ;;
esac

install -d -m 0700 -o postgres -g postgres /tmp/tls
for f in pg.crt pg.key etcd.crt etcd.key ca.crt; do
    install -m 0600 -o postgres -g postgres "/certs/$f" "/tmp/tls/$f"
done
install -d -m 0700 -o postgres -g postgres /var/lib/postgresql/data

install -m 0600 -o postgres -g postgres /dev/null /tmp/patroni.yml
# Одинарні лапки навмисно: це перелік змінних для envsubst, а не підстановка оболонки.
# shellcheck disable=SC2016
envsubst '${CLUSTER_SCOPE} ${NODE_NAME} ${NODE_IP} ${A_IP} ${B_IP} ${W_IP} ${PG_SUPERUSER_PASSWORD} ${PG_REPLICATION_PASSWORD}' \
    < /opt/dw/patroni.yml.tmpl > /tmp/patroni.yml

exec gosu postgres /opt/patroni/bin/patroni /tmp/patroni.yml
