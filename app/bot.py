import asyncio
import html
import logging
from pathlib import Path
from uuid import uuid4

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
from app.data.pixabay_tags import pick_pixabay_search_query
from app.schemas import (
    ChatFlow,
    JobMode,
    JobSource,
    JobStatus,
    ReviewStage,
    ShortsMetadata,
)
from app.services.cleanup import (
    clear_video_storage_dir,
    delete_pixabay_job_files,
    delete_video_file,
    discard_job,
)
from app.services.ffmpeg_utils import FFmpegError, mux_audio_onto_video, probe_duration_seconds
from app.services.gemini_client import GeminiMetadataClient, GeminiMetadataError
from app.services.pixabay_audio_client import (
    PixabayAudioError,
    PixabayAudioExhaustedError,
    PixabayAudioResult,
    find_and_download_audio,
)
from app.services.pixabay_client import (
    PixabayError,
    PixabayExhaustedError,
    PixabayStream,
    PixabayVideoResult,
    find_and_download_video,
)
from app.services.twitter_downloader import TwitterDownloadError, download_twitter_video
from app.services.video_pipeline import HuggingFaceVideoPipeline, VideoPipelineError
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
ACTION_MODIFY_AUDIO = "modify_audio"
ACTION_MODIFY_VIDEO = "modify_video"
ACTION_BACK_MENU = "back_menu"
ACTION_START_PIXABAY = "start_pixabay"

MENU_TEXT = (
    "Available commands:\n"
    "/twitter — download an X/Twitter video, generate Shorts metadata, then publish\n"
    "/hugging_face — generate an AI Short for Midnight Souls, then review & publish\n"
    "/pixabay — fetch a 9:16 HD/4K Pixabay Short for Midnight Souls, then review & publish"
)

MAX_PIXABAY_PHRASE_ATTEMPTS = 3

gemini_client = GeminiMetadataClient()
youtube_uploader = YouTubeUploader()
hf_pipeline = HuggingFaceVideoPipeline(gemini_client=gemini_client)


def _is_admin(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.id == settings.admin_chat_id


async def _reject_unauthorized(update: Update) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "Unauthorized. This bot is restricted to the configured admin."
        )


def _build_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Back to menu", callback_data=ACTION_BACK_MENU)]]
    )


def _build_pixabay_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Start", callback_data=ACTION_START_PIXABAY)],
            [InlineKeyboardButton("Back to menu", callback_data=ACTION_BACK_MENU)],
        ]
    )


def _build_action_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Approve", callback_data=f"{ACTION_APPROVE}:{job_id}"),
                InlineKeyboardButton("Decline", callback_data=f"{ACTION_DECLINE}:{job_id}"),
            ],
            [InlineKeyboardButton("Modify", callback_data=f"{ACTION_MODIFY}:{job_id}")],
            [InlineKeyboardButton("Back to menu", callback_data=ACTION_BACK_MENU)],
        ]
    )


def _build_pixabay_video_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Approve", callback_data=f"{ACTION_APPROVE}:{job_id}"),
                InlineKeyboardButton("Decline", callback_data=f"{ACTION_DECLINE}:{job_id}"),
            ],
            [
                InlineKeyboardButton(
                    "Modify audio", callback_data=f"{ACTION_MODIFY_AUDIO}:{job_id}"
                ),
                InlineKeyboardButton(
                    "Modify video", callback_data=f"{ACTION_MODIFY_VIDEO}:{job_id}"
                ),
            ],
            [InlineKeyboardButton("Back to menu", callback_data=ACTION_BACK_MENU)],
        ]
    )


def _has_blocking_job(chat_id: int) -> tuple[bool, str | None]:
    active = session_store.get_active_for_chat(chat_id)
    if not active:
        return False, None
    if active.mode == JobMode.PROCESSING:
        return True, "A job is already processing. Please wait."
    if active.status == JobStatus.PENDING_REVIEW:
        if active.mode == JobMode.AWAITING_MODIFIED_JSON:
            return (
                True,
                "You are waiting to send modified JSON for a pending review. "
                "Use Back to menu, or send the JSON, or Decline the review first.",
            )
        stage = "video" if active.review_stage == ReviewStage.VIDEO else "metadata"
        return (
            True,
            f"You have a pending {stage} review. Use the buttons on the latest review "
            "message, or Decline it before starting a new job.",
        )
    return False, None


