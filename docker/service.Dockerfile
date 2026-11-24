# Збирати з кореня репозиторію: docker build -f docker/service.Dockerfile .
FROM python:3.15-slim
WORKDIR /app
# Залежності беруться з pyproject.toml і ставляться окремим шаром: він перезбирається лише при зміні залежностей, а не коду
COPY pyproject.toml .
# + extra redis: без нього ні спільні сесії, ні HA (профіль ha) не стартують; + extra postgres: HA на двох вузлах (storage.backend:
# postgres). На роботу однієї копії на SQLite обидва не впливають
RUN python -c "import tomllib; p = tomllib.load(open('pyproject.toml','rb'))['project']; print('\n'.join(p['dependencies'] + p['optional-dependencies']['redis'] + p['optional-dependencies']['postgres']))" > /tmp/req.txt \
    && pip install --no-cache-dir -r /tmp/req.txt && rm /tmp/req.txt
COPY src src
RUN pip install --no-cache-dir --no-deps .
ENV DB_PATH=/data/incidents.db PYTHONUNBUFFERED=1
RUN mkdir -p /data
EXPOSE 8000
# HTTPS: сертифікат і ключ сервісу задаються TLS_CERT_FILE / TLS_KEY_FILE (див. docker-compose.yml); без них сервіс не стартує
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s CMD ["python", "-m", "dronewatch.service.healthcheck"]
CMD ["python", "-m", "dronewatch.service.serve"]
