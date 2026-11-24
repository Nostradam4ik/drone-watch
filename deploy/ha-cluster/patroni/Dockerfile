# PostgreSQL 16 під керуванням Patroni (лідерство в etcd). Spilo не береться: він важкий і заточений під AWS/Kubernetes.
# Збирати на кожному вузлі даних (A і B): docker compose -f docker-compose.node.yml build postgres
FROM postgres:16
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-pip python3-venv gettext-base ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/patroni \
    && /opt/patroni/bin/pip install --no-cache-dir "patroni[etcd3]>=4.0,<5" psycopg2-binary
COPY entrypoint.sh patroni.yml.tmpl post_bootstrap.sh /opt/dw/
RUN chmod 0755 /opt/dw/entrypoint.sh /opt/dw/post_bootstrap.sh
ENTRYPOINT ["/opt/dw/entrypoint.sh"]
