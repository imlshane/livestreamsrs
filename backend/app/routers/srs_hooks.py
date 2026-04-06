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

    live_stream = LiveStream(
        stream_key=stream_key,
        educator_id=educator.id,
        title=f"{educator.name} — Live",
        status="live",
        started_at=now,
        hls_manifest_url="",   # set after flush once we have the UUID
        srs_client_id=payload.client_id,
        publisher_ip=payload.ip,
        timeout_at=timeout_at,
    )
    db.add(live_stream)
    await db.flush()   # populates live_stream.id

    # Session-based manifest URL — unique per stream session, no cache clash
    hls_url = f"https://{settings.domain}/live/{live_stream.id}.m3u8"
    live_stream.hls_manifest_url = hls_url

    # Clear any stale state from a previous session with this stream key
    await redis.delete(
        key(f"stream:{stream_key}:ended"),
        key(f"stream:{stream_key}:id"),
        key(f"stream:{stream_key}:viewers"),
        key(f"stream:{stream_key}:peak"),
        key(f"stream:{stream_key}:timeout"),
    )

    ttl = settings.stream_max_duration_seconds + 300
    # Forward mapping: stream_key → session id (for status endpoint)
    await redis.set(key(f"stream:{stream_key}:id"), str(live_stream.id), ex=ttl)
    # Reverse mapping: session id → stream_key (for manifest endpoint)
    await redis.set(key(f"session:{live_stream.id}:stream_key"), stream_key, ex=ttl)

    await redis.sadd(key("active_streams"), str(live_stream.id))
    await redis.set(key(f"stream:{stream_key}:viewers"), "0", ex=ttl)
    await redis.set(key(f"stream:{stream_key}:timeout"), str(timeout_at.timestamp()), ex=ttl)

    # Bust any cached manifest from a previous session
    invalidate_manifest_cache(stream_key)

    # SRS handles segment cleanup via hls_cleanup + hls_dispose.
    # Session-based manifest URLs (/live/{session_id}.m3u8) and session-prefixed
    # segment URLs prevent browser cache clashes between sessions.
    # We no longer rmtree the HLS directory — that caused a 2-4s video gap on
    # OBS reconnects because SRS had to recreate the directory and first segment.

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

    # Find the active LiveStream matching this SRS client_id.
    # IMPORTANT: We match on srs_client_id (not just stream_key + status=live)
    # to avoid a race condition where OBS reconnects quickly:
    #   1. on_publish (new) fires first → creates session B
    #   2. on_unpublish (old) fires later → would wrongly close session B
    # By matching on client_id, the late on_unpublish only closes session A.
    result = await db.execute(
        select(LiveStream).where(
            LiveStream.stream_key == stream_key,
            LiveStream.status == "live",
            LiveStream.srs_client_id == payload.client_id,
        ).limit(1)
    )
    live_stream = result.scalar_one_or_none()

    if not live_stream:
        # No match on client_id — either already ended, or a newer session
        # replaced this one. Check if a newer live session exists (reconnect case).
        result2 = await db.execute(
            select(LiveStream).where(
                LiveStream.stream_key == stream_key,
                LiveStream.status == "live",
            ).order_by(LiveStream.created_at.desc()).limit(1)
        )
        newer = result2.scalar_one_or_none()
        if newer:
            logger.info(
                "on_unpublish: ignoring stale unpublish for key=%s client=%s "
                "(newer session %s is live)",
                stream_key, payload.client_id, newer.id,
            )
        else:
            logger.warning("on_unpublish: no active stream found for key=%s client=%s", stream_key, payload.client_id)
        return {"code": 0}

    now = datetime.utcnow()
    started = live_stream.started_at.replace(tzinfo=None) if live_stream.started_at else None
    duration = (now - started).total_seconds() if started else 0

    live_stream.status = "ended"
    live_stream.ended_at = now
    live_stream.duration_seconds = duration

    # Get peak viewer count from Redis before cleanup
    redis = await get_redis()
    peak_str = await redis.get(key(f"stream:{stream_key}:peak"))
    if peak_str:
        live_stream.viewer_peak = int(peak_str)

    # Only set ended flag if no newer session has taken over.
    # Check the current session_id in Redis — if it still points to THIS session,
    # mark ended. If it points to a newer session, skip (don't kill the new stream).
    current_session = await redis.get(key(f"stream:{stream_key}:id"))
    if current_session == str(live_stream.id):
        await redis.set(key(f"stream:{stream_key}:ended"), "1", ex=3600)
        await redis.delete(
            key(f"stream:{stream_key}:id"),
            key(f"session:{live_stream.id}:stream_key"),
            key(f"stream:{stream_key}:viewers"),
            key(f"stream:{stream_key}:peak"),
            key(f"stream:{stream_key}:timeout"),
        )
        invalidate_manifest_cache(stream_key)
    else:
        # A newer session owns this stream_key — only clean up our own session mapping
        await redis.delete(key(f"session:{live_stream.id}:stream_key"))
        logger.info("on_unpublish: newer session active, skipping stream-level Redis cleanup for key=%s", stream_key)

    await redis.srem(key("active_streams"), str(live_stream.id))

    stream_id = live_stream.id
    dvr_path = f"{settings.dvr_path}/live/{stream_key}"

    logger.info("Stream ended: key=%s session=%s duration=%.0fs", stream_key, stream_id, duration)

    # Process DVR in background (FLV → MP4 → DO Spaces upload)
    background_tasks.add_task(process_dvr_async, stream_id, stream_key, dvr_path)

    return {"code": 0}


@router.post("/on_hls")
async def on_hls(payload: SRSHlsPayload):
    # Manifest now reads SRS m3u8 directly — no Redis tracking needed here.
    return {"code": 0}


@router.post("/on_error")
async def on_error(payload: SRSErrorPayload):
    logger.error("SRS error: stream=%s msg=%s", payload.stream, payload.msg)
    return {"code": 0}
