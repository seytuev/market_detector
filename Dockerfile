FROM python:3.11-slim

WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

ENV HTF_DB_PATH=/data/htf_zones.db
VOLUME /data

EXPOSE 8000
CMD ["python", "-m", "app.main"]
