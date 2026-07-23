"""Pixabay Video API search + lossless download of 9:16 Shorts HD/4K clips."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

PIXABAY_VIDEOS_URL = "https://pixabay.com/api/videos/"
CACHE_TTL_SECONDS = 24 * 60 * 60
MIN_DURATION_SECONDS = 1
MAX_DURATION_SECONDS = 60
# HD floor (720p). SD streams/videos are below this and are excluded.
MIN_SHORT_SIDE = 720
# YouTube Shorts target: width:height = 9:16  →  height/width = 16/9
TARGET_ASPECT_HW = 16 / 9
# Allow tiny codec/rounding drift (e.g. 1080x1918) but reject 3:4 / 4:5 / 2:3.
ASPECT_RATIO_TOLERANCE = 0.05
# 720p 9:16 short side → 720x1280
MIN_WIDTH = 720
MIN_HEIGHT = 1280
# Pixabay renditions: large≈4K, medium≈HD. Omit small/tiny (SD-tier).
STREAM_KEYS = ("large", "medium")
USER_AGENT = "YouTubeShortsUploader/1.0"


class PixabayError(Exception):
    pass


class PixabayExhaustedError(PixabayError):
    """No unused 9:16 HD/4K clips left for this phrase (within searched pages)."""


@dataclass(frozen=True)
class PixabayStream:
    url: str
    width: int
    height: int
    size: int


@dataclass(frozen=True)
class PixabayVideoResult:
    video_id: int
    page_url: str
    user: str
    duration: int
    phrase: str
    stream: PixabayStream
    local_path: Path

    @property
    def attribution(self) -> str:
        return (
            f"Pixabay video by {self.user}\n"
            f"{self.page_url}\n"
            f"{self.stream.width}x{self.stream.height}, {self.duration}s"
        )


# phrase -> (fetched_at, payload)
_search_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def find_and_download_video(
    phrase: str,
    output_dir: Path,
    job_id: str,
    *,
    used_ids: list[int] | None = None,
    max_pages: int = 5,
    filename: str | None = None,
) -> PixabayVideoResult:
    """Search Pixabay and download the next unused 9:16 HD/4K clip for ``phrase``."""
    if not settings.pixabay_api_key:
        raise PixabayError("PIXABAY_API_KEY is not configured.")

    cleaned_phrase = " ".join(phrase.strip().split())
    if not cleaned_phrase:
        raise PixabayError("Search phrase is empty.")

    used = set(used_ids or [])
    for page in range(1, max_pages + 1):
        payload = _search_videos(cleaned_phrase, page=page)
        hits = payload.get("hits")
        if not isinstance(hits, list) or not hits:
            continue

        for hit in hits:
            if not isinstance(hit, dict):
                continue
            try:
                video_id = int(hit["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if video_id in used:
                continue
            # Skip SD-only assets; keep HD and 4K.
            if _is_sd_only_video(hit):
                continue

            stream = _best_nine_sixteen_stream(hit)
            if stream is None:
                continue

            duration = _safe_int(hit.get("duration"), default=0)
            if not (MIN_DURATION_SECONDS <= duration <= MAX_DURATION_SECONDS):
                continue

            local_path = _download_stream(
                stream.url, output_dir, job_id, filename=filename
            )
            return PixabayVideoResult(
                video_id=video_id,
                page_url=str(hit.get("pageURL") or ""),
                user=str(hit.get("user") or "unknown"),
                duration=duration,
                phrase=cleaned_phrase,
                stream=stream,
                local_path=local_path,
            )

    raise PixabayExhaustedError(
        f"No unused 9:16 HD/4K Pixabay videos (≤{MAX_DURATION_SECONDS}s) "
        f"found for phrase: {cleaned_phrase!r}"
    )


def _search_videos(phrase: str, *, page: int) -> dict[str, Any]:
    cache_key = f"{phrase.casefold()}|page={page}|9x16|{MIN_WIDTH}x{MIN_HEIGHT}"
    cached = _search_cache.get(cache_key)
    now = time.time()
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    params = urllib.parse.urlencode(
        {
            "key": settings.pixabay_api_key,
            "q": phrase,
            "safesearch": "true",
            # Helps when supported; Videos API may ignore this — client still enforces 9:16.
            "orientation": "vertical",
            "order": "latest",
            "min_width": MIN_WIDTH,
            "min_height": MIN_HEIGHT,
            "per_page": 50,
            "page": page,
        }
    )
    url = f"{PIXABAY_VIDEOS_URL}?{params}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        raise PixabayError(f"Pixabay API HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise PixabayError(f"Pixabay API request failed: {exc}") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise PixabayError("Pixabay API returned invalid JSON.") from exc

    if not isinstance(payload, dict):
        raise PixabayError("Pixabay API JSON root must be an object.")

    _search_cache[cache_key] = (now, payload)
    return payload


def _is_nine_sixteen(width: int, height: int) -> bool:
    """True when dimensions match YouTube Shorts 9:16 (portrait) within tolerance."""
    if width <= 0 or height <= width:
        return False
    return abs((height / width) - TARGET_ASPECT_HW) <= ASPECT_RATIO_TOLERANCE


def _iter_stream_dicts(hit: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    videos = hit.get("videos")
    if not isinstance(videos, dict):
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for key in ("large", "medium", "small", "tiny"):
        raw = videos.get(key)
        if isinstance(raw, dict):
            out.append((key, raw))
    return out


def _max_available_short_side(hit: dict[str, Any]) -> int:
    """Highest short-side among Pixabay streams that have a real URL."""
    best = 0
    for _key, raw in _iter_stream_dicts(hit):
        url = str(raw.get("url") or "").strip()
        width = _safe_int(raw.get("width"))
        height = _safe_int(raw.get("height"))
        if not url or width <= 0 or height <= 0:
            continue
        best = max(best, min(width, height))
    return best


def _is_sd_only_video(hit: dict[str, Any]) -> bool:
    """True when the asset's best available rendition is below HD (Pixabay SD)."""
    return _max_available_short_side(hit) < MIN_SHORT_SIDE


