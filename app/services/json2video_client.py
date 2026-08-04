"""JSON2Video REST client: create movie, poll until done, download MP4."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

API_BASE = "https://api.json2video.com/v2/movies"
USER_AGENT = "YouTubeShortsUploader/1.0"
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 600


class Json2VideoError(Exception):
    pass


def _require_api_key() -> str:
    key = (settings.json2video_api_key or "").strip()
    if not key:
        raise Json2VideoError(
            "JSON2VIDEO_API_KEY is not configured. Get a free key at "
            "https://json2video.com/get-api-key/ and add it to .env."
        )
    return key


def _request(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    api_key = _require_api_key()
    data = None
    headers = {
        "x-api-key": api_key,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        raise Json2VideoError(
            f"JSON2Video HTTP {exc.code}: {detail or exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise Json2VideoError(f"JSON2Video network error: {exc.reason}") from exc

    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Json2VideoError("JSON2Video returned non-JSON response.") from exc
    if not isinstance(payload, dict):
        raise Json2VideoError("JSON2Video response root must be an object.")
    return payload


def create_movie(payload: dict[str, Any]) -> str:
    """Submit a movie JSON and return the project id."""
    response = _request("POST", API_BASE, body=payload)
    if response.get("success") is False:
        raise Json2VideoError(
            f"JSON2Video create failed: {response.get('message') or response}"
        )
    project = response.get("project") or response.get("projectId") or response.get("id")
    if not project:
        # Some responses nest under movie
        movie = response.get("movie")
        if isinstance(movie, dict):
            project = movie.get("project") or movie.get("id")
    if not project:
        raise Json2VideoError(f"JSON2Video create response missing project id: {response}")
    return str(project)


def get_movie(project_id: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"project": project_id})
    response = _request("GET", f"{API_BASE}?{query}", timeout=60)
    if response.get("success") is False:
        raise Json2VideoError(
            f"JSON2Video status failed: {response.get('message') or response}"
        )
    return response


def wait_for_movie(
    project_id: str,
    *,
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    timeout_seconds: float = POLL_TIMEOUT_SECONDS,
) -> str:
    """Poll until the movie is done and return the CDN URL."""
    started = time.monotonic()
    while True:
        response = get_movie(project_id)
        movie = response.get("movie") if isinstance(response.get("movie"), dict) else response
        status = str(movie.get("status") or response.get("status") or "").lower()
        if status in {"done", "success"}:
            url = movie.get("url") or response.get("url")
            if not url:
                raise Json2VideoError(
                    f"JSON2Video finished without a URL: {response}"
                )
            return str(url)
        if status in {"error", "failed"}:
            message = movie.get("message") or response.get("message") or response
            raise Json2VideoError(f"JSON2Video render failed: {message}")
        if time.monotonic() - started >= timeout_seconds:
            raise Json2VideoError(
                f"Timed out waiting for JSON2Video project {project_id} "
                f"(last status={status or 'unknown'})."
            )
        logger.info(
            "JSON2Video project %s status=%s; waiting %.0fs",
            project_id,
            status or "unknown",
            poll_interval_seconds,
        )
        time.sleep(poll_interval_seconds)


def download_movie(url: str, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        raise Json2VideoError(f"Failed to download render ({exc.code}): {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise Json2VideoError(f"Failed to download render: {exc.reason}") from exc

    if not data:
        raise Json2VideoError("Downloaded movie was empty.")
    output_path.write_bytes(data)
    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise Json2VideoError(f"Movie was not written: {output_path}")
    return output_path
