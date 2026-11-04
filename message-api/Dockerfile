FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY message-api/requirements.txt /app/message-api/requirements.txt
RUN pip install --no-cache-dir -r /app/message-api/requirements.txt

COPY common /app/common
COPY message-api /app/message-api

WORKDIR /app/message-api
EXPOSE 8001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001", "--no-access-log"]
