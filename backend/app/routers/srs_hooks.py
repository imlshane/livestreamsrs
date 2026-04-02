"""
SRS HTTP hook endpoints.
SRS calls these on stream lifecycle events.
All hooks must return HTTP 200 with {"code": 0} to allow the action,
or non-zero code / non-200 status to reject.
"""
import logging
from datetime import datetime, timedelta
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Educator, LiveStream
from app.redis_client import get_redis, key
from app.schemas import SRSErrorPayload, SRSHlsPayload, SRSPublishPayload, SRSUnpublishPayload
from app.routers.manifest import invalidate_manifest_cache
from app.services.dvr_processor import process_dvr_async

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/srs", tags=["srs-hooks"])


def _parse_secret(param: str) -> str | None:
    """Extract ?secret= from SRS param string."""
    qs = parse_qs(param.lstrip("?"))
    secrets = qs.get("secret", [])
    return secrets[0] if secrets else None


@router.post("/on_publish")
async def on_publish(
    payload: SRSPublishPayload,
    db: AsyncSession = Depends(get_db),
):
    """
    Called by SRS when OBS starts publishing.
    Validates stream key + publish secret, creates LiveStream record.
    """
    stream_key = payload.stream
    secret = _parse_secret(payload.param)

    # Validate global publish secret
    if secret != settings.srs_publish_secret:
        logger.warning("on_publish rejected: bad secret for stream_key=%s ip=%s", stream_key, payload.ip)
        return {"code": 1, "error": "invalid secret"}

    # Look up educator by stream key
    result = await db.execute(
        select(Educator).where(Educator.stream_key == stream_key, Educator.is_active == True)
    )
    educator = result.scalar_one_or_none()

    if not educator:
        logger.warning("on_publish rejected: unknown stream_key=%s", stream_key)
        return {"code": 2, "error": "unknown stream key"}

    # Check concurrent stream limit
    redis = await get_redis()
    active_count = await redis.scard(key("active_streams"))
    if active_count >= settings.max_concurrent_streams:
        logger.warning("on_publish rejected: max concurrent streams reached (%d)", active_count)
        return {"code": 3, "error": "max concurrent streams reached"}

    # Create LiveStream session
    now = datetime.utcnow()
    timeout_at = now + timedelta(seconds=settings.stream_max_duration_seconds)

    hls_url = (
        f"{settings.do_spaces_cdn_url}/live/{stream_key}/index.m3u8"
    )

    live_stream = LiveStream(
        stream_key=stream_key,
        educator_id=educator.id,
        title=f"{educator.name} — Live",
        status="live",
        started_at=now,
        hls_manifest_url=hls_url,
        srs_client_id=payload.client_id,
        publisher_ip=payload.ip,
        timeout_at=timeout_at,
    )
    db.add(live_stream)
    await db.flush()

    # Clear any stale state from a previous session with this stream key
    await redis.delete(
        key(f"stream:{stream_key}:segments"),
        key(f"stream:{stream_key}:ended"),
        key(f"stream:{stream_key}:id"),
        key(f"stream:{stream_key}:viewers"),
        key(f"stream:{stream_key}:peak"),
        key(f"stream:{stream_key}:timeout"),
    )

    # Track in Redis
    await redis.sadd(key("active_streams"), live_stream.id)
    await redis.set(key(f"stream:{stream_key}:id"), live_stream.id, ex=settings.stream_max_duration_seconds + 300)
    await redis.set(key(f"stream:{stream_key}:viewers"), "0", ex=settings.stream_max_duration_seconds + 300)
    await redis.set(key(f"stream:{stream_key}:timeout"), str(timeout_at.timestamp()), ex=settings.stream_max_duration_seconds + 300)

    # Bust any cached manifest from a previous session
    invalidate_manifest_cache(stream_key)

    logger.info("Stream started: key=%s educator=%s id=%s", stream_key, educator.name, live_stream.id)
    return {"code": 0}


@router.post("/on_unpublish")
async def on_unpublish(
    payload: SRSUnpublishPayload,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Called by SRS when OBS stops publishing."""
    stream_key = payload.stream

    # Find the active LiveStream
    result = await db.execute(
        select(LiveStream).where(
            LiveStream.stream_key == stream_key,
            LiveStream.status == "live",
        ).order_by(LiveStream.created_at.desc()).limit(1)
    )
    live_stream = result.scalar_one_or_none()

    if not live_stream:
        logger.warning("on_unpublish: no active stream found for key=%s", stream_key)
        return {"code": 0}

    now = datetime.utcnow()
    duration = (now - live_stream.started_at).total_seconds() if live_stream.started_at else 0

    live_stream.status = "ended"
    live_stream.ended_at = now
    live_stream.duration_seconds = duration

    # Get peak viewer count from Redis before cleanup
    redis = await get_redis()
    peak_str = await redis.get(key(f"stream:{stream_key}:peak"))
    if peak_str:
        live_stream.viewer_peak = int(peak_str)

    # Mark stream ended in Redis (manifest proxy adds EXT-X-ENDLIST)
    await redis.set(key(f"stream:{stream_key}:ended"), "1", ex=3600)

    # Clean up Redis
    await redis.srem(key("active_streams"), live_stream.id)
    await redis.delete(
        key(f"stream:{stream_key}:id"),
        key(f"stream:{stream_key}:viewers"),
        key(f"stream:{stream_key}:peak"),
        key(f"stream:{stream_key}:timeout"),
    )

    stream_id = live_stream.id
    dvr_path = f"{settings.dvr_path}/live/{stream_key}"

    logger.info("Stream ended: key=%s duration=%.0fs", stream_key, duration)

    # Process DVR in background (FLV → MP4 → DO Spaces upload)
    background_tasks.add_task(process_dvr_async, stream_id, stream_key, dvr_path)

    return {"code": 0}


@router.post("/on_hls")
async def on_hls(payload: SRSHlsPayload):
    """
    Called by SRS immediately after each segment is written to disk.
    Segments are served directly by nginx from the shared hls_data volume —
    no upload needed. Just register in Redis so the manifest proxy picks it up.
    SRS fires hooks sequentially so order is guaranteed.
    """
    redis = await get_redis()
    stream_seg_key = key(f"stream:{payload.stream}:segments")
    await redis.rpush(stream_seg_key, payload.seq_no)
    await redis.expire(stream_seg_key, settings.stream_max_duration_seconds + 600)
    logger.debug("seg-%d registered for %s", payload.seq_no, payload.stream)
    return {"code": 0}


@router.post("/on_error")
async def on_error(payload: SRSErrorPayload):
    logger.error("SRS error: stream=%s msg=%s", payload.stream, payload.msg)
    return {"code": 0}
