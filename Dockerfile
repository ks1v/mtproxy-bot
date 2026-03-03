FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r botuser && useradd -r -g botuser -s /sbin/nologin botuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mtproxy-bot.py .
COPY log_cron.py .
COPY supervisord.conf /etc/supervisor/conf.d/guroo-bot.conf

# Pre-create /data and give ownership to botuser
RUN mkdir -p /data && chown botuser:botuser /data

VOLUME ["/data"]

USER botuser

CMD ["supervisord", "-c", "/etc/supervisor/conf.d/mtproxy-bot.conf"]
