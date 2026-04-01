from datetime import datetime
from typing import Optional
from pydantic import BaseModel


# --- SRS Hook payloads ---

class SRSPublishPayload(BaseModel):
    action: str
    client_id: str
    ip: str
    vhost: str
    app: str
    stream: str          # this is the stream_key
    param: str = ""      # query string, e.g. ?secret=xxx


class SRSUnpublishPayload(BaseModel):
    action: str
    client_id: str
    ip: str
    vhost: str
    app: str
    stream: str
    param: str = ""


class SRSHlsPayload(BaseModel):
    action: str
    client_id: str
    ip: str
    vhost: str
    app: str
    stream: str
    param: str = ""
    duration: float = 0
    cwd: str = ""
    file: str = ""
    url: str = ""
    m3u8: str = ""
    m3u8_url: str = ""
    seq_no: int = 0


class SRSErrorPayload(BaseModel):
    action: str
    client_id: str = ""
    ip: str = ""
    vhost: str = ""
    app: str = ""
    stream: str = ""
    param: str = ""
    msg: str = ""


# --- Stream responses ---

class LiveStreamOut(BaseModel):
    id: str
    stream_key: str
    educator_id: Optional[str]
    educator_name: Optional[str] = None
    title: Optional[str]
    status: str
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    duration_seconds: Optional[float]
    viewer_peak: int
    hls_manifest_url: Optional[str]
    do_mp4_path: Optional[str]
    viewer_count: int = 0
    created_at: datetime

    class Config:
        from_attributes = True


class ViewerCountOut(BaseModel):
    stream_id: str
    stream_key: str
    viewer_count: int
    viewer_peak: int


class ActiveStreamsOut(BaseModel):
    streams: list[LiveStreamOut]
    total: int
