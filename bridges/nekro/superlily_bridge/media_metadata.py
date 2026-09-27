"""Retain bounded protocol image labels across Nekro's content conversion."""

import json
import re
from collections import defaultdict, deque


_FILE_HASH = re.compile(r"^([0-9a-fA-F]{32})\.[a-zA-Z0-9]+$")


def _key(data):
    value = data.get("file") or data.get("file_name") or ""
    match = _FILE_HASH.fullmatch(str(value))
    return match.group(1).lower() if match else None


def capture_image_metadata(message):
    images = []
    for segment in (message or [])[:64]:
        if not isinstance(segment, dict) or segment.get("type") != "image":
            continue
        data = segment.get("data") or {}
        key = _key(data)
        if key is None:
            continue
        metadata = {}
        for field in ("summary", "sub_type", "emoji_id", "emoji_package_id"):
            value = data.get(field)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                metadata[field] = value[:256] if isinstance(value, str) else value
        images.append([key, metadata])
    return {"images": json.dumps(images, ensure_ascii=False)} if images else {}


def enrich_image_segments(segments, captured):
    by_key = defaultdict(deque)
    for key, metadata in json.loads((captured or {}).get("images", "[]")):
        by_key[key].append(metadata)
    result = []
    for segment in segments:
        if segment.get("type") != "image":
            result.append(segment)
            continue
        data = segment.get("data", segment)
        matches = by_key.get(_key(data))
        if not matches:
            result.append(segment)
            continue
        enriched = {**data, **matches.popleft()}
        result.append({**segment, "data": enriched} if "data" in segment else enriched)
    return result
