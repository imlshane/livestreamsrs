from fastapi import APIRouter
from app.redis_client import get_redis

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    redis = await get_redis()
    try:
        await redis.ping()
        redis_ok = True
    except Exception:
        redis_ok = False

    return {
        "status": "ok",
        "service": "Zinrai Livestream API",
        "redis": "ok" if redis_ok else "error",
    }
