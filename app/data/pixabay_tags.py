"""Lazy-loaded Pixabay search tags from ``pixabay_tags.txt`` (appendable)."""

from __future__ import annotations

import random
from pathlib import Path
from random import Random

_TAGS_PATH = Path(__file__).with_name("pixabay_tags.txt")
_cached_tags: list[str] | None = None


def _parse_tags(raw: str) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        tag = line.strip()
        if not tag or tag.startswith("#"):
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
    return tags


def load_pixabay_search_tags(*, force_reload: bool = False) -> list[str]:
    """Return search tags, reading ``pixabay_tags.txt`` only on first use (or reload)."""
    global _cached_tags
    if _cached_tags is not None and not force_reload:
        return list(_cached_tags)

    if not _TAGS_PATH.is_file():
        raise FileNotFoundError(f"Pixabay tags file missing: {_TAGS_PATH}")

    tags = _parse_tags(_TAGS_PATH.read_text(encoding="utf-8"))
    if not tags:
        raise ValueError(
            f"Pixabay tags file is empty; add one tag per line to {_TAGS_PATH.name}."
        )

    _cached_tags = tags
    return list(tags)


def append_pixabay_search_tag(tag: str) -> bool:
    """Append ``tag`` to the library file if new. Returns True when a line was written."""
    cleaned = " ".join(tag.strip().split())
    if not cleaned:
        raise ValueError("Tag must be non-empty.")

    existing = {t.casefold() for t in load_pixabay_search_tags()}
    if cleaned.casefold() in existing:
        return False

    prior = _TAGS_PATH.read_text(encoding="utf-8") if _TAGS_PATH.is_file() else ""
    with _TAGS_PATH.open("a", encoding="utf-8") as handle:
        if prior and not prior.endswith("\n"):
            handle.write("\n")
        handle.write(cleaned)
        handle.write("\n")

    load_pixabay_search_tags(force_reload=True)
    return True


def pick_pixabay_search_query(rng: Random | None = None) -> str:
    """Return a Pixabay ``q`` string from exactly 3 randomly sampled tags."""
    tags = load_pixabay_search_tags()
    picker = rng if rng is not None else random
    count = min(3, len(tags))
    selected = picker.sample(tags, k=count)
    return " ".join(selected)
