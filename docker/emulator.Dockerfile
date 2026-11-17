# Збирати з кореня репозиторію: docker build -f docker/emulator.Dockerfile .
FROM python:3.15-slim
WORKDIR /app
# Емулятор самодостатній (лише стандартна бібліотека), тож пакет dronewatch у образ не ставимо
COPY src/dronewatch/emulator/emulate.py .
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "emulate.py"]
