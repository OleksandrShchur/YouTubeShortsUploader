"""Unofficial Pixabay Music search + download via HTML/CDN scrape.

Pixabay has no public Music API. This client scrapes search/track pages and
downloads CDN MP3s. It may break if Pixabay or Cloudflare changes.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from app.services.ffmpeg_utils import FFmpegError, probe_duration_seconds

logger = logging.getLogger(__name__)

PIXABAY_ORIGIN = "https://pixabay.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
TRACK_PATH_RE = re.compile(r"/music/[a-z0-9\-]+-(\d+)/", re.IGNORECASE)
ISO_DURATION_RE = re.compile(
    r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$",
    re.IGNORECASE,
)
FETCH_RETRIES = 3
FETCH_RETRY_SLEEP_SECONDS = 1.5


class PixabayAudioError(Exception):
    pass


class PixabayAudioExhaustedError(PixabayAudioError):
    """No unused music track long enough for this phrase (within searched pages)."""


@dataclass(frozen=True)
class PixabayAudioResult:
    audio_id: int
    page_url: str
    user: str
    duration: float
    phrase: str
    download_url: str
    local_path: Path
    name: str

    @property
    def attribution(self) -> str:
        label = self.name or f"track {self.audio_id}"
        return (
            f"Pixabay music: {label} by {self.user}\n"
            f"{self.page_url}\n"
            f"duration {self.duration:.1f}s"
        )


def find_and_download_audio(
    phrase: str,
    output_dir: Path,
    job_id: str,
    *,
    min_duration_seconds: float,
    used_ids: list[int] | None = None,
    max_pages: int = 3,
    filename: str | None = None,
) -> PixabayAudioResult:
    """Search Pixabay Music and download the next unused track long enough for the video."""
    cleaned_phrase = " ".join(phrase.strip().split())
    if not cleaned_phrase:
        raise PixabayAudioError("Search phrase is empty.")
    if min_duration_seconds <= 0:
        raise PixabayAudioError("min_duration_seconds must be positive.")

    used = set(used_ids or [])
    seen_ids: set[int] = set()

    for page in range(1, max_pages + 1):
        html = _fetch_search_html(cleaned_phrase, page=page)
        candidates = _extract_track_paths(html)
        if not candidates:
            continue

        for audio_id, track_path in candidates:
            if audio_id in used or audio_id in seen_ids:
                continue
            seen_ids.add(audio_id)

            page_url = f"{PIXABAY_ORIGIN}{track_path}"
            try:
                track_html = _fetch_html(page_url)
                meta = _parse_track_json_ld(track_html, audio_id=audio_id, page_url=page_url)
            except PixabayAudioError as exc:
                logger.info("Skipping Pixabay music %s: %s", audio_id, exc)
                continue

            if meta.duration + 0.05 < min_duration_seconds:
                logger.info(
                    "Skipping Pixabay music %s: duration %.2fs < need %.2fs",
                    audio_id,
                    meta.duration,
                    min_duration_seconds,
                )
                continue

            local_path = _download_audio(
                meta.download_url,
                output_dir,
                job_id,
                referer=page_url,
                filename=filename,
            )
            try:
                probed = probe_duration_seconds(local_path)
            except FFmpegError as exc:
                # Missing ffmpeg is fatal; corrupt/single-file probe issues skip the track.
                message = str(exc).lower()
                if "not installed" in message or "not on path" in message:
                    local_path.unlink(missing_ok=True)
                    raise PixabayAudioError(str(exc)) from exc
                local_path.unlink(missing_ok=True)
                logger.info("Skipping Pixabay music %s: ffprobe failed: %s", audio_id, exc)
                continue
            if probed + 0.05 < min_duration_seconds:
                local_path.unlink(missing_ok=True)
                logger.info(
                    "Skipping Pixabay music %s after probe: %.2fs < need %.2fs",
                    audio_id,
                    probed,
                    min_duration_seconds,
                )
                continue

            return PixabayAudioResult(
                audio_id=audio_id,
                page_url=page_url,
                user=meta.user,
                duration=probed,
                phrase=cleaned_phrase,
                download_url=meta.download_url,
                local_path=local_path,
                name=meta.name,
            )

    raise PixabayAudioExhaustedError(
        f"No unused Pixabay music (≥{min_duration_seconds:.1f}s) "
        f"found for phrase: {cleaned_phrase!r}"
    )


@dataclass(frozen=True)
class _TrackMeta:
    audio_id: int
    page_url: str
    download_url: str
    duration: float
    user: str
    name: str


def _fetch_search_html(phrase: str, *, page: int) -> str:
    encoded = urllib.parse.quote(phrase)
    if page <= 1:
        url = f"{PIXABAY_ORIGIN}/music/search/{encoded}/"
    else:
        url = f"{PIXABAY_ORIGIN}/music/search/{encoded}/?pagi={page}"
    return _fetch_html(url)


def _fetch_html(url: str) -> str:
    last_error: Exception | None = None
    for attempt in range(1, FETCH_RETRIES + 1):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": f"{PIXABAY_ORIGIN}/music/",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
            last_error = PixabayAudioError(
                f"Pixabay music HTTP {exc.code} for {url}: {detail[:200]}"
            )
            if exc.code in {403, 429, 503} and attempt < FETCH_RETRIES:
                time.sleep(FETCH_RETRY_SLEEP_SECONDS * attempt)
                continue
            raise last_error from exc
        except urllib.error.URLError as exc:
            last_error = PixabayAudioError(f"Pixabay music request failed for {url}: {exc}")
            if attempt < FETCH_RETRIES:
                time.sleep(FETCH_RETRY_SLEEP_SECONDS * attempt)
                continue
            raise last_error from exc

    raise last_error or PixabayAudioError(f"Pixabay music request failed for {url}")


def _extract_track_paths(html: str) -> list[tuple[int, str]]:
    """Return ordered unique (audio_id, absolute path) pairs from search HTML."""
    out: list[tuple[int, str]] = []
    seen: set[int] = set()
    for match in TRACK_PATH_RE.finditer(html):
        path = match.group(0)
        audio_id = int(match.group(1))
        if audio_id in seen:
            continue
        seen.add(audio_id)
        out.append((audio_id, path))
    return out


def _parse_track_json_ld(
    html: str, *, audio_id: int, page_url: str
) -> _TrackMeta:
    scripts = re.findall(
        r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for raw in scripts:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        content_url = str(data.get("contentUrl") or "").strip()
        if not content_url:
            continue
        if "cdn.pixabay.com" not in content_url and ".mp3" not in content_url.lower():
            continue

        duration = _parse_iso_duration(str(data.get("duration") or ""))
        if duration <= 0:
            # Fall back later via ffprobe after download; use a large placeholder skip check.
            duration = 0.0

        creator = data.get("creator")
        user = "unknown"
        if isinstance(creator, dict):
            user = str(creator.get("name") or "unknown").strip() or "unknown"
        elif isinstance(creator, str) and creator.strip():
            user = creator.strip()

        name = str(data.get("name") or data.get("caption") or "").strip()
        name = re.sub(r"\s*\|\s*Royalty-free Music\s*$", "", name, flags=re.I).strip()

        return _TrackMeta(
            audio_id=audio_id,
            page_url=page_url,
            download_url=content_url,
            duration=duration,
            user=user,
            name=name or f"track {audio_id}",
        )

    # Fallback: raw CDN mp3 URL in HTML
    mp3_matches = re.findall(
        r"https?://cdn\.pixabay\.com/download/audio/[^\s\"']+\.mp3[^\s\"']*",
        html,
        flags=re.IGNORECASE,
    )
    if mp3_matches:
        return _TrackMeta(
            audio_id=audio_id,
            page_url=page_url,
            download_url=mp3_matches[0],
            duration=0.0,
            user="unknown",
            name=f"track {audio_id}",
        )

    raise PixabayAudioError(f"No audio download URL found on track page {page_url}")


def _parse_iso_duration(value: str) -> float:
    cleaned = value.strip()
    if not cleaned:
        return 0.0
    match = ISO_DURATION_RE.match(cleaned)
    if not match:
        return 0.0
    hours = float(match.group(1) or 0)
    minutes = float(match.group(2) or 0)
    seconds = float(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def _download_audio(
    url: str,
    output_dir: Path,
    job_id: str,
    *,
    referer: str,
    filename: str | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / (filename or f"{job_id}_audio.mp3")
    if destination.exists():
        destination.unlink()

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": referer,
            "Accept": "*/*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        raise PixabayAudioError(f"Pixabay audio download HTTP {exc.code}: {detail[:200]}") from exc
    except urllib.error.URLError as exc:
        raise PixabayAudioError(f"Pixabay audio download failed: {exc}") from exc

    if not data:
        raise PixabayAudioError("Pixabay audio download returned an empty file.")

    destination.write_bytes(data)
    logger.info("Downloaded Pixabay audio to %s (%s bytes)", destination, len(data))
    return destination
