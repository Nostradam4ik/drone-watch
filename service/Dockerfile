# Збирати з кореня репозиторію: docker build -f service/Dockerfile .
FROM python:3.12-slim
WORKDIR /app
COPY service/requirements.txt service/requirements.txt
RUN pip install --no-cache-dir -r service/requirements.txt
COPY fusion_proto fusion_proto
COPY service service
WORKDIR /app/service
ENV DB_PATH=/data/incidents.db PYTHONUNBUFFERED=1
RUN mkdir -p /data
EXPOSE 8000
# HTTPS: сертифікат і ключ сервісу задаються TLS_CERT_FILE / TLS_KEY_FILE (див. docker-compose.yml); без них сервіс не стартує
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s CMD ["python", "healthcheck.py"]
CMD ["python", "serve.py"]
