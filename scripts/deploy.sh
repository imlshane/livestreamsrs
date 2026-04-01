#!/bin/bash
set -euo pipefail

# ============================================================
# Zinrai Livestream — Deploy / Update Script
# Run on server inside /opt/livestream after setup_server.sh
# ============================================================

REPO_DIR="/opt/livestream"
cd "$REPO_DIR"

echo "=== Pull latest code ==="
git pull origin main

echo "=== Build containers ==="
docker compose build --no-cache

echo "=== Restart services ==="
docker compose up -d --remove-orphans

echo "=== Run DB migrations ==="
sleep 5  # wait for backend to be ready
docker compose exec -T backend alembic upgrade head

echo "=== Status ==="
docker compose ps

echo "=== Deploy complete ==="
