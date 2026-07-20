import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.bot import create_telegram_application
from app.config import settings
from app.services.cleanup import cleanup_stale_sessions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    stale_removed = cleanup_stale_sessions(settings.session_ttl_hours * 3600)
    if stale_removed:
        logger.info("Removed %s stale session(s) on startup", stale_removed)

    telegram_app = create_telegram_application()
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)
    logger.info("Telegram bot started")

    try:
        yield
    finally:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        logger.info("Telegram bot stopped")


app = FastAPI(
    title="YouTube Shorts Automation",
    description="Telegram bot pipeline for X/Twitter video to YouTube Shorts",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
