"""
Live manifest proxy — reads SRS-generated m3u8 from disk and rewrites
segment URLs to point at nginx (direct disk serve, zero upload delay).

Why read SRS m3u8 instead of building from Redis:
- SRS m3u8 is a sliding window of segments that actually exist on disk.
  Our old EVENT playlist referenced ALL segments ever written, but SRS
  deletes old ones (hls_cleanup on). Result: player gets 404 on ~95% of
  segment requests → constant buffering.
- SRS writes accurate EXTINF durations (not our hardcoded 2.000).
- Standard live sliding window is what HLS.js is designed for.
"""
import logging
import os
import time

from fastapi import APIRouter, HTTPException, Response

from app.config import settings
from app.redis_client import get_redis, key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/live", tags=["manifest"])

_cache: dict[str, tuple[str, float]] = {}
CACHE_TTL = 0.5  # 500ms burst cache


def invalidate_manifest_cache(stream_key: str) -> None:
    """Clear cached manifest when a stream starts/ends."""
    _cache.pop(stream_key, None)


async def _build_manifest(stream_key: str) -> str:
    now = time.monotonic()
    cached = _cache.get(stream_key)
    if cached and (now - cached[1]) < CACHE_TTL:
        return cached[0]

    m3u8_path = f"{settings.hls_path}/live/{stream_key}/index.m3u8"
    try:
        with open(m3u8_path, "r") as f:
            srs_content = f.read()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Stream not found or not started yet")

    # Rewrite relative segment filenames → full nginx URLs
    seg_base = f"{settings.segments_base_url}/live/{stream_key}"
    lines = []
    for line in srs_content.splitlines():
        stripped = line.strip()
        if stripped.endswith(".ts") and not stripped.startswith("#"):
            lines.append(f"{seg_base}/{os.path.basename(stripped)}")
        else:
            lines.append(line)

    content = "\n".join(lines)

    # Add EXT-X-ENDLIST if stream has ended and SRS hasn't already added it
    if "#EXT-X-ENDLIST" not in content:
        redis = await get_redis()
        ended = await redis.get(key(f"stream:{stream_key}:ended"))
        if ended:
            content += "\n#EXT-X-ENDLIST"

    content += "\n"
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
