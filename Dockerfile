FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLUGIN_HOST=0.0.0.0 \
    PLUGIN_PORT=8018 \
    PLUGIN_DATA_DIR=/app/data

COPY pyproject.toml setup.py README.md ./
COPY src ./src

RUN pip install --no-cache-dir . && mkdir -p /app/data /app/exports

EXPOSE 8018

CMD ["python", "-m", "tg_plugin", "serve"]
