"""
DVR post-processing: FLV → MP4 conversion + DO Spaces upload.
Runs as a background task after stream ends.
"""
import asyncio
import glob
import logging
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import LiveStream
from app.services.do_storage import upload_file

logger = logging.getLogger(__name__)


async def process_dvr_async(stream_id: str, stream_key: str, dvr_dir: str):
    """Find DVR FLV file, convert to MP4, upload to DO Spaces."""
    try:
        # Find the FLV file(s) for this stream
        pattern = f"{dvr_dir}/{stream_key}-*.flv"
        # SRS DVR path is /dvr/live/<stream_key>-<timestamp>.flv
        alt_pattern = f"{settings.dvr_path}/live/{stream_key}-*.flv"

        flv_files = glob.glob(pattern) or glob.glob(alt_pattern)
        if not flv_files:
            logger.warning("DVR: no FLV file found for stream_key=%s", stream_key)
            return

        # Use the most recent FLV
        flv_path = sorted(flv_files)[-1]
        mp4_path = flv_path.replace(".flv", ".mp4")

        logger.info("DVR: converting %s -> %s", flv_path, mp4_path)

        # FFmpeg: FLV → MP4 (fast remux, no re-encode)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-i", flv_path,
            "-c", "copy",
            "-movflags", "+faststart",
            mp4_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error("DVR ffmpeg failed for %s: %s", flv_path, stderr.decode())
            await _update_stream(stream_id, error=f"ffmpeg failed: {stderr.decode()[-200:]}")
            return

        logger.info("DVR: ffmpeg done, uploading to DO Spaces")

        # Upload MP4 to DO Spaces
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        object_key = f"recordings/{stream_key}/{stream_id}_{timestamp}.mp4"

        loop = asyncio.get_event_loop()
        mp4_url = await loop.run_in_executor(None, upload_file, mp4_path, object_key, True)

        logger.info("DVR: uploaded MP4 -> %s", mp4_url)

        # Update DB record
        await _update_stream(stream_id, do_mp4_path=object_key)

        # Clean up local files
        try:
            os.remove(flv_path)
            os.remove(mp4_path)
        except OSError:
            pass

    except Exception as e:
        logger.exception("DVR processing error for stream_id=%s: %s", stream_id, e)
        await _update_stream(stream_id, error=str(e))


async def _update_stream(stream_id: str, do_mp4_path: str | None = None, error: str | None = None):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(LiveStream).where(LiveStream.id == stream_id))
        stream = result.scalar_one_or_none()
        if stream:
            if do_mp4_path:
                stream.do_mp4_path = do_mp4_path
            if error:
                stream.error_message = error
            await db.commit()
