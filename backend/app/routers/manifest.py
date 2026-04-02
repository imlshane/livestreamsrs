"""
Live manifest proxy — builds HLS EVENT playlist from Redis segment registry.

Key design decisions:
- Segments are added to Redis ONLY after confirmed upload to DO Spaces.
  This means the manifest NEVER references a segment that isn't available yet.
  Eliminates the 404 → seek-back flickering in HLS.js.

- EXT-X-PLAYLIST-TYPE:EVENT keeps the full stream from start in the manifest.
  Viewers can scrub back to the beginning. Live edge is always at the end.
  No latency impact — HLS.js naturally plays at the live edge.

- Manifest built in memory from Redis — no DO Spaces fetch, no CDN cache issue.
  50ms in-memory cache handles burst requests from multiple viewers.
"""
import logging
import time

from fastapi import APIRouter, HTTPException, Response

from app.config import settings
from app.redis_client import get_redis, key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/live", tags=["manifest"])

# Tiny in-memory cache to absorb burst requests (multiple viewers at same second)
_cache: dict[str, tuple[str, float]] = {}
CACHE_TTL = 0.5  # 500ms


def invalidate_manifest_cache(stream_key: str) -> None:
    """Call this when a stream starts/ends to immediately clear stale cached manifest."""
    _cache.pop(stream_key, None)


async def _build_manifest(stream_key: str) -> str:
    now = time.monotonic()
    cached = _cache.get(stream_key)
    if cached and (now - cached[1]) < CACHE_TTL:
        return cached[0]

    redis = await get_redis()

    # Get all confirmed-uploaded segment sequence numbers
    raw = await redis.lrange(key(f"stream:{stream_key}:segments"), 0, -1)
    if not raw:
        raise HTTPException(status_code=404, detail="Stream not found or no segments yet")

    seq_numbers = [int(s) for s in raw]
    first_seq = seq_numbers[0]
    cdn_base = f"{settings.segments_base_url}/live/{stream_key}"

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:2",
        "#EXT-X-PLAYLIST-TYPE:EVENT",       # full history, live edge at end
        f"#EXT-X-MEDIA-SEQUENCE:{first_seq}",
    ]

    for seq in seq_numbers:
        lines.append("#EXTINF:2.000,")
        lines.append(f"{cdn_base}/seg-{seq}.ts")

    # If stream has ended, close the playlist so player knows it's VOD now
    ended = await redis.get(key(f"stream:{stream_key}:ended"))
    if ended:
        lines.append("#EXT-X-ENDLIST")

    content = "\n".join(lines) + "\n"
    _cache[stream_key] = (content, now)
    return content


@router.get("/{stream_key}/index.m3u8")
async def get_live_manifest(stream_key: str):
    manifest = await _build_manifest(stream_key)
    return Response(
        content=manifest,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
        },
    )


@router.options("/{stream_key}/index.m3u8")
async def manifest_cors_preflight(stream_key: str):
    return Response(
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        }
    )
