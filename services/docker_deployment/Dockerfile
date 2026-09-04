FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ARG AEGIS_DEPLOYMENT_UID=10003
ARG AEGIS_DOCKER_GID=983

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends docker-cli docker-compose \
    && rm -rf /var/lib/apt/lists/*

COPY services/docker_deployment/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/__init__.py /app/services/__init__.py
COPY services/docker_deployment /app/services/docker_deployment

RUN groupadd --system --gid ${AEGIS_DOCKER_GID} docker-host \
    && useradd --system --uid ${AEGIS_DEPLOYMENT_UID} --gid docker-host deployment \
    && mkdir -p /var/lib/aegis-deployment \
    && chown deployment:docker-host /var/lib/aegis-deployment

USER deployment

CMD ["uvicorn", "services.docker_deployment.main:app", "--host", "0.0.0.0", "--port", "8013"]
