#!/bin/bash
set -euo pipefail

# ============================================================
# Zinrai Livestream — Server Setup Script
# Run once on a fresh Ubuntu 24.04 droplet as root
# ============================================================

echo "=== [1/6] System update ==="
apt-get update && apt-get upgrade -y
apt-get install -y curl git ufw certbot

echo "=== [2/6] Install Docker ==="
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
fi

echo "=== [3/6] Install Docker Compose plugin ==="
if ! docker compose version &> /dev/null; then
    apt-get install -y docker-compose-plugin
fi

echo "=== [4/6] Mount Block Volume at /dvr ==="
# DigitalOcean block volumes appear as /dev/sda (check with lsblk)
VOLUME_DEVICE="/dev/sda"
MOUNT_POINT="/dvr"

if ! mountpoint -q "$MOUNT_POINT"; then
    mkdir -p "$MOUNT_POINT"
    # Format only if not already formatted
    if ! blkid "$VOLUME_DEVICE" &> /dev/null; then
        echo "Formatting $VOLUME_DEVICE as ext4..."
        mkfs.ext4 "$VOLUME_DEVICE"
    fi
    mount "$VOLUME_DEVICE" "$MOUNT_POINT"
    # Persist mount across reboots
    DEVICE_UUID=$(blkid -s UUID -o value "$VOLUME_DEVICE")
    echo "UUID=$DEVICE_UUID $MOUNT_POINT ext4 defaults,nofail,discard 0 2" >> /etc/fstab
    echo "Block volume mounted at $MOUNT_POINT"
else
    echo "Block volume already mounted at $MOUNT_POINT"
fi

mkdir -p /dvr/live
chmod 777 /dvr

echo "=== [5/6] Firewall ==="
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 1935/tcp   # RTMP
ufw --force enable

echo "=== [6/6] Clone repo ==="
if [ ! -d /opt/livestream ]; then
    # Use GitHub PAT for private repo
    REPO_URL="https://livestreamserver:${GITHUB_TOKEN}@github.com/imlshane/livestreamsrs.git"
    git clone "$REPO_URL" /opt/livestream
    echo "Repo cloned to /opt/livestream"
else
    echo "Repo already exists at /opt/livestream"
fi

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "  1. cd /opt/livestream"
echo "  2. Copy .env: scp .env root@SERVER:/opt/livestream/.env"
echo "  3. Issue TLS cert: certbot certonly --standalone -d livestream.zinrai.live"
echo "  4. Run: docker compose up -d"
echo "  5. Run migrations: docker compose exec backend alembic upgrade head"