async def _edit_callback_message(query, text: str, *, parse_mode: str | None = None) -> None:
    """Edit text or caption depending on whether the callback message is a video."""
    message = query.message
    if message is None:
        return
    try:
        if message.video or message.document or message.animation:
            await query.edit_message_caption(caption=text, parse_mode=parse_mode)
        else:
            await query.edit_message_text(text, parse_mode=parse_mode)
    except Exception:
        await message.reply_text(text, parse_mode=parse_mode)


async def _send_menu(message) -> None:
    await message.reply_text(MENU_TEXT)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        await _reject_unauthorized(update)
        return

    await _send_menu(update.effective_message)


async def twitter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        await _reject_unauthorized(update)
        return

    message = update.effective_message
    if not message:
        return

    blocked, reason = _has_blocking_job(message.chat_id)
    if blocked:
        await message.reply_text(reason or "A job is already in progress.")
        return

    session_store.set_chat_flow(message.chat_id, ChatFlow.TWITTER)
    await message.reply_text(
        "Send an X/Twitter post URL with a video.\n\n"
        "I will download it, generate Shorts metadata with Gemini, "
        "and ask you to Approve, Decline, or Modify before publishing.",
        reply_markup=_build_back_keyboard(),
    )


async def hugging_face_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        await _reject_unauthorized(update)
        return

    message = update.effective_message
    if not message:
        return

    blocked, reason = _has_blocking_job(message.chat_id)
    if blocked:
        await message.reply_text(reason or "A job is already in progress.")
        return

    if not settings.hf_token:
        await message.reply_text(
            "HF_TOKEN is not configured. Add a Hugging Face token with "
            "Inference Providers permission to .env, then retry /hugging_face."
        )
        return

    session_store.set_chat_flow(message.chat_id, ChatFlow.HUGGING_FACE)
    await _run_hugging_face_generation(message)


