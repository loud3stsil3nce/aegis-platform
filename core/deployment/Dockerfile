FROM python:3.12-slim

ARG AEGIS_DEPLOYMENT_UID=10003
ARG AEGIS_DEPLOYMENT_GID=10003

WORKDIR /app
COPY core/deployment/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY core /app/core

RUN groupadd --system --gid ${AEGIS_DEPLOYMENT_GID} deployment \
    && useradd --system --uid ${AEGIS_DEPLOYMENT_UID} --gid ${AEGIS_DEPLOYMENT_GID} deployment \
    && mkdir -p /var/lib/aegis-deployment \
    && chown deployment /var/lib/aegis-deployment
USER deployment

CMD ["uvicorn", "core.deployment.server:app", "--host", "0.0.0.0", "--port", "8012"]
