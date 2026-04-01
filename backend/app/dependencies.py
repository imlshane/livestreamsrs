from fastapi import Header, HTTPException, status
from app.config import settings


async def verify_internal(x_internal_secret: str = Header(default="")):
    """Protect internal-only endpoints (e.g. called by syncer or scripts)."""
    if x_internal_secret != settings.srs_publish_secret:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