async def pixabay_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        await _reject_unauthorized(update)
        return

    message = update.effective_message
    if not message:
        return

    blocked, reason = _has_blocking_job(message.chat_id)
    if blocked:
        await message.reply_text(reason or "A job is already in progress.")
        return

    if not settings.pixabay_api_key:
        await message.reply_text(
            "PIXABAY_API_KEY is not configured. Get a free key at "
            "https://pixabay.com/api/docs/ (log in to see it), add it to .env, "
            "then retry /pixabay."
        )
        return

    session_store.set_chat_flow(message.chat_id, ChatFlow.PIXABAY)
    await message.reply_text(
        "Ready to fetch a Midnight Souls Pixabay Short.\n\n"
        "I will pick 3–4 tags from the library, then download a 9:16 HD/4K "
        "video (≤60s, original quality) for review before publishing.\n\n"
        "Tap Start to begin, or Back to menu if this was a misclick.",
        reply_markup=_build_pixabay_confirm_keyboard(),
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
        stage = "video" if active.review_stage == ReviewStage.VIDEO else "metadata"
        await message.reply_text(
            f"You have a pending {stage} review. Use the buttons on the latest review "
            "message, or Decline it before starting a new job."
        )
        return

    if session_store.get_chat_flow(message.chat_id) != ChatFlow.TWITTER:
        await message.reply_text(
            "Use /twitter, /hugging_face, or /pixabay to start a Shorts flow, "
            "or /start for available commands."
        )
        return

    if not is_twitter_url(text):
        await message.reply_text(
            "Please send a valid X/Twitter post URL containing a video.",
            reply_markup=_build_back_keyboard(),
        )
        return

    twitter_url = extract_twitter_url(text)
    if not twitter_url:
        await message.reply_text(
            "Could not parse the X/Twitter URL.",
            reply_markup=_build_back_keyboard(),
        )
        return

    await _process_new_url(update, twitter_url)


async def _process_new_url(update: Update, twitter_url: str) -> None:
    message = update.effective_message
    assert message is not None

    status_msg = await message.reply_text("Downloading video from X/Twitter...")

    job_id: str | None = None
    video_path = None
    try:
        job_id = uuid4().hex[:12]
        clear_video_storage_dir(settings.video_storage_path)
        video_path = download_twitter_video(
            twitter_url,
            settings.video_storage_path,
            job_id,
        )
        stored_video_path = str(video_path)
        session_store.create_job(
            message.chat_id,
            stored_video_path,
            job_id=job_id,
            source=JobSource.TWITTER,
            twitter_url=twitter_url,
            review_stage=ReviewStage.METADATA,
        )

        await status_msg.edit_text("Generating metadata with Gemini...")
        metadata = await asyncio.to_thread(gemini_client.generate_metadata, video_path)
        await _send_metadata_review(message, job_id, metadata, status_msg)
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


async def _run_hugging_face_generation(message, *, existing_job_id: str | None = None) -> None:
    status_msg = await message.reply_text("Starting Hugging Face Shorts generation...")
    job_id = existing_job_id or uuid4().hex[:12]
    video_path: Path | None = None

    loop = asyncio.get_running_loop()
    progress_queue: asyncio.Queue[str | None] = asyncio.Queue()

    def on_progress(text: str) -> None:
        loop.call_soon_threadsafe(progress_queue.put_nowait, text)

    async def drain_progress() -> None:
        while True:
            item = await progress_queue.get()
            if item is None:
                break
            try:
                await status_msg.edit_text(item)
            except Exception:
                logger.debug("Could not edit progress message", exc_info=True)

    progress_task = asyncio.create_task(drain_progress())

    try:
        if existing_job_id:
            session = session_store.get(existing_job_id)
            if not session:
                await progress_queue.put(None)
                await progress_task
                await status_msg.edit_text("This job no longer exists.")
                await message.reply_text(MENU_TEXT)
                return
            if session.video_path:
                delete_video_file(session.video_path)
            session_store.set_mode(job_id, JobMode.PROCESSING)
            session_store.set_review_stage(job_id, ReviewStage.VIDEO)
        else:
            clear_video_storage_dir(settings.video_storage_path)
            pending_path = str(settings.video_storage_path / f"{job_id}.pending")
            session_store.create_job(
                message.chat_id,
                pending_path,
                job_id=job_id,
                source=JobSource.HUGGING_FACE,
                review_stage=ReviewStage.VIDEO,
            )

        video_path, plan = await asyncio.to_thread(
            hf_pipeline.generate,
            settings.video_storage_path,
            job_id,
            on_progress=on_progress,
        )
        await progress_queue.put(None)
        await progress_task

        session_store.update_video(
            job_id,
            str(video_path),
            video_prompts=plan.model_dump(),
            review_stage=ReviewStage.VIDEO,
            mode=JobMode.AWAITING_URL,
        )
        await _send_video_review(message, job_id, video_path, plan.scene_summary, status_msg)
    except VideoPipelineError as exc:
        await progress_queue.put(None)
        await progress_task
        if job_id:
            discard_job(job_id)
        elif video_path:
            delete_video_file(video_path)
        await status_msg.edit_text(f"Hugging Face generation failed: {exc}")
        await message.reply_text(MENU_TEXT)
    except Exception:
        logger.exception("Unexpected Hugging Face generation error")
        await progress_queue.put(None)
        await progress_task
        if job_id:
            discard_job(job_id)
        elif video_path:
            delete_video_file(video_path)
        await status_msg.edit_text("An unexpected error occurred during video generation.")
        await message.reply_text(MENU_TEXT)


def _pixabay_review_caption(
    video: PixabayVideoResult,
    audio: PixabayAudioResult,
) -> str:
    return (
        "Pixabay Midnight Souls Short (video + audio review):\n"
        f"Phrase: {video.phrase}\n"
        f"{video.attribution}\n"
        f"{audio.attribution}\n\n"
        "Approve → generate title/description with Gemini\n"
        "Modify audio → keep video, fetch different music (same tags)\n"
        "Modify video → new tags, new video + new music\n"
        "Decline → discard"
    )


def _cleanup_pixabay_in_progress(
    job_id: str,
    *,
    silent_path: Path | None = None,
    audio_path: Path | None = None,
    video_path: Path | None = None,
) -> None:
    """Best-effort wipe of any mid-flow Pixabay downloads that may not be in session meta yet."""
    for path in (silent_path, audio_path, video_path):
        if path:
            delete_video_file(path)
    delete_pixabay_job_files(job_id, settings.video_storage_path)


async def _run_pixabay_generation(
    message,
    *,
    existing_job_id: str | None = None,
    modify_audio_only: bool = False,
) -> None:
    status_msg = await message.reply_text("Starting Pixabay Shorts fetch...")
    job_id = existing_job_id or uuid4().hex[:12]
    video_path: Path | None = None
    used_ids: list[int] = []
    used_audio_ids: list[int] = []
    phrase: str | None = None
    silent_path: Path | None = None
    audio_result: PixabayAudioResult | None = None

    try:
        if existing_job_id:
            session = session_store.get(existing_job_id)
            if not session:
                await status_msg.edit_text("This job no longer exists.")
                await message.reply_text(MENU_TEXT)
                return
            used_ids = list(session.pixabay_used_ids or [])
            used_audio_ids = list(session.pixabay_used_audio_ids or [])
            phrase = session.pixabay_phrase
            meta = session.pixabay_meta if isinstance(session.pixabay_meta, dict) else {}
            if meta.get("silent_path"):
                silent_path = Path(str(meta["silent_path"]))
            session_store.set_mode(job_id, JobMode.PROCESSING)
            session_store.set_review_stage(job_id, ReviewStage.VIDEO)

            if modify_audio_only:
                if not silent_path or not silent_path.exists():
                    await status_msg.edit_text(
                        "Silent video is missing; cannot modify audio. Try Modify video."
                    )
                    session_store.set_mode(job_id, JobMode.AWAITING_URL)
                    return
                if not phrase:
                    await status_msg.edit_text(
                        "Search phrase is missing; cannot modify audio. Try Modify video."
                    )
                    session_store.set_mode(job_id, JobMode.AWAITING_URL)
                    return

                old_muxed = Path(session.video_path) if session.video_path else None
                old_audio = (
                    Path(str(meta["audio_path"])) if meta.get("audio_path") else None
                )

                await status_msg.edit_text(
                    f"Searching Pixabay Music (same tags):\n{phrase}"
                )
                video_duration = await asyncio.to_thread(
                    probe_duration_seconds, silent_path
                )
                audio_result = await asyncio.to_thread(
                    find_and_download_audio,
                    phrase,
                    settings.video_storage_path,
                    job_id,
                    min_duration_seconds=video_duration,
                    used_ids=used_audio_ids,
                    filename=f"{job_id}_audio_tmp.mp3",
                )
                used_audio_ids = [*used_audio_ids, audio_result.audio_id]
                muxed_path = settings.video_storage_path / f"{job_id}.mp4"
                final_audio = settings.video_storage_path / f"{job_id}_audio.mp3"
                await status_msg.edit_text("Muxing new audio onto video...")
                # Mux to a temp file first so a failure keeps the previous review video.
                temp_muxed = settings.video_storage_path / f"{job_id}_mux_tmp.mp4"
                video_path = await asyncio.to_thread(
                    mux_audio_onto_video,
                    silent_path,
                    audio_result.local_path,
                    temp_muxed,
                )
                if muxed_path.exists():
                    muxed_path.unlink()
                temp_muxed.replace(muxed_path)
                video_path = muxed_path
                if final_audio.exists() and final_audio != audio_result.local_path:
                    final_audio.unlink()
                audio_result.local_path.replace(final_audio)
                # dataclass is frozen — rebuild result with final path
                audio_result = PixabayAudioResult(
                    audio_id=audio_result.audio_id,
                    page_url=audio_result.page_url,
                    user=audio_result.user,
                    duration=audio_result.duration,
                    phrase=audio_result.phrase,
                    download_url=audio_result.download_url,
                    local_path=final_audio,
                    name=audio_result.name,
                )
                if old_audio and old_audio.exists() and old_audio != final_audio:
                    delete_video_file(old_audio)
                if old_muxed and old_muxed.exists() and old_muxed != muxed_path:
                    delete_video_file(old_muxed)
                video_meta = {
                    k: v
                    for k, v in meta.items()
                    if k
                    not in {
                        "audio_id",
                        "audio_page_url",
                        "audio_user",
                        "audio_duration",
                        "audio_name",
                        "audio_path",
                    }
                }
                video_meta.update(
                    {
                        "silent_path": str(silent_path),
                        "audio_id": audio_result.audio_id,
                        "audio_page_url": audio_result.page_url,
                        "audio_user": audio_result.user,
                        "audio_duration": audio_result.duration,
                        "audio_name": audio_result.name,
                        "audio_path": str(audio_result.local_path),
                    }
                )
                session_store.update_video(
                    job_id,
                    str(video_path),
                    review_stage=ReviewStage.VIDEO,
                    mode=JobMode.AWAITING_URL,
                    pixabay_phrase=phrase,
                    pixabay_used_ids=used_ids,
                    pixabay_used_audio_ids=used_audio_ids,
                    pixabay_meta=video_meta,
                )
                # Build a lightweight video result for caption from stored meta.
                caption_video = PixabayVideoResult(
                    video_id=int(video_meta.get("video_id") or 0),
                    page_url=str(video_meta.get("page_url") or ""),
                    user=str(video_meta.get("user") or "unknown"),
                    duration=int(video_meta.get("duration") or 0),
                    phrase=phrase,
                    stream=PixabayStream(
                        url="",
                        width=int(video_meta.get("width") or 0),
                        height=int(video_meta.get("height") or 0),
                        size=0,
                    ),
                    local_path=silent_path,
                )
                await _send_video_review(
                    message,
                    job_id,
                    video_path,
                    phrase,
                    status_msg,
                    caption=_pixabay_review_caption(caption_video, audio_result),
                    reply_markup=_build_pixabay_video_keyboard(job_id),
                )
                return

            # Modify video: force new tags and replace silent/audio/muxed assets.
            phrase = None
            delete_pixabay_job_files(job_id, settings.video_storage_path)
            silent_path = None
            audio_result = None
        else:
            clear_video_storage_dir(settings.video_storage_path)
            pending_path = str(settings.video_storage_path / f"{job_id}.pending")
            session_store.create_job(
                message.chat_id,
                pending_path,
                job_id=job_id,
                source=JobSource.PIXABAY,
                review_stage=ReviewStage.VIDEO,
            )

        video_result: PixabayVideoResult | None = None
        last_error: Exception | None = None

        for attempt in range(1, MAX_PIXABAY_PHRASE_ATTEMPTS + 1):
            if not phrase:
                await status_msg.edit_text(
                    f"Picking Pixabay search tags "
                    f"(attempt {attempt}/{MAX_PIXABAY_PHRASE_ATTEMPTS})..."
                )
                phrase = pick_pixabay_search_query()

            await status_msg.edit_text(
                f"Searching Pixabay for 9:16 HD/4K video:\n{phrase}"
            )
            try:
                video_result = await asyncio.to_thread(
                    find_and_download_video,
                    phrase,
                    settings.video_storage_path,
                    job_id,
                    used_ids=used_ids,
                    filename=f"{job_id}_silent.mp4",
                )
            except PixabayExhaustedError as exc:
                last_error = exc
                logger.info("Pixabay video exhausted for phrase %r: %s", phrase, exc)
                phrase = None
                continue

            silent_path = video_result.local_path
            video_duration = await asyncio.to_thread(
                probe_duration_seconds, silent_path
            )
            await status_msg.edit_text(
                f"Searching Pixabay Music for matching audio:\n{phrase}"
            )
            try:
                audio_result = await asyncio.to_thread(
                    find_and_download_audio,
                    phrase,
                    settings.video_storage_path,
                    job_id,
                    min_duration_seconds=video_duration,
                    used_ids=used_audio_ids,
                )
                break
            except PixabayAudioExhaustedError as exc:
                last_error = exc
                logger.info("Pixabay audio exhausted for phrase %r: %s", phrase, exc)
                # Mark this video as used and try a new phrase set.
                used_ids = [*used_ids, video_result.video_id]
                delete_video_file(silent_path)
                silent_path = None
                video_result = None
                audio_result = None
                phrase = None
                continue
            except PixabayAudioError:
                # Non-exhausted audio failure: drop the silent clip before bubbling up.
                delete_video_file(silent_path)
                silent_path = None
                video_result = None
                audio_result = None
                raise

        if video_result is None or audio_result is None or silent_path is None:
            raise last_error or PixabayError(
                "Could not find a suitable Pixabay video+audio pair after several tag sets."
            )

        muxed_path = settings.video_storage_path / f"{job_id}.mp4"
        await status_msg.edit_text("Muxing audio onto video...")
        video_path = await asyncio.to_thread(
            mux_audio_onto_video,
            silent_path,
            audio_result.local_path,
            muxed_path,
        )

        used_ids = [*used_ids, video_result.video_id]
        used_audio_ids = [*used_audio_ids, audio_result.audio_id]
        pixabay_meta = {
            "video_id": video_result.video_id,
            "page_url": video_result.page_url,
            "user": video_result.user,
            "duration": video_result.duration,
            "width": video_result.stream.width,
            "height": video_result.stream.height,
            "silent_path": str(silent_path),
            "audio_id": audio_result.audio_id,
            "audio_page_url": audio_result.page_url,
            "audio_user": audio_result.user,
            "audio_duration": audio_result.duration,
            "audio_name": audio_result.name,
            "audio_path": str(audio_result.local_path),
        }
        session_store.update_video(
            job_id,
            str(video_path),
            review_stage=ReviewStage.VIDEO,
            mode=JobMode.AWAITING_URL,
            pixabay_phrase=video_result.phrase,
            pixabay_used_ids=used_ids,
            pixabay_used_audio_ids=used_audio_ids,
            pixabay_meta=pixabay_meta,
        )
        await _send_video_review(
            message,
            job_id,
            video_path,
            video_result.phrase,
            status_msg,
            caption=_pixabay_review_caption(video_result, audio_result),
            reply_markup=_build_pixabay_video_keyboard(job_id),
        )
    except ValueError as exc:
        if modify_audio_only and existing_job_id:
            delete_video_file(settings.video_storage_path / f"{existing_job_id}_audio_tmp.mp3")
            delete_video_file(settings.video_storage_path / f"{existing_job_id}_mux_tmp.mp4")
            session_store.set_mode(existing_job_id, JobMode.AWAITING_URL)
            await status_msg.edit_text(f"Pixabay tag library error: {exc}")
            return
        _cleanup_pixabay_in_progress(
            job_id,
            silent_path=silent_path,
            audio_path=audio_result.local_path if audio_result else None,
            video_path=video_path,
        )
        discard_job(job_id)
        await status_msg.edit_text(f"Pixabay tag library error: {exc}")
        await message.reply_text(MENU_TEXT)
    except (PixabayError, PixabayAudioError, FFmpegError) as exc:
        if modify_audio_only and existing_job_id:
            delete_video_file(settings.video_storage_path / f"{existing_job_id}_audio_tmp.mp3")
            delete_video_file(settings.video_storage_path / f"{existing_job_id}_mux_tmp.mp4")
            session_store.set_mode(existing_job_id, JobMode.AWAITING_URL)
            await status_msg.edit_text(f"Pixabay audio modify failed: {exc}")
            return
        _cleanup_pixabay_in_progress(
            job_id,
            silent_path=silent_path,
            audio_path=audio_result.local_path if audio_result else None,
            video_path=video_path,
        )
        discard_job(job_id)
        await status_msg.edit_text(f"Pixabay fetch failed: {exc}")
        await message.reply_text(MENU_TEXT)
    except Exception:
        logger.exception("Unexpected Pixabay generation error")
        if modify_audio_only and existing_job_id:
            delete_video_file(settings.video_storage_path / f"{existing_job_id}_audio_tmp.mp3")
            delete_video_file(settings.video_storage_path / f"{existing_job_id}_mux_tmp.mp4")
            session_store.set_mode(existing_job_id, JobMode.AWAITING_URL)
            await status_msg.edit_text(
                "An unexpected error occurred while modifying Pixabay audio."
            )
            return
        _cleanup_pixabay_in_progress(
            job_id,
            silent_path=silent_path,
            audio_path=audio_result.local_path if audio_result else None,
            video_path=video_path,
        )
        discard_job(job_id)
        await status_msg.edit_text("An unexpected error occurred during Pixabay fetch.")
        await message.reply_text(MENU_TEXT)


async def _send_video_review(
    message,
    job_id: str,
    video_path: Path,
    scene_summary: str,
    status_msg=None,
    *,
    caption: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    resolved_caption = caption or (
        "Generated Midnight Souls Short (video review):\n"
        f"{scene_summary}\n\n"
        "Approve → generate title/description with Gemini\n"
        "Modify → generate a new video\n"
        "Decline → discard"
    )
    markup = reply_markup or _build_action_keyboard(job_id)
    with video_path.open("rb") as video_file:
        review_message = await message.reply_video(
            video=video_file,
            caption=resolved_caption,
            reply_markup=markup,
            supports_streaming=True,
        )
    session_store.update_video(
        job_id,
        str(video_path),
        review_stage=ReviewStage.VIDEO,
        mode=JobMode.AWAITING_URL,
        review_message_id=review_message.message_id,
    )
    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass


async def _send_metadata_review(
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
        review_stage=ReviewStage.METADATA,
    )
    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass


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
        await message.reply_text(
            f"Invalid metadata JSON: {exc}",
            reply_markup=_build_back_keyboard(),
        )
        return

    await _send_metadata_review(message, job_id, metadata)
    await message.reply_text(
        "Updated metadata received. Review the latest JSON and choose an action."
    )


async def _handle_back_to_menu(update: Update) -> None:
    query = update.callback_query
    assert query is not None
    message = query.message
    chat_id = message.chat_id if message else None
    if chat_id is None:
        return

    active = session_store.get_active_for_chat(chat_id)

    if active and active.mode == JobMode.PROCESSING:
        await query.answer(
            "A job is processing. Please wait until it finishes.",
            show_alert=True,
        )
        return

    if active and active.mode == JobMode.AWAITING_MODIFIED_JSON:
        session_store.set_mode(active.job_id, JobMode.AWAITING_URL)
        session_store.set_chat_flow(chat_id, ChatFlow.IDLE)
        await query.answer()
        await message.reply_text(MENU_TEXT)
        return

    if active and active.status == JobStatus.PENDING_REVIEW:
        await query.answer(
            "You have a pending review. Decline it first to leave this job.",
            show_alert=True,
        )
        return

    session_store.set_chat_flow(chat_id, ChatFlow.IDLE)
    await query.answer()
    await message.reply_text(MENU_TEXT)


async def _handle_video_stage_action(update: Update, action: str, job_id: str) -> None:
    query = update.callback_query
    assert query is not None
    message = query.message
    session = session_store.get(job_id)
    # #region agent log
    try:
        import json as _json, time as _time
        with open("debug-083327.log", "a", encoding="utf-8") as _f:
            _f.write(_json.dumps({"sessionId":"083327","hypothesisId":"H1","location":"bot.py:_handle_video_stage_action","message":"video_stage_action_entry","data":{"action":action,"job_id":job_id,"has_session":session is not None,"has_message":message is not None},"timestamp":int(_time.time()*1000),"runId":"post-fix"}) + "\n")
    except Exception:
        pass
    # #endregion
    if not session or not message:
        # #region agent log
        try:
            import json as _json, time as _time
            with open("debug-083327.log", "a", encoding="utf-8") as _f:
                _f.write(_json.dumps({"sessionId":"083327","hypothesisId":"H1","location":"bot.py:_handle_video_stage_action","message":"early_return_missing_session_or_message","data":{"job_id":job_id},"timestamp":int(_time.time()*1000),"runId":"post-fix"}) + "\n")
        except Exception:
            pass
        # #endregion
        return

    if action == ACTION_DECLINE:
        if session.source == JobSource.PIXABAY:
            delete_pixabay_job_files(job_id)
        else:
            delete_video_file(session.video_path)
        session_store.complete(job_id, JobStatus.DECLINED)
        await _edit_callback_message(query, "Declined. Video removed.")
        await message.reply_text(MENU_TEXT)
        return

    if action == ACTION_MODIFY_AUDIO:
        if session.source != JobSource.PIXABAY:
            await _edit_callback_message(query, "Modify audio is only available for Pixabay.")
            return
        await _edit_callback_message(query, "Fetching different Pixabay music...")
        await _run_pixabay_generation(
            message, existing_job_id=job_id, modify_audio_only=True
        )
        return

    if action == ACTION_MODIFY_VIDEO:
        if session.source != JobSource.PIXABAY:
            await _edit_callback_message(query, "Modify video is only available for Pixabay.")
            return
        await _edit_callback_message(query, "Fetching another Pixabay video + audio...")
        await _run_pixabay_generation(message, existing_job_id=job_id)
        return

    if action == ACTION_MODIFY:
        if session.source == JobSource.PIXABAY:
            # Legacy single Modify button should behave like Modify video.
            await _edit_callback_message(query, "Fetching another Pixabay video + audio...")
            await _run_pixabay_generation(message, existing_job_id=job_id)
            return
        await _edit_callback_message(query, "Regenerating a new video...")
        await _run_hugging_face_generation(message, existing_job_id=job_id)
        return

    if action == ACTION_APPROVE:
        video_path = Path(session.video_path)
        if not video_path.exists():
            await _edit_callback_message(query, "Video file is missing; cannot generate metadata.")
            return

        await _edit_callback_message(query, "Generating metadata with Gemini...")
        session_store.set_mode(job_id, JobMode.PROCESSING)
        try:
            metadata = await asyncio.to_thread(gemini_client.generate_metadata, video_path)
        except GeminiMetadataError as exc:
            session_store.set_mode(job_id, JobMode.AWAITING_URL)
            await message.reply_text(
                f"Gemini processing failed: {exc}",
                reply_markup=(
                    _build_pixabay_video_keyboard(job_id)
                    if session.source == JobSource.PIXABAY
                    else _build_action_keyboard(job_id)
                ),
            )
            return
        except Exception:
            logger.exception("Unexpected Gemini metadata error")
            session_store.set_mode(job_id, JobMode.AWAITING_URL)
            await message.reply_text(
                "An unexpected error occurred while generating metadata.",
                reply_markup=(
                    _build_pixabay_video_keyboard(job_id)
                    if session.source == JobSource.PIXABAY
                    else _build_action_keyboard(job_id)
                ),
            )
            return

        await _send_metadata_review(message, job_id, metadata)
        return

    await _edit_callback_message(query, "Unknown action.")


async def _handle_metadata_stage_action(update: Update, action: str, job_id: str) -> None:
    query = update.callback_query
    assert query is not None
    message = query.message
    session = session_store.get(job_id)
    if not session or not message:
        return

    if action == ACTION_MODIFY:
        session_store.set_mode(job_id, JobMode.AWAITING_MODIFIED_JSON)
        await message.reply_text(
            "Send the modified metadata as JSON with these fields:\n"
            "`title`, `description`, `viral_title_tags`, `shorts_tags`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_build_back_keyboard(),
        )
        return

    if action == ACTION_DECLINE:
        if session.source == JobSource.PIXABAY:
            delete_pixabay_job_files(job_id)
        else:
            delete_video_file(session.video_path)
        session_store.complete(job_id, JobStatus.DECLINED)
        await _edit_callback_message(query, "Declined. Video removed. No YouTube upload.")
        await message.reply_text(MENU_TEXT)
        return

    if action == ACTION_APPROVE:
        if not session.metadata:
            await _edit_callback_message(query, "No metadata available for upload.")
            return

        await _edit_callback_message(query, "Uploading to YouTube Shorts...")
        try:
            response = await asyncio.to_thread(
                youtube_uploader.upload_short,
                session.video_path,
                session.metadata,
            )
        except YouTubeUploadError as exc:
            await message.reply_text(f"YouTube upload failed: {exc}")
            return
        except Exception:
            logger.exception("Unexpected YouTube upload error")
            await message.reply_text("An unexpected error occurred during YouTube upload.")
            return

        video_id = response.get("id", "unknown")
        youtube_url = f"https://www.youtube.com/watch?v={video_id}"
        if session.source == JobSource.PIXABAY:
            delete_pixabay_job_files(job_id)
        else:
            delete_video_file(session.video_path)
        session_store.complete(job_id, JobStatus.APPROVED)
        await _edit_callback_message(
            query,
            f"Published to YouTube.\n\nVideo ID: <code>{html.escape(video_id)}</code>\n"
            f"URL: {html.escape(youtube_url)}",
            parse_mode=ParseMode.HTML,
        )
        await message.reply_text(MENU_TEXT)
        return

    await _edit_callback_message(query, "Unknown action.")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    if not _is_admin(update):
        await query.answer()
        await _edit_callback_message(query, "Unauthorized.")
        return

    if query.data == ACTION_BACK_MENU:
        await _handle_back_to_menu(update)
        return

    if query.data == ACTION_START_PIXABAY:
        message = query.message
        if not message:
            await query.answer()
            return

        chat_id = message.chat_id
        blocked, reason = _has_blocking_job(chat_id)
        if blocked:
            await query.answer(reason or "A job is already in progress.", show_alert=True)
            return

        await query.answer()

        if not settings.pixabay_api_key:
            await _edit_callback_message(
                query,
                "PIXABAY_API_KEY is not configured. Add it to .env and retry /pixabay.",
            )
            return

        if session_store.get_chat_flow(chat_id) != ChatFlow.PIXABAY:
            session_store.set_chat_flow(chat_id, ChatFlow.PIXABAY)

        await _edit_callback_message(query, "Starting Pixabay flow...")
        await _run_pixabay_generation(message)
        return

    await query.answer()

    try:
        action, job_id = query.data.split(":", 1)
    except ValueError:
        await _edit_callback_message(query, "Invalid action.")
        return

    session = session_store.get(job_id)
    if not session:
        await _edit_callback_message(query, "This job no longer exists.")
        return

    if session.chat_id != query.message.chat_id:
        await query.answer("This action belongs to another chat.", show_alert=True)
        return

    if session.mode == JobMode.PROCESSING:
        await query.answer("This job is still processing. Please wait.", show_alert=True)
        return

    if session.review_stage == ReviewStage.VIDEO:
        await _handle_video_stage_action(update, action, job_id)
        return

    await _handle_metadata_stage_action(update, action, job_id)


def create_telegram_application() -> Application:
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .build()
    )
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("twitter", twitter_command))
    application.add_handler(CommandHandler("hugging_face", hugging_face_command))
    application.add_handler(CommandHandler("pixabay", pixabay_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    application.add_handler(CallbackQueryHandler(handle_callback))
    # #region agent log
    try:
        import json as _json, time as _time
        with open("debug-083327.log", "a", encoding="utf-8") as _f:
            _f.write(_json.dumps({"sessionId":"083327","hypothesisId":"H1","location":"bot.py:create_telegram_application","message":"bot_module_importable_app_built","data":{"handlers":len(application.handlers.get(0, []))},"timestamp":int(_time.time()*1000),"runId":"post-fix"}) + "\n")
    except Exception:
        pass
    # #endregion
    return application
