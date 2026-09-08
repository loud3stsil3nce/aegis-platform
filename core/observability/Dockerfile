FROM python:3.12-slim
WORKDIR /app
COPY core/observability/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY core /app/core
COPY sdk/python /app/sdk/python
ENV PYTHONPATH=/app:/app/sdk/python
RUN useradd --create-home --uid 10001 aegis
USER aegis
EXPOSE 8020
CMD ["uvicorn", "core.observability.server:app", "--host", "0.0.0.0", "--port", "8020"]
