from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "Zinrai Livestream API"
    debug: bool = False
    domain: str = "livestream.zinrai.live"

    database_url: str
    redis_url: str
    redis_prefix: str = "livestream:"

    do_spaces_access_key: str
    do_spaces_secret_key: str
    do_spaces_bucket: str = "zinrai-live-stream-cdn"
    do_spaces_region: str = "nyc3"
    do_spaces_endpoint: str = "https://nyc3.digitaloceanspaces.com"
    do_spaces_cdn_url: str = "https://zinrai-live-stream-cdn.nyc3.digitaloceanspaces.com"

    srs_publish_secret: str
    srs_api_url: str = "http://srs:1985"

    jwt_secret_key: str
    jwt_expiry_minutes: int = 480

    dvr_path: str = "/dvr"
    hls_path: str = "/hls-data"

    stream_max_duration_seconds: int = 7200
    max_concurrent_streams: int = 5

    allowed_origins: str = "https://zinrai.live"

    @property
    def origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",")]

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