def _best_nine_sixteen_stream(hit: dict[str, Any]) -> PixabayStream | None:
    """Pick the highest-res 9:16 HD/4K stream (prefers ``large``/4K when present)."""
    candidates: list[PixabayStream] = []
    videos = hit.get("videos")
    if not isinstance(videos, dict):
        return None

    for key in STREAM_KEYS:
        raw = videos.get(key)
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or "").strip()
        width = _safe_int(raw.get("width"))
        height = _safe_int(raw.get("height"))
        size = _safe_int(raw.get("size"))
        if not url or width <= 0 or height <= 0:
            continue
        if not _is_nine_sixteen(width, height):
            continue
        if min(width, height) < MIN_SHORT_SIDE:
            continue
        candidates.append(PixabayStream(url=url, width=width, height=height, size=size))

    if not candidates:
        return None

    # Prefer 4K (large) over HD (medium) by pixel count, then file size.
    return max(candidates, key=lambda s: (s.width * s.height, s.size))


def _download_stream(
    url: str, output_dir: Path, job_id: str, *, filename: str | None = None
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / (filename or f"{job_id}.mp4")
    if destination.exists():
        destination.unlink()

    download_url = url
    if "download=" not in url:
        sep = "&" if "?" in url else "?"
        download_url = f"{url}{sep}download=1"

    request = urllib.request.Request(download_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        raise PixabayError(f"Pixabay download HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise PixabayError(f"Pixabay download failed: {exc}") from exc

    if not data:
        raise PixabayError("Pixabay download returned an empty file.")

    destination.write_bytes(data)
    logger.info("Downloaded Pixabay video to %s (%s bytes)", destination, len(data))
    return destination


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
