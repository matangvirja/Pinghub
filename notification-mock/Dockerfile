FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY notification-mock/requirements.txt /app/notification-mock/requirements.txt
RUN pip install --no-cache-dir -r /app/notification-mock/requirements.txt

COPY common /app/common
COPY notification-mock /app/notification-mock

WORKDIR /app/notification-mock
EXPOSE 8002

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002", "--no-access-log"]
