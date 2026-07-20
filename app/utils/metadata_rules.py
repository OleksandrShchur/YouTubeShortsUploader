import json
import re

from app.schemas import ShortsMetadata

YOUTUBE_TITLE_MAX = 100
TWITTER_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:twitter\.com|x\.com)/\w+/status/\d+",
    re.IGNORECASE,
)


def is_twitter_url(text: str) -> bool:
    return bool(TWITTER_URL_PATTERN.search(text.strip()))


def extract_twitter_url(text: str) -> str | None:
    match = TWITTER_URL_PATTERN.search(text.strip())
    return match.group(0) if match else None


def build_display_title(metadata: ShortsMetadata) -> str:
    """Build title with viral tags appended, respecting YouTube 100-char limit."""
    base_title = metadata.title.strip()
    tags = [tag.lstrip("#") for tag in metadata.viral_title_tags]
    tag_suffix = " ".join(f"#{tag}" for tag in tags)

    if not tag_suffix:
        return base_title[:YOUTUBE_TITLE_MAX]

    combined = f"{base_title} {tag_suffix}".strip()
    if len(combined) <= YOUTUBE_TITLE_MAX:
        return combined

    available = YOUTUBE_TITLE_MAX - len(tag_suffix) - 1
    if available < 1:
        return base_title[:YOUTUBE_TITLE_MAX]
    return f"{base_title[:available].rstrip()} {tag_suffix}"


def normalize_metadata(raw: dict) -> ShortsMetadata:
    metadata = ShortsMetadata.model_validate(raw)
    display_title = build_display_title(metadata)
    if len(display_title) > YOUTUBE_TITLE_MAX:
        raise ValueError(
            f"Title with viral tags exceeds {YOUTUBE_TITLE_MAX} characters."
        )
    return metadata


def metadata_to_json(metadata: ShortsMetadata) -> str:
    payload = {
        "title": metadata.title,
        "description": metadata.description,
        "viral_title_tags": metadata.viral_title_tags,
        "shorts_tags": metadata.shorts_tags,
        "display_title": build_display_title(metadata),
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def parse_modified_json(text: str) -> ShortsMetadata:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid JSON. Please send valid JSON only.") from exc

    if not isinstance(data, dict):
        raise ValueError("JSON root must be an object.")

    allowed_keys = {"title", "description", "viral_title_tags", "shorts_tags"}
    filtered = {key: data[key] for key in allowed_keys if key in data}
    if len(filtered) < 4:
        missing = allowed_keys - set(filtered.keys())
        raise ValueError(f"Missing required fields: {', '.join(sorted(missing))}")

    return normalize_metadata(filtered)
