#!/usr/bin/env bash
# setup.sh — run once on VPS after git clone
# Usage: sudo bash setup.sh
set -e

DEPLOY_DIR="/opt/telemt"

echo "=== MTProxy Bot Setup ==="

# 1. Copy project files into /opt/telemt
echo "[1/6] Copying files to $DEPLOY_DIR..."
cp -r mtproxy-bot.py log-cron.py Dockerfile requirements.txt supervisord.conf "$DEPLOY_DIR/"

# 2. Copy .env (already has correct values)
echo "[2/6] Installing .env..."
cp .env "$DEPLOY_DIR/.env"
chmod 600 "$DEPLOY_DIR/.env"

# 3. Copy docker-compose.yml
echo "[3/6] Installing docker-compose.yml..."
cp docker-compose.yml "$DEPLOY_DIR/docker-compose.yml"

# 4. Migrate telemt.toml if it's still the old single-secret format
echo "[4/6] Checking telemt.toml format..."
if grep -q '^\[access\.users\]' "$DEPLOY_DIR/telemt.toml"; then
    echo "      telemt.toml already has [access.users] — skipping migration"
else
    echo "      Migrating to named-user format..."
    # Back up old config
    cp "$DEPLOY_DIR/telemt.toml" "$DEPLOY_DIR/telemt.toml.bak"
    echo "      Backup saved to telemt.toml.bak"
    # Remove old single secret line, append new section
    sed -i '/^secret\s*=/d' "$DEPLOY_DIR/telemt.toml"
    echo "" >> "$DEPLOY_DIR/telemt.toml"
    echo "[access.users]" >> "$DEPLOY_DIR/telemt.toml"
    echo "# Add users below. Bot will manage this section." >> "$DEPLOY_DIR/telemt.toml"
    echo "      Done. Add users via the bot or edit telemt.toml directly."
fi

# 5. Create data directory and fix telemt.toml permissions
echo "[5/6] Creating data directory and setting permissions..."
mkdir -p "$DEPLOY_DIR/data"
# botuser inside container maps to nobody (system user) —
# make it world-writable so botuser can write stats
chmod 777 "$DEPLOY_DIR/data"
# telemt.toml must be writable by botuser inside the container
chmod 666 "$DEPLOY_DIR/telemt.toml"

# 6. Stop old standalone telemt container, start everything via compose
echo "[6/6] Restarting containers..."
docker stop telemt 2>/dev/null && docker rm telemt 2>/dev/null || true
cd "$DEPLOY_DIR"
docker compose up -d --build

echo ""
echo "=== Done! ==="
echo ""
echo "Check logs:     docker compose -f $DEPLOY_DIR/docker-compose.yml logs -f"
echo "Check bot:      send /start to your bot on Telegram"
echo "Add users:      send @username to the bot"
echo ""
echo "NOTE: telemt.toml has no users yet — add them via the bot before"
echo "      sharing any proxy links."
