"""
Live manifest proxy — serves index.m3u8 fresh from DO Spaces origin on every request.

Why: DO Spaces CDN caches manifests despite no-cache headers, breaking live HLS.
This endpoint bypasses CDN for the manifest only. Segments still come from CDN.

Player uses:  https://livestream.zinrai.live/api/live/{stream_key}/index.m3u8
Segments use: https://livestreamcdn.zinrai.live/live/{stream_key}/seg-N.ts  (CDN)
"""
import logging

import httpx
from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import PlainTextResponse

from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/live", tags=["manifest"])

# Fetch directly from DO Spaces origin, not CDN
SPACES_ORIGIN = settings.do_spaces_endpoint.rstrip("/")
BUCKET = settings.do_spaces_bucket


async def _fetch_manifest(stream_key: str) -> str:
    url = f"{SPACES_ORIGIN}/{BUCKET}/live/{stream_key}/index.m3u8"
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(url)
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail="Stream not found or not yet live")
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to fetch manifest from storage")
        return resp.text


def _rewrite_segment_urls(manifest: str, stream_key: str) -> str:
    """
    Rewrite relative segment filenames to absolute CDN URLs.
    SRS writes:  seg-N.ts
    We want:     https://livestreamcdn.zinrai.live/live/{stream_key}/seg-N.ts
    """
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
    """
    Returns the live HLS manifest with hard no-cache headers.
    Segments are rewritten to point to CDN.
    """
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
