# The Persian campaign dashboard, for local testing with Docker Compose
# (Phase 3). Production hosting is DevOps' decision: the image only needs
# environment variables, the data/ volume, and /healthz.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=sms_sender_web.settings \
    SMS_SENDER_DATA_DIR=/app/data \
    DJANGO_STATIC_ROOT=/app/static

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[web]"
COPY manage.py ./
# Static files are collected at build time; this key is only for that step.
RUN DJANGO_SECRET_KEY=collectstatic-only python manage.py collectstatic --noinput

RUN useradd --create-home --uid 1000 app && mkdir -p /app/data && chown app /app/data
USER app
EXPOSE 8000
# Threads, not gunicorn's sync workers: browsers reach gunicorn directly (no
# buffering proxy) and open idle connections ahead of time. Each would hold a
# sync worker until it was killed, and the next page got "Internal Server Error".
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn sms_sender_web.wsgi:application --bind 0.0.0.0:8000 --workers 2 --worker-class gthread --threads 4 --access-logfile -"]
