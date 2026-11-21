# QC V6 — one image, several entry points.  docker build -t qc6 .
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY qc6 /app/qc6
COPY engine /app/engine
COPY web/dist /app/web/dist
ENV PYTHONPATH=/app:/app/engine QC6_DATA_DIR=/data
RUN useradd -m qc && mkdir -p /data && chown qc /data
USER qc
EXPOSE 8003
CMD ["uvicorn", "qc6.api.app:app", "--host", "0.0.0.0", "--port", "8003"]
