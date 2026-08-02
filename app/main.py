import asyncio
import logging
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI, Header, HTTPException, Request, Response
from telegram import Bot, Update

from app.bot import create_telegram_application, youtube_uploader
from app.config import settings
from app.services.cleanup import cleanup_stale_sessions, clear_video_storage_dir
from app.services.youtube_uploader import YouTubeUploadError, refresh_credentials

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _webhook_enabled() -> bool:
    return bool(settings.telegram_webhook_url.strip())


def _webhook_path() -> str:
    path = settings.telegram_webhook_path.strip() or "/telegram/webhook"
    return path if path.startswith("/") else f"/{path}"


def _webhook_url() -> str:
    return f"{settings.telegram_webhook_url.rstrip('/')}{_webhook_path()}"


async def _notify_admin(bot: Bot, text: str) -> None:
    try:
        await bot.send_message(chat_id=settings.admin_chat_id, text=text)
    except Exception:
        logger.exception("Failed to notify admin about YouTube token refresh")


async def _refresh_youtube_token_once(bot: Bot) -> None:
    try:
        creds = await asyncio.to_thread(refresh_credentials)
        youtube_uploader.invalidate()
        expiry = creds.expiry.isoformat() if creds.expiry else "unknown"
        await _notify_admin(
            bot,
            f"YouTube OAuth token refreshed successfully.\nAccess token expiry: {expiry}",
        )
    except YouTubeUploadError as exc:
        logger.error("YouTube token refresh failed: %s", exc)
        await _notify_admin(bot, f"YouTube OAuth token refresh failed:\n{exc}")
    except Exception as exc:
        logger.exception("Unexpected YouTube token refresh error")
        await _notify_admin(
            bot,
            f"YouTube OAuth token refresh failed unexpectedly:\n{exc}",
        )


async def _youtube_token_refresh_loop(bot: Bot) -> None:
    interval_hours = max(1, settings.youtube_token_refresh_hours)
    interval_seconds = interval_hours * 3600
    # Refresh shortly after startup so a redeploy does not wait a full day.
    await asyncio.sleep(15)
    while True:
        await _refresh_youtube_token_once(bot)
        await asyncio.sleep(interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    stale_removed = cleanup_stale_sessions(settings.session_ttl_hours * 3600)
    if stale_removed:
        logger.info("Removed %s stale session(s) on startup", stale_removed)

    # Sessions are in-memory; leftover media from a previous process is always orphaned.
    orphaned = clear_video_storage_dir(settings.video_storage_path)
    if orphaned:
        logger.info("Cleared %s orphaned media item(s) on startup", orphaned)

    use_webhook = _webhook_enabled()
    telegram_app = create_telegram_application(use_webhook=use_webhook)
    app.state.telegram_app = telegram_app

    await telegram_app.initialize()
    await telegram_app.start()

    if use_webhook:
        webhook_url = _webhook_url()
        webhook_kwargs: dict = {
            "url": webhook_url,
            "allowed_updates": Update.ALL_TYPES,
            "drop_pending_updates": True,
        }
        secret = settings.telegram_webhook_secret.strip()
        if secret:
            webhook_kwargs["secret_token"] = secret
        await telegram_app.bot.set_webhook(**webhook_kwargs)
        logger.info("Telegram bot started (webhook: %s)", webhook_url)
    else:
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        logger.info(
            "Telegram bot started (long polling; set TELEGRAM_WEBHOOK_URL to use webhooks)"
        )

    refresh_task = asyncio.create_task(
        _youtube_token_refresh_loop(telegram_app.bot),
        name="youtube-token-refresh",
    )
    logger.info(
        "YouTube token refresh scheduled every %s hour(s)",
        max(1, settings.youtube_token_refresh_hours),
    )

    try:
        yield
    finally:
        refresh_task.cancel()
        try:
            await refresh_task
        except asyncio.CancelledError:
            pass
        if use_webhook:
            await telegram_app.bot.delete_webhook()
        elif telegram_app.updater is not None:
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


async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> Response:
    """Receive Telegram updates pushed to the configured webhook URL."""
    if not _webhook_enabled():
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail="Webhook mode is disabled",
        )

    expected_secret = settings.telegram_webhook_secret.strip()
    if expected_secret and x_telegram_bot_api_secret_token != expected_secret:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail="Invalid secret token",
        )

    telegram_app = request.app.state.telegram_app
    data = await request.json()
    update = Update.de_json(data=data, bot=telegram_app.bot)
    await telegram_app.update_queue.put(update)
    return Response(status_code=HTTPStatus.OK)


app.add_api_route(
    _webhook_path(),
    telegram_webhook,
    methods=["POST"],
    name="telegram_webhook",
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
