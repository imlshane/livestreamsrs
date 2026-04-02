"""
HLS Syncer — watches /hls-data and uploads new segments + manifests to DO Spaces.

SRS writes:  /hls-data/live/{stream_key}/seg-{N}.ts
             /hls-data/live/{stream_key}/index.m3u8

Syncer uploads to DO Spaces:
             live/{stream_key}/seg-{N}.ts
             live/{stream_key}/index.m3u8

Viewers pull from:
             https://{DO_SPACES_CDN_URL}/live/{stream_key}/index.m3u8
"""
import logging
import mimetypes
import os
import threading
import time
from pathlib import Path
from queue import Empty, Queue

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers.polling import PollingObserver

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
)
logger = logging.getLogger("syncer")

HLS_PATH = os.getenv("HLS_PATH", "/hls-data")
DO_SPACES_ACCESS_KEY = os.getenv("DO_SPACES_ACCESS_KEY")
DO_SPACES_SECRET_KEY = os.getenv("DO_SPACES_SECRET_KEY")
DO_SPACES_BUCKET = os.getenv("DO_SPACES_BUCKET", "zinrai-live-stream-cdn")
DO_SPACES_REGION = os.getenv("DO_SPACES_REGION", "nyc3")
DO_SPACES_ENDPOINT = os.getenv("DO_SPACES_ENDPOINT", "https://nyc3.digitaloceanspaces.com")
DO_SPACES_CDN_URL = os.getenv("DO_SPACES_CDN_URL", "")

s3 = boto3.client(
    "s3",
    region_name=DO_SPACES_REGION,
    endpoint_url=DO_SPACES_ENDPOINT,
    aws_access_key_id=DO_SPACES_ACCESS_KEY,
    aws_secret_access_key=DO_SPACES_SECRET_KEY,
    config=Config(
        signature_version="s3v4",
        retries={"max_attempts": 3, "mode": "adaptive"},
    ),
)


def upload(local_path: str, object_key: str):
    content_type, _ = mimetypes.guess_type(local_path)
    if not content_type:
        content_type = "application/octet-stream"

    is_manifest = local_path.endswith(".m3u8")
    cache_control = "no-cache, no-store, must-revalidate" if is_manifest else "public, max-age=31536000"

    try:
        s3.upload_file(
            local_path,
            DO_SPACES_BUCKET,
            object_key,
            ExtraArgs={
                "ContentType": content_type,
                "CacheControl": cache_control,
                "ACL": "public-read",
            },
        )
        logger.info("uploaded: %s", object_key)
    except ClientError as e:
        logger.error("upload failed %s: %s", object_key, e)
    except FileNotFoundError:
        # File may have been cleaned up by SRS before we uploaded
        pass


def local_to_object_key(local_path: str) -> str:
    """Convert /hls-data/live/STREAMKEY/seg-1.ts -> live/STREAMKEY/seg-1.ts"""
    rel = os.path.relpath(local_path, HLS_PATH)
    return rel.replace(os.sep, "/")


class HLSEventHandler(FileSystemEventHandler):
    def __init__(self, upload_queue: Queue):
        self.upload_queue = upload_queue

    def on_created(self, event):
        if event.is_directory:
            return
        self._enqueue(event.src_path)

    def on_modified(self, event):
        if event.is_directory:
            return
        if event.src_path.endswith(".m3u8"):
            self._enqueue(event.src_path)

    def on_moved(self, event):
        # SRS writes seg-N.ts.tmp then renames to seg-N.ts — catch the rename
        if event.is_directory:
            return
        self._enqueue(event.dest_path)

    def _enqueue(self, path: str):
        if path.endswith((".ts", ".m3u8")):
            self.upload_queue.put(path)


def upload_worker(upload_queue: Queue):
    """Consume upload queue in a dedicated thread."""
    while True:
        try:
            local_path = upload_queue.get(timeout=5)
            object_key = local_to_object_key(local_path)
            upload(local_path, object_key)
            upload_queue.task_done()
        except Empty:
            continue
        except Exception as e:
            logger.exception("upload_worker error: %s", e)


def main():
    logger.info("HLS Syncer starting, watching: %s", HLS_PATH)
    logger.info("Target bucket: %s", DO_SPACES_BUCKET)

    Path(HLS_PATH).mkdir(parents=True, exist_ok=True)

    upload_queue: Queue = Queue(maxsize=500)

    # Start upload worker threads (2 threads for parallel uploads)
    for i in range(2):
        t = threading.Thread(target=upload_worker, args=(upload_queue,), daemon=True)
        t.start()

    handler = HLSEventHandler(upload_queue)
    observer = PollingObserver(timeout=0.5)  # poll every 500ms — reliable across Docker volumes
    observer.schedule(handler, HLS_PATH, recursive=True)
    observer.start()

    logger.info("Syncer running")
    try:
        while True:
            time.sleep(1)
            if not observer.is_alive():
                logger.error("Observer died, restarting")
                observer.start()
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
