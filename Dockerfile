# The Persian campaign dashboard's server image (docs/deploy.md). It only
# needs environment variables, the data/ volume, and /healthz.

# SQLite from its own source. Debian's (3.46.1 in trixie) has the WAL-reset
# bug: a write and a checkpoint at the same instant, on two connections, can
# lose committed pages and corrupt the database, and the web, the worker and
# the send's threads write at once. Fixed in SQLite 3.51.3. The hash is of
# sqlite.org's file (its page publishes the SHA3-256; both were checked).
FROM python:3.13 AS sqlite
ARG SQLITE_TARBALL=2026/sqlite-autoconf-3530400.tar.gz
ARG SQLITE_SHA256=0e9483900e92cd5de8fd48d16bf9200145a61f7fd5be542a5ac81d8a9516eb9c
WORKDIR /build
RUN curl -fsSLo sqlite.tar.gz "https://www.sqlite.org/${SQLITE_TARBALL}" \
    && echo "${SQLITE_SHA256}  sqlite.tar.gz" | sha256sum -c - \
    && tar -xzf sqlite.tar.gz --strip-components=1 \
    && ./configure --prefix=/opt/sqlite --disable-static --disable-readline \
    && make -j"$(nproc)" install

FROM python:3.13-slim
# Python's sqlite3 loads that SQLite, not Debian's (the library path comes
# before the system's), and the build stops if it doesn't.
COPY --from=sqlite /opt/sqlite/lib/ /opt/sqlite/lib/
ENV LD_LIBRARY_PATH=/opt/sqlite/lib
RUN python -c "import sqlite3, sys; sys.exit(sqlite3.sqlite_version_info < (3, 51, 3) \
    and f'SQLite {sqlite3.sqlite_version} has the WAL-reset bug')"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=sms_sender_web.settings \
    SMS_SENDER_DATA_DIR=/app/data \
    DJANGO_STATIC_ROOT=/app/static

WORKDIR /app
# Exactly the locked versions, each checked against its hash, so two builds
# of one commit install the same thing. Updating: requirements/server.in.
COPY requirements/server.lock ./requirements/
RUN pip install --require-hashes -r requirements/server.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps .
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
