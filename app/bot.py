import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import settings
from app.schemas import JobMode, JobStatus, ShortsMetadata
from app.services.cleanup import delete_video_file, discard_job
from app.services.gemini_client import GeminiMetadataClient, GeminiMetadataError
from app.services.twitter_downloader import TwitterDownloadError, download_twitter_video
from app.services.youtube_uploader import YouTubeUploadError, YouTubeUploader
from app.session_store import session_store
from app.utils.metadata_rules import (
    extract_twitter_url,
    is_twitter_url,
    metadata_to_json,
    parse_modified_json,
)

logger = logging.getLogger(__name__)

ACTION_APPROVE = "approve"
ACTION_DECLINE = "decline"
ACTION_MODIFY = "modify"

gemini_client = GeminiMetadataClient()
youtube_uploader = YouTubeUploader()


def _is_admin(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.id == settings.admin_chat_id


async def _reject_unauthorized(update: Update) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "Unauthorized. This bot is restricted to the configured admin."
        )


def _build_action_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Approve", callback_data=f"{ACTION_APPROVE}:{job_id}"),
                InlineKeyboardButton("Decline", callback_data=f"{ACTION_DECLINE}:{job_id}"),
            ],
            [InlineKeyboardButton("Modify", callback_data=f"{ACTION_MODIFY}:{job_id}")],
        ]
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        await _reject_unauthorized(update)
        return

    await update.effective_message.reply_text(
        "Send an X/Twitter post URL with a video.\n\n"
        "I will download it, generate Shorts metadata with Gemini, "
        "and ask you to Approve, Decline, or Modify before publishing."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        await _reject_unauthorized(update)
        return

    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.strip()
    active = session_store.get_active_for_chat(message.chat_id)

    if active and active.mode == JobMode.AWAITING_MODIFIED_JSON:
        await _handle_modified_json(update, active.job_id, text)
        return

    if active and active.mode == JobMode.PROCESSING:
        await message.reply_text("A job is already processing. Please wait.")
        return

    if active and active.status == JobStatus.PENDING_REVIEW:
        await message.reply_text(
            "You have a pending review. Use the buttons on the latest metadata message, "
            "or Decline it before starting a new URL."
        )
        return

    if not is_twitter_url(text):
        await message.reply_text("Please send a valid X/Twitter post URL containing a video.")
        return

    twitter_url = extract_twitter_url(text)
    if not twitter_url:
        await message.reply_text("Could not parse the X/Twitter URL.")
        return

    await _process_new_url(update, twitter_url)


async def _process_new_url(update: Update, twitter_url: str) -> None:
    message = update.effective_message
    assert message is not None

    status_msg = await message.reply_text("Downloading video from X/Twitter...")

    job_id: str | None = None
    video_path = None
    try:
        from uuid import uuid4

        job_id = uuid4().hex[:12]
        video_path = download_twitter_video(
            twitter_url,
            settings.video_storage_path,
            job_id,
        )
        stored_video_path = str(video_path)
        # #region agent log
        import json as _json, time as _time
        with open("debug-25cf5d.log", "a", encoding="utf-8") as _f:
            _f.write(_json.dumps({"sessionId": "25cf5d", "hypothesisId": "A", "location": "bot.py:create_job", "message": "storing video_path in session", "data": {"type": type(stored_video_path).__name__, "value": stored_video_path, "job_id": job_id}, "timestamp": int(_time.time() * 1000), "runId": "pre-fix"}) + "\n")
        # #endregion
        session_store.create_job(
            message.chat_id,
            twitter_url,
            stored_video_path,
            job_id=job_id,
        )

        await status_msg.edit_text("Generating metadata with Gemini...")
        metadata = gemini_client.generate_metadata(video_path)
        await _send_review_message(message, job_id, metadata, status_msg)
    except TwitterDownloadError as exc:
        if job_id:
            discard_job(job_id)
        elif video_path:
            delete_video_file(video_path)
        await status_msg.edit_text(f"Download failed: {exc}")
    except GeminiMetadataError as exc:
        if job_id:
            discard_job(job_id)
        elif video_path:
            delete_video_file(video_path)
        await status_msg.edit_text(f"Gemini processing failed: {exc}")
    except Exception:
        logger.exception("Unexpected error processing URL")
        if job_id:
            discard_job(job_id)
        elif video_path:
            delete_video_file(video_path)
        await status_msg.edit_text("An unexpected error occurred while processing the URL.")


async def _send_review_message(
    message,
    job_id: str,
    metadata: ShortsMetadata,
    status_msg=None,
) -> None:
    json_text = metadata_to_json(metadata)
    body = (
        "Generated metadata (review before publishing):\n\n"
        f"<pre><code>{html.escape(json_text)}</code></pre>"
    )
    review_message = await message.reply_text(
        body,
        parse_mode=ParseMode.HTML,
        reply_markup=_build_action_keyboard(job_id),
    )
    session_store.update_metadata(
        job_id,
        metadata,
        review_message_id=review_message.message_id,
        mode=JobMode.AWAITING_URL,
    )
    if status_msg:
        await status_msg.delete()


async def _handle_modified_json(update: Update, job_id: str, text: str) -> None:
    message = update.effective_message
    assert message is not None

    session = session_store.get(job_id)
    if not session:
        await message.reply_text("This job no longer exists.")
        return

    try:
        metadata = parse_modified_json(text)
    except ValueError as exc:
        await message.reply_text(f"Invalid metadata JSON: {exc}")
        return

    await _send_review_message(message, job_id, metadata)
    await message.reply_text("Updated metadata received. Review the latest JSON and choose an action.")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()

    if not _is_admin(update):
        await query.edit_message_text("Unauthorized.")
        return

    try:
        action, job_id = query.data.split(":", 1)
    except ValueError:
        await query.edit_message_text("Invalid action.")
        return

    session = session_store.get(job_id)
    if not session:
        await query.edit_message_text("This job no longer exists.")
        return

    if session.chat_id != query.message.chat_id:
        await query.answer("This action belongs to another chat.", show_alert=True)
        return

    if action == ACTION_MODIFY:
        session_store.set_mode(job_id, JobMode.AWAITING_MODIFIED_JSON)
        await query.message.reply_text(
            "Send the modified metadata as JSON with these fields:\n"
            "`title`, `description`, `viral_title_tags`, `shorts_tags`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if action == ACTION_DECLINE:
        delete_video_file(session.video_path)
        session_store.complete(job_id, JobStatus.DECLINED)
        await query.edit_message_text("Declined. Video removed. No YouTube upload.")
        return

    if action == ACTION_APPROVE:
        if not session.metadata:
            await query.edit_message_text("No metadata available for upload.")
            return

        await query.edit_message_text("Uploading to YouTube Shorts...")
        # #region agent log
        import json as _json, time as _time
        from pathlib import Path as _Path
        _vp = session.video_path
        with open("debug-25cf5d.log", "a", encoding="utf-8") as _f:
            _f.write(_json.dumps({"sessionId": "25cf5d", "hypothesisId": "A,B,C", "location": "bot.py:ACTION_APPROVE", "message": "before upload_short", "data": {"type": type(_vp).__name__, "value": str(_vp), "has_exists_attr": hasattr(_vp, "exists"), "file_exists": _Path(_vp).exists() if _vp else False, "job_id": job_id}, "timestamp": int(_time.time() * 1000), "runId": "pre-fix"}) + "\n")
        # #endregion
        try:
            response = youtube_uploader.upload_short(
                video_path=session.video_path,
                metadata=session.metadata,
            )
        except YouTubeUploadError as exc:
            await query.message.reply_text(f"YouTube upload failed: {exc}")
            return
        except Exception:
            logger.exception("Unexpected YouTube upload error")
            await query.message.reply_text("An unexpected error occurred during YouTube upload.")
            return

        video_id = response.get("id", "unknown")
        youtube_url = f"https://www.youtube.com/watch?v={video_id}"
        delete_video_file(session.video_path)
        session_store.complete(job_id, JobStatus.APPROVED)
        await query.edit_message_text(
            f"Published to YouTube.\n\nVideo ID: <code>{html.escape(video_id)}</code>\n"
            f"URL: {html.escape(youtube_url)}",
            parse_mode=ParseMode.HTML,
        )
        return

    await query.edit_message_text("Unknown action.")


def create_telegram_application() -> Application:
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .build()
    )
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    application.add_handler(CallbackQueryHandler(handle_callback))
    return application
