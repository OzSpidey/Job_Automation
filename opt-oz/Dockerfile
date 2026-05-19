FROM python:3.11-slim

WORKDIR /app

# System deps for ib_insync and pyarrow
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libffi-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Data directory for Parquet chain store
RUN mkdir -p /data/chains /data/surfaces /data/db

EXPOSE 8080

CMD ["python", "entrypoint.py"]
