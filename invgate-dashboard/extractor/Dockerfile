FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Entrypoint: genera el crontab dinamicamente desde CRON_SCHEDULE y arranca
CMD ["sh", "-c", "\
    echo \"${CRON_SCHEDULE:-0 * * * *} cd /app && python extract.py >> /data/extract.log 2>&1\" > /etc/cron.d/invgate && \
    chmod 0644 /etc/cron.d/invgate && \
    crontab /etc/cron.d/invgate && \
    echo 'Corriendo carga inicial...' && \
    python extract.py && \
    echo 'Carga inicial lista. Iniciando cron...' && \
    cron -f \
"]
