FROM python:3.11-slim

WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

# Запасной путь, если том смонтирован в /data. Иначе app.config.resolve_db_path
# переносит файл в RAILWAY_VOLUME_MOUNT_PATH: каталог образа не переживает деплой.
ENV HTF_DB_PATH=/data/htf_zones.db
RUN mkdir -p /data

EXPOSE 8000
CMD ["python", "-m", "app.main"]
