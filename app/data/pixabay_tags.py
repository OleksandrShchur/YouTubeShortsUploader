"""Predefined Pixabay search tags. Expand PIXABAY_SEARCH_TAGS as needed."""

from __future__ import annotations

import random
from random import Random

# Placeholder library — populate with real ambient / mood search tags later.
PIXABAY_SEARCH_TAGS: list[str] = [
    "cozy rain",
    "misty forest",
    "fireplace",
    "soft candlelight",
    "ocean waves",
    "night city window",
]


def pick_pixabay_search_query(rng: Random | None = None) -> str:
    """Return a Pixabay ``q`` string from 3–4 randomly sampled tags."""
    tags = [tag.strip() for tag in PIXABAY_SEARCH_TAGS if tag and tag.strip()]
    if not tags:
        raise ValueError("PIXABAY_SEARCH_TAGS is empty; add search tags before using /pixabay.")

    picker = rng if rng is not None else random
    if len(tags) >= 4:
        count = picker.choice((3, 4))
    else:
        count = len(tags)

    selected = picker.sample(tags, k=count)
    return " ".join(selected)
