"""
Public + internal stream management API.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Educator, LiveStream
from app.redis_client import get_redis, key
from app.schemas import ActiveStreamsOut, LiveStreamOut, ViewerCountOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/streams", tags=["streams"])


async def _enrich(stream: LiveStream, redis) -> LiveStreamOut:
    viewer_count = 0
    raw = await redis.get(key(f"stream:{stream.stream_key}:viewers"))
    if raw:
        viewer_count = int(raw)

    educator_name = stream.educator.name if stream.educator else None

    return LiveStreamOut(
        id=stream.id,
        stream_key=stream.stream_key,
        educator_id=stream.educator_id,
        educator_name=educator_name,
        title=stream.title,
        status=stream.status,
        started_at=stream.started_at,
        ended_at=stream.ended_at,
        duration_seconds=stream.duration_seconds,
        viewer_peak=stream.viewer_peak,
        hls_manifest_url=stream.hls_manifest_url,
        do_mp4_path=stream.do_mp4_path,
        viewer_count=viewer_count,
        created_at=stream.created_at,
    )


@router.get("/active", response_model=ActiveStreamsOut)
async def list_active_streams(
    db: AsyncSession = Depends(get_db),
):
    """List all currently live streams."""
    result = await db.execute(
        select(LiveStream)
        .where(LiveStream.status == "live")
        .order_by(LiveStream.started_at.desc())
    )
    streams = result.scalars().all()
    redis = await get_redis()
    enriched = [await _enrich(s, redis) for s in streams]
    return ActiveStreamsOut(streams=enriched, total=len(enriched))


@router.get("/{stream_id}", response_model=LiveStreamOut)
async def get_stream(
    stream_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(LiveStream).where(LiveStream.id == stream_id)
    )
    stream = result.scalar_one_or_none()
    if not stream:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stream not found")

    redis = await get_redis()
    return await _enrich(stream, redis)


@router.get("/{stream_id}/viewers", response_model=ViewerCountOut)
async def get_viewer_count(
    stream_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(LiveStream).where(LiveStream.id == stream_id)
    )
    stream = result.scalar_one_or_none()
    if not stream:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stream not found")

    redis = await get_redis()
    viewer_count = int(await redis.get(key(f"stream:{stream.stream_key}:viewers")) or 0)
    peak = int(await redis.get(key(f"stream:{stream.stream_key}:peak")) or stream.viewer_peak)

    return ViewerCountOut(
        stream_id=stream.id,
        stream_key=stream.stream_key,
        viewer_count=viewer_count,
        viewer_peak=peak,
    )


@router.post("/{stream_id}/viewers/join")
async def viewer_join(stream_id: str, db: AsyncSession = Depends(get_db)):
    """Increment viewer count when a player starts watching."""
    result = await db.execute(
        select(LiveStream).where(LiveStream.id == stream_id, LiveStream.status == "live")
    )
    stream = result.scalar_one_or_none()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found or not live")

    redis = await get_redis()
    count = await redis.incr(key(f"stream:{stream.stream_key}:viewers"))
    peak = int(await redis.get(key(f"stream:{stream.stream_key}:peak")) or 0)
    if count > peak:
        await redis.set(key(f"stream:{stream.stream_key}:peak"), str(count), ex=settings_ttl())
    return {"viewer_count": count}


@router.post("/{stream_id}/viewers/leave")
async def viewer_leave(stream_id: str, db: AsyncSession = Depends(get_db)):
    """Decrement viewer count when a player stops watching."""
    result = await db.execute(
        select(LiveStream).where(LiveStream.id == stream_id)
    )
    stream = result.scalar_one_or_none()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    redis = await get_redis()
    count = await redis.decr(key(f"stream:{stream.stream_key}:viewers"))
    if count < 0:
        await redis.set(key(f"stream:{stream.stream_key}:viewers"), "0")
        count = 0
    return {"viewer_count": count}


@router.get("/status/{stream_key}")
async def stream_status(stream_key: str):
    """
    Check if a streamer is currently live.
    Returns is_live flag and the unique session m3u8 URL to pass to the player.
    """
    from app.config import settings as _settings
    redis = await get_redis()
    session_id = await redis.get(key(f"stream:{stream_key}:id"))
    if not session_id:
        return {"is_live": False, "stream_key": stream_key}

    m3u8_url = f"https://{_settings.domain}/live/{session_id}.m3u8"
    return {
        "is_live": True,
        "stream_key": stream_key,
        "session_id": session_id,
        "m3u8_url": m3u8_url,
    }


@router.get("/by-key/{stream_key}", response_model=LiveStreamOut)
async def get_stream_by_key(
    stream_key: str,
    db: AsyncSession = Depends(get_db),
):
    """Get active stream by stream key (used by player to find stream)."""
    result = await db.execute(
        select(LiveStream).where(
            LiveStream.stream_key == stream_key,
            LiveStream.status == "live",
        ).order_by(LiveStream.created_at.desc())
    )
    stream = result.scalar_one_or_none()
    if not stream:
        raise HTTPException(status_code=404, detail="No active stream for this key")

    redis = await get_redis()
    return await _enrich(stream, redis)


def settings_ttl() -> int:
    from app.config import settings
    return settings.stream_max_duration_seconds + 300
