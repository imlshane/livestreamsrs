import logging
import mimetypes
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import settings

logger = logging.getLogger(__name__)

_s3 = None


def get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client(
            "s3",
            region_name=settings.do_spaces_region,
            endpoint_url=settings.do_spaces_endpoint,
            aws_access_key_id=settings.do_spaces_access_key,
            aws_secret_access_key=settings.do_spaces_secret_key,
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "adaptive"},
            ),
        )
    return _s3


def upload_file(local_path: str, object_key: str, public: bool = True) -> str:
    """Upload a file to DO Spaces. Returns the public CDN URL."""
    s3 = get_s3()
    content_type, _ = mimetypes.guess_type(local_path)
    if not content_type:
        content_type = "application/octet-stream"

    # HLS manifests must not be cached
    cache_control = "no-cache, no-store, must-revalidate" if local_path.endswith(".m3u8") else "public, max-age=3600"

    extra_args = {
        "ContentType": content_type,
        "CacheControl": cache_control,
    }
    if public:
        extra_args["ACL"] = "public-read"

    try:
        s3.upload_file(local_path, settings.do_spaces_bucket, object_key, ExtraArgs=extra_args)
        return f"{settings.do_spaces_cdn_url}/{object_key}"
    except ClientError as e:
        logger.error("DO Spaces upload failed: %s -> %s: %s", local_path, object_key, e)
        raise


def upload_bytes(data: bytes, object_key: str, content_type: str, public: bool = True) -> str:
    """Upload raw bytes to DO Spaces."""
    s3 = get_s3()
    cache_control = "no-cache, no-store, must-revalidate" if object_key.endswith(".m3u8") else "public, max-age=3600"
    extra_args: dict = {"ContentType": content_type, "CacheControl": cache_control}
    if public:
        extra_args["ACL"] = "public-read"

    try:
        s3.put_object(
            Bucket=settings.do_spaces_bucket,
            Key=object_key,
            Body=data,
            **extra_args,
        )
        return f"{settings.do_spaces_cdn_url}/{object_key}"
    except ClientError as e:
        logger.error("DO Spaces put_object failed: %s: %s", object_key, e)
        raise


def delete_prefix(prefix: str):
    """Delete all objects under a prefix (cleanup after stream)."""
    s3 = get_s3()
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=settings.do_spaces_bucket, Prefix=prefix):
            objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if objects:
                s3.delete_objects(Bucket=settings.do_spaces_bucket, Delete={"Objects": objects})
    except ClientError as e:
        logger.error("DO Spaces delete_prefix failed: %s: %s", prefix, e)
