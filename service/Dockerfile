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
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2).status == 200 else 1)"
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
