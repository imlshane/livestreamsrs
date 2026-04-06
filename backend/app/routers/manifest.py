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
    """Clear cached manifests for a stream_key (all sessions)."""
    to_remove = [k for k in _cache if k.startswith(f"{stream_key}:")]
    for k in to_remove:
        _cache.pop(k, None)


async def _build_manifest(stream_key: str, session_id: str) -> str:
    cache_key = f"{stream_key}:{session_id}"
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached and (now - cached[1]) < CACHE_TTL:
        return cached[0]

    m3u8_path = f"{settings.hls_path}/live/{stream_key}/index.m3u8"
    try:
        with open(m3u8_path, "r") as f:
            srs_content = f.read()
    except FileNotFoundError:
        # SRS hasn't written the first segment yet (takes 2-4s after on_publish).
        # Return a minimal valid live playlist so HLS.js keeps polling instead of failing.
        return "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:0\n"

    # Segment URL includes session_id as a path component — acts as cache-buster.
    # Each stream session produces unique segment URLs so browsers never serve
    # segments from a previous session. nginx strips the session_id before
    # looking up the file: /segments/live/{stream_key}/{session_id}/seg-N.ts
    #                                                → /hls-data/live/{stream_key}/seg-N.ts
    #
    # Also strip #EXT-X-DISCONTINUITY — SRS adds it at stream start due to sequence
    # gap from previous session. With session-based URLs the player starts fresh
    # each time, so this tag is incorrect and causes HLS.js to flicker.
    seg_base = f"{settings.segments_base_url}/live/{stream_key}/{session_id}"
    lines = []
    for line in srs_content.splitlines():
        stripped = line.strip()
        if stripped == "#EXT-X-DISCONTINUITY":
            continue
        if stripped.endswith(".ts") and not stripped.startswith("#"):
            # URL format: /segments/live/{stream_key}/{session_id}-seg-N.ts
            # session_id is embedded in the filename (no subfolder) — nginx strips
            # everything up to and including the UUID prefix before the disk lookup.
            lines.append(f"{seg_base}-{os.path.basename(stripped)}")
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
    _cache[cache_key] = (content, now)
    return content


_MANIFEST_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
}


@router.get("/{session_id}.m3u8")
async def get_session_manifest(session_id: str):
    """
    Session-based manifest URL — unique per stream session.
    session_id is the live_stream UUID assigned at publish time.
    Looks up stream_key from Redis, then reads SRS m3u8 from disk.
    """
    redis = await get_redis()
    stream_key = await redis.get(key(f"session:{session_id}:stream_key"))
    if not stream_key:
        raise HTTPException(status_code=404, detail="Stream session not found or has ended")
    manifest = await _build_manifest(stream_key, session_id)
    return Response(content=manifest, media_type="application/vnd.apple.mpegurl", headers=_MANIFEST_HEADERS)


@router.options("/{session_id}.m3u8")
async def manifest_cors_preflight(session_id: str):
    return Response(
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        }
    )
