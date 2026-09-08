FROM python:3.12-slim
WORKDIR /app
COPY core /app/core
ENV PYTHONPATH=/app
RUN useradd --create-home --uid 10001 aegis
USER aegis
CMD ["python", "-m", "core.agent_runtime.verify_ollama"]
