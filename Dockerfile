FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Tashkent
WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
COPY edge ./edge
RUN pip install --no-cache-dir .
COPY migrations ./migrations
COPY alembic.ini ./
RUN useradd -r -u 1000 app && mkdir -p /srv/storage && chown app /srv/storage
USER app
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
