FROM python:3.12-slim

ARG AEGIS_LIFECYCLE_UID=10002
ARG AEGIS_LIFECYCLE_GID=10002

WORKDIR /app

COPY services/docker-lifecycle/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/docker-lifecycle/main.py .

RUN groupadd --system --gid ${AEGIS_LIFECYCLE_GID} lifecycle \
    && useradd --system --uid ${AEGIS_LIFECYCLE_UID} --gid ${AEGIS_LIFECYCLE_GID} lifecycle \
    && mkdir -p /var/lib/aegis-lifecycle \
    && chown lifecycle /var/lib/aegis-lifecycle

USER lifecycle

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8011"]
