FROM python:3.11.14-slim

WORKDIR /srv
COPY pyproject.toml ./
COPY requirements.lock ./
COPY app ./app
COPY tools/build_static.py ./tools/build_static.py
RUN python tools/build_static.py && pip install --no-cache-dir --constraint requirements.lock .

# Запасной путь, если том смонтирован в /data. Иначе app.config.resolve_db_path
# переносит файл в RAILWAY_VOLUME_MOUNT_PATH: каталог образа не переживает деплой.
ENV HTF_DB_PATH=/data/htf_zones.db
RUN useradd --uid 10001 --create-home htf && mkdir -p /data && chown htf:htf /data
ENV HTF_HOST=0.0.0.0
USER htf

EXPOSE 8000
CMD ["python", "-m", "app.main"]
