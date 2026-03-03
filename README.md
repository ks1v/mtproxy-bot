# mtproxy-bot

Private Telegram bot for managing an MTProto proxy ([telemt](https://github.com/telemt/telemt)).  
Runs alongside the proxy on the same VPS in a single Docker container.

## What it does

- Issues and revokes per-user proxy secrets in `telemt.toml`
- Aggregates proxy connection stats from Docker logs every 5 minutes
- Reports usage by user with anomaly detection (leaked keys, high error rates)

## Architecture

```
/opt/telemt/
├── docker-compose.yml     # runs telemt + mtproxy-bot
├── telemt.toml            # source of truth for proxy secrets (rw by bot)
├── .env                   # secrets — never committed
├── data/
│   ├── stats.json         # hourly bucketed usage stats, retained indefinitely
│   └── .log_cursor        # tracks last processed log position
├── bot/bot.py             # Telegram bot (python-telegram-bot)
└── cron/log_cron.py       # log aggregator, runs every 5 min via supervisord
```

Inside `mtproxy-bot` container, `supervisord` manages two processes:
- `bot.py` — always-on polling bot
- `log_cron.py` — digest loop (runs, sleeps 300s, repeats)

## Setup

### Prerequisites
- VPS with Docker + Docker Compose v2
- Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your Telegram numeric user ID

### Deploy

```bash
# On VPS
git clone git@github.com:YOURUSERNAME/mtproxy-bot.git /opt/telemt
cd /opt/telemt

# Create .env (not in repo)
cat > .env << 'END'
BOT_TOKEN=your_bot_token
OWNER_ID=your_telegram_id
DOCKER_GID=$(getent group docker | cut -d: -f3)
END

sudo bash setup.sh
```

`setup.sh` will:
1. Migrate `telemt.toml` from single-secret to named-user format (backs up original)
2. Stop the existing standalone `telemt` container
3. Build and start both containers via Compose

### telemt.toml format after migration

```toml
[censorship]
tls_domain = "1c.ru"

[server]
listen = "0.0.0.0:443"

[mask]
host = "1c.ru"
port = 443

[access.users]
# managed by bot — username = "32 hex char secret"
alice = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
```

## Bot usage

The primary interface is plain text — just send a username:

| Input | Action |
|-------|--------|
| `@alice` or `alice` | Find or create key, return proxy link |
| `@alice @bob @carol` | Batch find/create |
| `/list` | Paginated user list with Send / Reset / Delete buttons |
| `/revoke @alice` | Delete key immediately |
| `/stats day\|week\|month` | Usage report with anomaly detection |
| `/batch @u1 @u2 ...` | Explicit batch command |

Proxy link format returned to you (for forwarding):
```
https://t.me/proxy?server=<YOUR_VPS_IP>&port=443&secret=ee<hex>
нажать на ссылку → добавить → работает само 🚀
```

> **Note:** Bot cannot send messages to users directly — Telegram bots can only
> message users who have started them first. Forward the link manually.

After adding/revoking users, restart telemt to apply changes:
```bash
docker compose restart telemt
```

## Stats & anomaly detection

`/stats week` output example:
```
📊 Статистика за 7 дней

👤 alice
   12,400 подкл. (38%) · 120 ошибок (1.0%) | tg timeout

👤 bob
   8,200 подкл. (25%) · 44 ошибок (0.5%)

Итого: 32,600 подключений · 280 ошибок · 0.9% error rate

⚠️ alice — 38% всего трафика (возможна утечка ключа)
```

Anomaly thresholds:
- `> 40%` of total traffic from one user → possible key leak
- `> 15%` error rate → connection quality issue

## Log management

telemt logs are capped at **150 MB** (3 × 50 MB files, Docker log rotation).  
`log_cron.py` digests logs into `stats.json` before rotation discards them.  
To reduce log volume, set `RUST_LOG=warn` in docker-compose.yml — drops ~95% of
log lines while keeping all error information.

## Operations

```bash
# View live logs
docker compose logs -f

# Restart bot only (after code changes)
docker compose restart mtproxy-bot

# Rebuild after code changes
docker compose up -d --build mtproxy-bot

# Inspect current users
grep -A99 '\[access.users\]' /opt/telemt/telemt.toml
```

## Security

- Bot only responds to `OWNER_ID` — all other users are silently ignored
- `botuser` (non-root) runs inside the container
- Docker socket is mounted read-only (needed for `docker logs telemt`)
- `.env`, `data/`, `telemt.toml` are gitignored — never committed
- `telemt.toml` on host: `chmod 640` recommended
