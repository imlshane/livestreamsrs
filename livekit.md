# LiveKit Streaming Platform — Setup Guide

> **Goal:** Low-latency (<500ms) live streaming with automatic failover  
> **Stack:** LiveKit · Nginx · Node.js API · DO Managed Redis  
> **Viewers:** Up to 500 concurrent · **Streams:** Up to 6 simultaneous  
> **Protocol:** WebRTC only — no HLS, no fallback, no extra complexity  

---

## Table of Contents

1. [Architecture](#1-architecture)
2. [Provision Droplets](#2-provision-droplets)
3. [Initial Server Setup](#3-initial-server-setup)
4. [Install Docker](#4-install-docker)
5. [Directory Structure](#5-directory-structure)
6. [Environment Variables](#6-environment-variables)
7. [Generate LiveKit Keys](#7-generate-livekit-keys)
8. [LiveKit Configuration](#8-livekit-configuration)
9. [Ingress Configuration](#9-ingress-configuration)
10. [Nginx Configuration](#10-nginx-configuration)
11. [Node.js API](#11-nodejs-api)
12. [Docker Compose](#12-docker-compose)
13. [SSL Certificate](#13-ssl-certificate)
14. [Firewall Rules](#14-firewall-rules)
15. [Start Everything](#15-start-everything)
16. [OBS Configuration](#16-obs-configuration)
17. [Frontend Integration](#17-frontend-integration)
18. [Verify the Setup](#18-verify-the-setup)
19. [Phase 2 — Second Node + Load Balancer](#19-phase-2--second-node--load-balancer)
20. [Failure Recovery](#20-failure-recovery)
21. [Common Issues](#21-common-issues)

---

## 1. Architecture

```
OBS (RTMP)
    │
    ▼
DO Load Balancer  ←── health checks every 10s (Phase 2)
    │
    ├──── LiveKit Node 1 (c-4 · 4vCPU · 8GB · $84/mo)   ← Phase 1 starts here
    │         ├── LiveKit Server  (WebRTC SFU, port 7880)
    │         ├── LiveKit Ingress (RTMP → WebRTC, port 1935)
    │         ├── TURN relay      (built-in, ports 443/5349)
    │         ├── Node.js API     (port 3000)
    │         └── Nginx           (SSL termination, port 443/80)
    │
    └──── LiveKit Node 2 (c-4 · $84/mo)   ← Phase 2 adds this
              └── (same stack)
                    │
              DO Managed Redis
              (cluster coordination + stream state)
```

**Monthly cost:**
| Phase | Components | Cost |
|---|---|---|
| Phase 1 (now) | 1 node + Redis | ~$99/mo |
| Phase 2 (production) | 2 nodes + LB + Redis | ~$207/mo |

**Latency:** 200–500ms glass-to-glass

---

## 2. Provision Droplets

In DigitalOcean console, create **one droplet** for Phase 1:

```
Droplet type  : CPU-Optimized
Size          : c-4  (4 vCPU · 8 GB RAM)  —  $84/mo
OS            : Ubuntu 22.04 LTS x64
Region        : Choose closest to your streamers
Authentication: SSH key (recommended)
```

Note your droplet's **public IP** — referred to as `YOUR_SERVER_IP` below.

> **Phase 2:** Repeat for a second identical droplet in the same region.

---

## 3. Initial Server Setup

```bash
ssh root@YOUR_SERVER_IP

# Update system
apt update && apt upgrade -y
apt install -y curl wget git ufw htop net-tools

# Create non-root user (optional)
adduser streaming
usermod -aG sudo streaming
usermod -aG docker streaming
```

---

## 4. Install Docker

```bash
# Remove old versions
apt remove -y docker docker-engine docker.io containerd runc 2>/dev/null

# Install dependencies
apt install -y ca-certificates curl gnupg lsb-release

# Add Docker GPG key
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

# Add Docker repository
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu \
  $(lsb_release -cs) stable" \
  | tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker
apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Enable on boot
systemctl enable docker
systemctl start docker

# Verify
docker --version
docker compose version
```

---

## 5. Directory Structure

```bash
mkdir -p /opt/streaming/{livekit,nginx/conf,api/routes}

chmod -R 755 /opt/streaming
```

```
/opt/streaming/
├── docker-compose.yml
├── .env
├── livekit.yaml          ← LiveKit server config
├── ingress.yaml          ← LiveKit ingress config (RTMP)
├── nginx/
│   └── conf/
│       └── nginx.conf
└── api/
    ├── Dockerfile
    ├── package.json
    ├── index.js
    └── routes/
        ├── stream.js     ← token generation + stream management
        └── hooks.js      ← LiveKit webhooks
```

---

## 6. Environment Variables

```bash
cat > /opt/streaming/.env << 'EOF'
# ── Security ── generate all values below ──
LIVEKIT_API_KEY=replace-with-livekit-api-key
LIVEKIT_API_SECRET=replace-with-livekit-api-secret
JWT_SECRET=replace-with-64-char-random-string

# ── Domain ──
DOMAIN=livestream.zinrai.live
LIVEKIT_WS_URL=wss://livestream.zinrai.live

# ── Redis (DO Managed Redis — TLS required) ──
REDIS_URL=rediss://default:your-redis-password@your-do-redis-host.db.ondigitalocean.com:25061

# ── App ──
NODE_ENV=production
PORT=3000
EOF
```

Generate secrets:

```bash
# API key — short memorable string
echo "LIVEKIT_API_KEY: lk-$(openssl rand -hex 8)"

# API secret — must be long and random
echo "LIVEKIT_API_SECRET: $(openssl rand -hex 32)"

# JWT secret
echo "JWT_SECRET: $(openssl rand -hex 32)"
```

Replace placeholder values in `.env` with the generated strings.

---

## 7. Generate LiveKit Keys

Save these — you need them in both `livekit.yaml` and `.env`:

```bash
# These are just labels — use the values you generated above
LIVEKIT_API_KEY=your-api-key
LIVEKIT_API_SECRET=your-api-secret
```

---

## 8. LiveKit Configuration

```bash
cat > /opt/streaming/livekit.yaml << 'EOF'
# ── LiveKit Server Configuration ──

port: 7880
bind_addresses:
  - ""

# WebRTC media ports
rtc:
  tcp_port: 7881
  port_range_start: 50000
  port_range_end: 60000
  use_external_ip: true    # CRITICAL on cloud VMs — uses public IP for ICE candidates
  use_ice_lite: true

# Redis — DO Managed Redis with TLS
redis:
  address: your-do-redis-host.db.ondigitalocean.com:25061
  username: default
  password: your-redis-password
  tls: true

# Built-in TURN relay — no separate TURN server needed
# Handles viewers behind strict NAT/firewalls (~20-30% of connections)
turn:
  enabled: true
  domain: livestream.zinrai.live
  tls_port: 5349
  udp_port: 3478
  external_tls: true       # TURN uses 443 via nginx for TLS

# Room defaults
room:
  max_participants: 510    # 500 viewers + 6 publishers + headroom
  empty_timeout: 300       # close room 5min after last participant leaves
  departure_timeout: 20    # wait 20s before removing disconnected participant

# Auth — must match .env
keys:
  LIVEKIT_API_KEY: LIVEKIT_API_SECRET    # replace with actual values

# Logging
logging:
  level: warn
  sample: true

# Webhook — notify your API of stream events
webhook:
  api_key: LIVEKIT_API_KEY
  urls:
    - http://localhost:3000/hooks/livekit
EOF
```

> **Important:** Replace `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` with your actual values from `.env`.
> Replace `your-do-redis-host` and `your-redis-password` with your DO Redis details.

---

## 9. Ingress Configuration

LiveKit Ingress converts RTMP from OBS into WebRTC.

```bash
cat > /opt/streaming/ingress.yaml << 'EOF'
# ── LiveKit Ingress Configuration ──

api_key: LIVEKIT_API_KEY         # replace with actual value
api_secret: LIVEKIT_API_SECRET   # replace with actual value

ws_url: wss://livestream.zinrai.live

redis:
  address: your-do-redis-host.db.ondigitalocean.com:25061
  username: default
  password: your-redis-password
  tls: true

rtmp:
  port: 1935

logging:
  level: warn
EOF
```

---

## 10. Nginx Configuration

```bash
cat > /opt/streaming/nginx/conf/nginx.conf << 'EOF'
worker_processes      auto;
worker_rlimit_nofile  65535;

events {
    worker_connections  8192;
    use                 epoll;
    multi_accept        on;
}

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;

    sendfile    on;
    tcp_nopush  on;
    tcp_nodelay on;
    keepalive_timeout    65;
    keepalive_requests 1000;

    gzip off;

    # ── HTTP → HTTPS redirect ──
    server {
        listen 80;
        server_name YOUR_DOMAIN;
        return 301 https://$host$request_uri;
    }

    # ── Main HTTPS server ──
    server {
        listen 443 ssl;
        http2 on;
        server_name YOUR_DOMAIN;

        ssl_certificate     /etc/letsencrypt/live/YOUR_DOMAIN/fullchain.pem;
        ssl_certificate_key /etc/letsencrypt/live/YOUR_DOMAIN/privkey.pem;
        ssl_protocols       TLSv1.2 TLSv1.3;
        ssl_ciphers         HIGH:!aNULL:!MD5;

        # ── LiveKit WebSocket + HTTP ──
        # SDK connects here for WebRTC signalling
        location / {
            proxy_pass         http://localhost:7880;
            proxy_http_version 1.1;
            proxy_set_header   Upgrade    $http_upgrade;
            proxy_set_header   Connection "upgrade";
            proxy_set_header   Host       $host;
            proxy_set_header   X-Real-IP  $remote_addr;
            proxy_read_timeout 86400s;     # long timeout for persistent WS connections
            proxy_send_timeout 86400s;
        }

        # ── Backend API ──
        location /api/ {
            proxy_pass         http://localhost:3000;
            proxy_set_header   Host            $host;
            proxy_set_header   X-Real-IP       $remote_addr;
            proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_http_version 1.1;
        }

        # ── LiveKit webhooks ──
        location /hooks/ {
            proxy_pass         http://localhost:3000;
            proxy_set_header   Host      $host;
            proxy_set_header   X-Real-IP $remote_addr;
            proxy_http_version 1.1;
        }

        # ── Health check ──
        location /health {
            proxy_pass http://localhost:3000/health;
        }
    }
}
EOF

# Replace YOUR_DOMAIN placeholder
sed -i 's/YOUR_DOMAIN/livestream.zinrai.live/g' /opt/streaming/nginx/conf/nginx.conf
```

---

## 11. Node.js API

### `package.json`

```bash
cat > /opt/streaming/api/package.json << 'EOF'
{
  "name": "streaming-api",
  "version": "1.0.0",
  "main": "index.js",
  "scripts": { "start": "node index.js" },
  "dependencies": {
    "express": "^4.18.2",
    "livekit-server-sdk": "^2.0.0",
    "ioredis": "^5.3.2",
    "jsonwebtoken": "^9.0.2",
    "axios": "^1.6.0"
  }
}
EOF
```

### `index.js`

```bash
cat > /opt/streaming/api/index.js << 'EOF'
const express = require('express');
const app     = express();

app.use(express.json());
app.use(express.raw({ type: 'application/webhook+json' }));

app.use('/', require('./routes/stream'));
app.use('/', require('./routes/hooks'));

app.get('/health', (req, res) => res.json({ status: 'ok', ts: Date.now() }));

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`API running on port ${PORT}`));
EOF
```

### `routes/stream.js`

```bash
cat > /opt/streaming/api/routes/stream.js << 'EOF'
const express    = require('express');
const { AccessToken, IngressClient, IngressInput } = require('livekit-server-sdk');
const Redis      = require('ioredis');
const router     = express.Router();

const redis      = new Redis(process.env.REDIS_URL);
const ingressClient = new IngressClient(
  process.env.LIVEKIT_WS_URL,
  process.env.LIVEKIT_API_KEY,
  process.env.LIVEKIT_API_SECRET
);

const TTL_7D = 7 * 24 * 60 * 60;

// ── POST /api/stream/start ──
// Called by your backend when a streamer goes live
// Creates a LiveKit room + RTMP ingress, returns OBS credentials
router.post('/api/stream/start', async (req, res) => {
  const { streamId } = req.body;
  if (!streamId) return res.status(400).json({ error: 'streamId required' });

  try {
    // Create RTMP ingress for this stream
    const ingress = await ingressClient.createIngress(IngressInput.RTMP_INPUT, {
      name:      streamId,
      roomName:  streamId,
      participantIdentity: `publisher-${streamId}`,
      participantName:     `Stream ${streamId}`,
    });

    // Store stream state in Redis
    await redis.hset(`stream:${streamId}:meta`, {
      stream_id:    streamId,
      status:       'live',
      started_at:   new Date().toISOString(),
      ingress_id:   ingress.ingressId,
      rtmp_url:     ingress.url,
      stream_key:   ingress.streamKey,
    });
    await redis.sadd('streams:active', streamId);

    res.json({
      rtmpUrl:   ingress.url,
      streamKey: ingress.streamKey,
      streamId,
    });
  } catch (err) {
    console.error('[stream/start]', err.message);
    res.status(500).json({ error: 'Failed to create stream' });
  }
});

// ── POST /api/stream/stop ──
// Called when streamer ends the stream
router.post('/api/stream/stop', async (req, res) => {
  const { streamId } = req.body;
  if (!streamId) return res.status(400).json({ error: 'streamId required' });

  try {
    const meta = await redis.hgetall(`stream:${streamId}:meta`);

    if (meta?.ingress_id) {
      await ingressClient.deleteIngress(meta.ingress_id).catch(() => {});
    }

    const startedAt   = meta?.started_at ? new Date(meta.started_at).getTime() : Date.now();
    const durationSec = Math.floor((Date.now() - startedAt) / 1000);

    await redis.hset(`stream:${streamId}:meta`, {
      status:       'ended',
      ended_at:     new Date().toISOString(),
      duration_sec: durationSec,
    });
    await redis.srem('streams:active', streamId);
    await redis.expire(`stream:${streamId}:meta`, TTL_7D);

    res.json({ ok: true, durationSec });
  } catch (err) {
    console.error('[stream/stop]', err.message);
    res.status(500).json({ error: 'Failed to stop stream' });
  }
});

// ── POST /api/stream-token ──
// Called by your frontend to get a viewer token
// Returns LiveKit JWT + wsUrl — pass directly to LiveKit JS SDK
router.post('/api/stream-token', async (req, res) => {
  const { streamId, userId } = req.body;
  if (!streamId) return res.status(400).json({ error: 'streamId required' });

  // Check stream is actually live
  const status = await redis.hget(`stream:${streamId}:meta`, 'status');
  if (status !== 'live') {
    return res.status(404).json({ error: 'Stream not found or not live' });
  }

  const token = new AccessToken(
    process.env.LIVEKIT_API_KEY,
    process.env.LIVEKIT_API_SECRET,
    {
      identity: userId || `viewer-${Date.now()}`,
      ttl:      '4h',
    }
  );

  token.addGrant({
    roomJoin:     true,
    room:         streamId,
    canSubscribe: true,
    canPublish:   false,   // viewers cannot publish
    canPublishData: false,
  });

  res.json({
    token:  await token.toJwt(),
    wsUrl:  process.env.LIVEKIT_WS_URL,
    room:   streamId,
  });
});

// ── GET /api/stream/:streamId/status ──
router.get('/api/stream/:streamId/status', async (req, res) => {
  const meta = await redis.hgetall(`stream:${streamId}:meta`);
  if (!meta?.stream_id) return res.status(404).json({ error: 'Not found' });
  res.json(meta);
});

// ── GET /api/streams/active ──
router.get('/api/streams/active', async (req, res) => {
  const streamIds = await redis.smembers('streams:active');
  res.json({ streams: streamIds, count: streamIds.length });
});

module.exports = router;
EOF
```

### `routes/hooks.js`

```bash
cat > /opt/streaming/api/routes/hooks.js << 'EOF'
const express  = require('express');
const { WebhookReceiver } = require('livekit-server-sdk');
const Redis    = require('ioredis');
const router   = express.Router();

const redis    = new Redis(process.env.REDIS_URL);
const receiver = new WebhookReceiver(
  process.env.LIVEKIT_API_KEY,
  process.env.LIVEKIT_API_SECRET
);

// ── POST /hooks/livekit ──
// LiveKit calls this on room/participant events
router.post('/hooks/livekit', async (req, res) => {
  try {
    const event = await receiver.receive(
      req.body.toString(),
      req.headers['webhook-id'],
      req.headers['webhook-timestamp'],
      req.headers['webhook-signature']
    );

    const roomName = event.room?.name;

    switch (event.event) {
      case 'room_started':
        console.log(`[livekit] room started: ${roomName}`);
        break;

      case 'room_finished':
        console.log(`[livekit] room finished: ${roomName}`);
        if (roomName) {
          await redis.hset(`stream:${roomName}:meta`, { status: 'ended' });
          await redis.srem('streams:active', roomName);
        }
        break;

      case 'participant_joined':
        if (roomName) {
          await redis.incr(`stream:${roomName}:total_viewers`).catch(() => {});
        }
        break;

      case 'participant_left':
        break;
    }

    res.status(200).end();
  } catch (err) {
    console.error('[hooks/livekit]', err.message);
    res.status(400).end();
  }
});

module.exports = router;
EOF
```

### `Dockerfile`

```bash
cat > /opt/streaming/api/Dockerfile << 'EOF'
FROM node:20-alpine
WORKDIR /app
COPY package.json .
RUN npm install --production
COPY . .
EXPOSE 3000
CMD ["node", "index.js"]
EOF
```

---

## 12. Docker Compose

```bash
cat > /opt/streaming/docker-compose.yml << 'EOF'
services:

  # ── LiveKit Server (WebRTC SFU + TURN) ──
  livekit:
    image: livekit/livekit-server:latest
    container_name: livekit
    restart: unless-stopped
    network_mode: host         # REQUIRED — WebRTC UDP needs host networking
    volumes:
      - ./livekit.yaml:/etc/livekit.yaml
    command: --config /etc/livekit.yaml
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost:7880/"]
      interval: 30s
      timeout: 5s
      retries: 3

  # ── LiveKit Ingress (RTMP → WebRTC) ──
  # OBS connects here via RTMP on port 1935
  ingress:
    image: livekit/ingress:latest
    container_name: livekit-ingress
    restart: unless-stopped
    network_mode: host         # REQUIRED — needs host networking with livekit
    volumes:
      - ./ingress.yaml:/etc/ingress.yaml
    environment:
      - INGRESS_CONFIG_FILE=/etc/ingress.yaml
    depends_on:
      - livekit

  # ── Backend API ──
  api:
    build: ./api
    container_name: streaming-api
    restart: unless-stopped
    ports:
      - "127.0.0.1:3000:3000"
    environment:
      - NODE_ENV=${NODE_ENV}
      - PORT=${PORT}
      - JWT_SECRET=${JWT_SECRET}
      - LIVEKIT_API_KEY=${LIVEKIT_API_KEY}
      - LIVEKIT_API_SECRET=${LIVEKIT_API_SECRET}
      - LIVEKIT_WS_URL=${LIVEKIT_WS_URL}
      - REDIS_URL=${REDIS_URL}
    depends_on:
      - livekit

  # ── Nginx (SSL termination) ──
  nginx:
    image: nginx:alpine
    container_name: nginx
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx/conf/nginx.conf:/etc/nginx/nginx.conf
      - /etc/letsencrypt:/etc/letsencrypt:ro
    depends_on:
      - livekit
      - api
EOF
```

---

## 13. SSL Certificate

```bash
# Install certbot
apt install -y certbot

# Get certificate (nothing else should be on port 80 yet)
certbot certonly --standalone \
  -d livestream.zinrai.live \
  --non-interactive \
  --agree-tos \
  --email your@email.com

# Verify
ls /etc/letsencrypt/live/livestream.zinrai.live/
# fullchain.pem  privkey.pem  cert.pem  chain.pem

# Auto-renewal
systemctl status certbot.timer
```

---

## 14. Firewall Rules

```bash
ufw default deny incoming
ufw default allow outgoing

ufw allow 22/tcp        # SSH
ufw allow 80/tcp        # HTTP → redirects to HTTPS
ufw allow 443/tcp       # HTTPS + LiveKit WSS + TURN/TLS
ufw allow 1935/tcp      # RTMP ingest (OBS)
ufw allow 7881/tcp      # WebRTC TCP fallback
ufw allow 5349/tcp      # TURN TLS
ufw allow 3478/udp      # TURN UDP
ufw allow 50000:60000/udp  # WebRTC UDP media

ufw enable
ufw status verbose
```

---

## 15. Start Everything

```bash
cd /opt/streaming

# Build API image
docker compose build api

# Start all services
docker compose up -d

# Check status
docker compose ps

# Expected:
# livekit          Up (healthy)
# livekit-ingress  Up
# streaming-api    Up
# nginx            Up

# Watch logs
docker compose logs -f
```

---

## 16. OBS Configuration

Before OBS can stream, create an ingress via the API:

```bash
# Create a stream ingress — returns RTMP URL and stream key
curl -X POST https://livestream.zinrai.live/api/stream/start \
  -H "Content-Type: application/json" \
  -d '{"streamId":"test-stream-001"}'

# Response:
# {
#   "rtmpUrl": "rtmp://livestream.zinrai.live/live",
#   "streamKey": "SK_xxxxxxxxxxxxx",
#   "streamId": "test-stream-001"
# }
```

In OBS — **Settings → Stream:**

```
Service    : Custom...
Server     : rtmp://livestream.zinrai.live/live
Stream Key : SK_xxxxxxxxxxxxx   ← from the API response above
```

**Settings → Output → Encoding:**

```
Encoder           : x264
Profile           : baseline
Tune              : zerolatency
Rate Control      : CBR
Bitrate           : 2500 Kbps
Keyframe Interval : 2 seconds
Preset            : veryfast
```

**Settings → Video:**

```
Base Resolution   : 1280 × 720
Output Resolution : 1280 × 720
FPS               : 30
```

---

## 17. Frontend Integration

Install the LiveKit JS SDK:

```bash
npm install livekit-client
```

```html
<video id="video" autoplay playsinline muted></video>
<script type="module">
import { Room, RoomEvent, Track } from 'livekit-client';

async function watchStream(streamId, userId) {
  // 1. Get token from your API
  const res = await fetch('/api/stream-token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ streamId, userId }),
  });
  const { token, wsUrl } = await res.json();

  // 2. Connect to LiveKit room
  const room = new Room({
    adaptiveStream: true,       // auto quality based on viewer bandwidth
    dynacast: true,             // only receive video you can actually see
    reconnectPolicy: {
      maxRetries: 10,           // auto-reconnect up to 10 times
    },
  });

  // 3. Handle video/audio tracks
  room.on(RoomEvent.TrackSubscribed, (track, publication, participant) => {
    if (track.kind === Track.Kind.Video) {
      track.attach(document.getElementById('video'));
    } else if (track.kind === Track.Kind.Audio) {
      track.attach();           // auto-plays audio
    }
  });

  room.on(RoomEvent.Disconnected, () => {
    console.log('Disconnected — will auto-reconnect');
  });

  room.on(RoomEvent.Reconnecting, () => {
    console.log('Reconnecting...');
  });

  room.on(RoomEvent.Reconnected, () => {
    console.log('Reconnected');
  });

  // 4. Join — this is all you need
  await room.connect(wsUrl, token);
}

// Usage
watchStream('test-stream-001', 'user-123');
</script>
```

---

## 18. Verify the Setup

**Step 1 — LiveKit server is healthy:**
```bash
curl http://localhost:7880/
# Should return: OK
```

**Step 2 — Create a stream and get OBS credentials:**
```bash
curl -X POST https://livestream.zinrai.live/api/stream/start \
  -H "Content-Type: application/json" \
  -d '{"streamId":"test-stream-001"}'
# Should return rtmpUrl + streamKey
```

**Step 3 — Start OBS with the credentials from Step 2, then check active rooms:**
```bash
curl -s https://livestream.zinrai.live/api/streams/active | python3 -m json.tool
# Should show: { "streams": ["test-stream-001"], "count": 1 }
```

**Step 4 — Get a viewer token and test in browser:**
```bash
curl -X POST https://livestream.zinrai.live/api/stream-token \
  -H "Content-Type: application/json" \
  -d '{"streamId":"test-stream-001","userId":"test-viewer"}'
# Returns: { "token": "eyJ...", "wsUrl": "wss://...", "room": "test-stream-001" }
```

Open the frontend, call `watchStream('test-stream-001')` — you should see sub-500ms video.

**Step 5 — Check viewer count in Redis:**
```bash
redis-cli -u "$REDIS_URL" --tls HGETALL stream:test-stream-001:meta
```

---

## 19. Phase 2 — Second Node + Load Balancer

Do this once Phase 1 is stable.

**Provision second c-4 droplet** (same region), repeat Sections 3–15 with identical config.

**Create DO Load Balancer:**

```
DO Console → Networking → Load Balancers → Create
  Region     : same as droplets
  Forwarding : HTTPS 443 → HTTP 7880
               TCP   1935 → TCP 1935
  Health check: HTTP · path / · port 7880
  Droplets   : add both nodes
```

> **Important:** Update your domain DNS to point to the Load Balancer IP, not the individual droplet IP.

Both nodes share the same Redis → LiveKit automatically coordinates rooms and participants across nodes.

**Test failover:**
```bash
# While streaming, stop one node
docker compose -f /opt/streaming/docker-compose.yml stop livekit

# Viewer should reconnect automatically within 10–20 seconds
# LB detects unhealthy node and routes to the healthy one
```

---

## 20. Failure Recovery

### Node goes down (automatic)
Load balancer detects failure in ~30s, routes all traffic to healthy node.
Viewers reconnect automatically via LiveKit SDK (built-in reconnect logic).
**No manual action needed.**

### Both nodes down
```bash
ssh root@YOUR_SERVER_IP
cd /opt/streaming
docker compose up -d
# Back online in ~60 seconds
```

### OBS stream drops
OBS has built-in auto-reconnect. If not enabled:
```
OBS → Settings → Output → Reconnect Delay: 5s · Retry attempts: 10
```

### Redis down
DO Managed Redis has automatic failover (~30s).
Existing viewer connections stay alive during Redis outage.
New connections may fail briefly — LiveKit SDK retries.

### SSL certificate expires
```bash
certbot renew
docker compose restart nginx
```
Certbot auto-renewal runs via systemd timer — verify with:
```bash
systemctl status certbot.timer
```

---

## 21. Common Issues

**LiveKit container won't start**
```bash
docker compose logs livekit | tail -30
# Most common: wrong Redis URL or API key mismatch in livekit.yaml
```

**OBS connects but stream doesn't appear in room**
```bash
docker compose logs ingress | tail -20
# Check ingress stream key matches what was returned by /api/stream/start
```

**Viewers can't connect (ICE failed)**
```bash
# 1. Verify ports are open
ufw status | grep -E "50000|7881|443"

# 2. Verify use_external_ip is true in livekit.yaml
grep use_external_ip /opt/streaming/livekit.yaml

# 3. Check TURN is working
docker compose logs livekit | grep -i turn
```

**API returns 404 for stream-token**
```bash
# Stream is not marked as live in Redis
redis-cli -u "$REDIS_URL" --tls HGET stream:test-stream-001:meta status
# If missing, call /api/stream/start first
```

**High latency (>1 second)**
```bash
# Check if viewers are going through TURN relay (adds ~50-100ms)
docker compose logs livekit | grep -i "relay\|turn"
# Normal WebRTC (STUN, no relay) should be 100-300ms
```

**Node memory pressure**
```bash
docker stats
# If livekit container > 6GB RAM, time to add Phase 2 second node
```

---

## Cost Summary

| Component | Phase 1 | Phase 2 |
|---|---|---|
| LiveKit nodes | $84/mo (1×) | $168/mo (2×) |
| DO Load Balancer | — | $12/mo |
| DO Managed Redis | ~$15/mo | ~$15/mo |
| **Total** | **~$99/mo** | **~$195/mo** |

Budget remaining at Phase 2: **$205–305/mo headroom** for a 3rd node or CDN if needed.

---

*LiveKit · WebRTC · Node.js · Docker Compose · DigitalOcean*
