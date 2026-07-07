FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV JUDY_DB_PATH=/data/judy.db
ENV PYTHONPATH=/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests
COPY README.md ./README.md

RUN mkdir -p /data

EXPOSE 50052

CMD ["python", "-m", "app.grpc_server"]
