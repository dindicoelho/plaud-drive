FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    LOG_PATH=/data/bot.log \
    LOCK_PATH=/data/plaud-drive.lock

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py agent.py drive_client.py plaud_client.py processor.py models.py setup_drive.py ./

RUN mkdir -p /data

CMD ["python", "-u", "bot.py"]
