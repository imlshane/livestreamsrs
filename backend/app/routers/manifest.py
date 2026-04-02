"""
Live manifest proxy — serves index.m3u8 bypassing CDN cache.
Caches each manifest for 200ms to avoid hammering DO Spaces origin when multiple viewers poll.
Segments are rewritten to full CDN URLs so browsers fetch them from the fast CDN edge.
"""
import logging
import time

import httpx
from fastapi import APIRouter, HTTPException, Response

from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/live", tags=["manifest"])

SPACES_ORIGIN = settings.do_spaces_endpoint.rstrip("/")
BUCKET = settings.do_spaces_bucket

# In-memory cache: stream_key -> (content, fetched_at_monotonic)
_cache: dict[str, tuple[str, float]] = {}
CACHE_TTL = 0.2  # 200ms — fresh enough for 1s segments


async def _fetch_manifest(stream_key: str) -> str:
    now = time.monotonic()
    cached = _cache.get(stream_key)
    if cached and (now - cached[1]) < CACHE_TTL:
        return cached[0]

    url = f"{SPACES_ORIGIN}/{BUCKET}/live/{stream_key}/index.m3u8"
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(url)
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail="Stream not found or not yet live")
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to fetch manifest from storage")
        content = resp.text
        _cache[stream_key] = (content, now)
        return content


def _rewrite_segment_urls(manifest: str, stream_key: str) -> str:
    """Rewrite relative seg-N.ts filenames to absolute CDN URLs."""
    cdn_base = f"{settings.do_spaces_cdn_url}/live/{stream_key}"
    lines = []
    for line in manifest.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped.endswith(".ts"):
            lines.append(f"{cdn_base}/{stripped}")
        else:
            lines.append(line)
    return "\n".join(lines)


@router.get("/{stream_key}/index.m3u8")
async def get_live_manifest(stream_key: str):
    manifest = await _fetch_manifest(stream_key)
    manifest = _rewrite_segment_urls(manifest, stream_key)

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
