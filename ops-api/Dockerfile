FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY ops-api/requirements.txt /app/ops-api/requirements.txt
RUN pip install --no-cache-dir -r /app/ops-api/requirements.txt

COPY common /app/common
COPY ops-api /app/ops-api

WORKDIR /app/ops-api
EXPOSE 8003

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8003", "--no-access-log"]
