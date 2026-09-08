FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

RUN useradd --system --uid 10001 observer

# Access to the mounted Docker socket is granted at runtime through the socket's
# group. The process otherwise has no writable application filesystem.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8010"]
