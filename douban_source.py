"""Identity-checked, read-only resolution of Shenyi Assistant's Douban cache."""

import json
import rating_policy
import re
import time
from pathlib import Path
from typing import Any

_SCORE = re.compile(r"(?:10(?:\.0+)?|[1-9](?:\.[0-9]+)?)")
_DOUBAN_ID = re.compile(r"[0-9]+")
_IMDB_ID = re.compile(r"tt[0-9]+", re.IGNORECASE)


def _id(ids: Any, name: str) -> str | None:
    if not isinstance(ids, dict):
        return None
    return next((str(value).strip() for key, value in ids.items()
                 if str(key).casefold() == name and value), None)


def _score(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, bool) or value is None or not _SCORE.fullmatch(str(value).strip()):
        return None
    score = float(value)
    return f"{score:.1f}" if 0 < score <= 10 else None


def _candidates(
    directory: Path,
    douban_id: str | None,
    imdb_id: str | None,
    item_type: str | None = None,
) -> list[Path]:
    """Return cache candidates for both Movie and Shenyi Series layouts.

    Shenyi Assistant stores movie payloads as ``all.json`` but stores TV
    payloads as ``series.json`` (with an optional ``seasons.json`` beside it).
    The old resolver only searched ``all.json``, which made every populated
    ``douban-tv`` directory look like a cache miss.
    """
    filename = "series.json" if item_type in {"Series", "Season"} else "all.json"
    candidates: list[Path] = []
    if douban_id:
        if imdb_id:
            candidates.append(directory / f"{douban_id}_{imdb_id}" / filename)
        candidates.append(directory / douban_id / filename)
        if item_type not in {"Series", "Season"}:
            candidates.append(directory / f"{douban_id}.json")
        candidates.extend(sorted(directory.glob(f"{douban_id}_tt*/{filename}")))
    elif imdb_id:
        candidates.extend(sorted(directory.glob(f"*_{imdb_id}/{filename}")))
    return list(dict.fromkeys(candidates))


def resolve(root: Path | None, item: dict[str, Any]) -> dict[str, Any]:
    """Never infer a rating from title or from Emby's generic community score.

    The JSON's Douban ID and directory ID must agree.  An available IMDb ID
    must also agree with the cache directory, when that directory encodes one.
    Unique IMDb-only cache matches may safely recover a missing Emby Douban ID.
    """
    ids = item.get("ProviderIds") or {}
    douban_id, imdb_id = _id(ids, "douban"), _id(ids, "imdb")
    if douban_id and not _DOUBAN_ID.fullmatch(douban_id):
        return {"status": "invalid_douban_id"}
    if imdb_id and not _IMDB_ID.fullmatch(imdb_id):
        return {"status": "invalid_imdb_id"}
    if not douban_id and not imdb_id:
        return {"status": "missing_ids"}
    if root is None or not root.is_dir():
        return {"status": "cache_unavailable"}
    # Official Shenyi cache can live directly under the metadata root's
    # cache folder; retain the older category-specific layout as well.
    category = "douban-tv" if item.get("Type") in {"Series", "Season"} else "douban-movies"
    dirs: list[Path] = []
    for base in (root, root / "cache"):
        dirs.extend([base, base / category])
    dirs = list(dict.fromkeys(dirs))
    try:
        paths = list(dict.fromkeys(path for directory in dirs for path in _candidates(directory, douban_id, imdb_id, item.get("Type"))))
    except OSError:
        return {"status": "cache_unavailable"}
    if not paths or not any(path.is_file() for path in paths):
        # Shenyi uses 0_tt... as a negative lookup marker, not a rating.
        if imdb_id and any((directory / f"0_{imdb_id}").is_dir() for directory in dirs):
            return {"status": "negative_placeholder"}
        return {"status": "missing_cache_file"}
    valid: list[dict[str, Any]] = []
    failures: set[str] = set()
    mismatch_metadata: dict[str, Any] = {}
    for path in paths:
        if not path.is_file():
            continue
        parent = path.parent.name if path.name in {"all.json", "series.json", "seasons.json"} else path.stem
        match = re.fullmatch(r"([0-9]+)(?:_(tt[0-9]+))?", parent, re.IGNORECASE)
        if not match:
            failures.add("invalid_cache_path")
            continue
        filename_douban, filename_imdb = match.group(1), match.group(2)
        if douban_id and filename_douban != douban_id:
            failures.add("douban_id_mismatch")
            continue
        if imdb_id and filename_imdb and filename_imdb.casefold() != imdb_id.casefold():
            failures.add("imdb_id_mismatch")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            failures.add("invalid_json")
            continue
        if not isinstance(payload, dict):
            failures.add("invalid_json")
            continue
        embedded_douban = str(payload.get("id") or "").strip()
        # Official Shenyi cache names are themselves the lookup table. When
        # the JSON omits its own Douban id, accept only a fully-qualified
        # <douban_id>_<imdb_id> directory that matches the current IMDb id.
        if embedded_douban and embedded_douban != filename_douban:
            failures.add("douban_id_mismatch")
            continue
        if not embedded_douban and not filename_imdb:
            failures.add("douban_id_mismatch")
            continue
        embedded_imdb = payload.get("imdb_id") or payload.get("imdbId")
        if imdb_id and embedded_imdb and str(embedded_imdb).casefold() != imdb_id.casefold():
            failures.add("imdb_id_mismatch")
            continue
        review = rating_policy.cache_identity_review(item, payload)
        if review["blocked"]:
            failures.add("media_type_mismatch" if "media_type_mismatch" in review["warnings"] else "identity_mismatch")
            mismatch_metadata = {"cache_title": payload.get("title"), "cache_year": payload.get("year"),
                                 "identity_warnings": review["warnings"], "douban_id": filename_douban}
            continue
        score = _score(payload.get("rating"))
        if not score:
            failures.add("json_without_rating")
            mismatch_metadata = {"path": str(path), "douban_id": filename_douban,
                                 "imdb_id": imdb_id or filename_imdb, "cache_title": payload.get("title"),
                                 "cache_year": payload.get("year"), "identity_warnings": review["warnings"],
                                 "cache_updated_at": path.stat().st_mtime,
                                 "cache_age_days": round(max(0, time.time()-path.stat().st_mtime)/86400, 1),
                                 "rating_value": (payload.get("rating") or {}).get("value") if isinstance(payload.get("rating"),dict) else payload.get("rating"),
                                 "rating_count": (payload.get("rating") or {}).get("count") if isinstance(payload.get("rating"),dict) else None}
            continue
        try:
            age = round(max(0, time.time() - path.stat().st_mtime) / 86400, 1)
        except OSError:
            age = None
        valid.append({"status": "ok", "value": score, "douban_id": filename_douban,
                      "imdb_id": imdb_id or filename_imdb, "path": str(path), "cache_age_days": age,
                      "match": "douban_id" if douban_id else "imdb_id",
                      "cache_updated_at": path.stat().st_mtime, "cache_title": payload.get("title"),
                      "cache_year": payload.get("year"), "identity_warnings": review["warnings"]})
    if valid:
        if len({(row["douban_id"], row["value"]) for row in valid}) != 1:
            return {"status": "cache_conflict"}
        # If a current IMDb ID is absent, disagreeing directory IMDb IDs are
        # ambiguous even when the Douban ID and score happen to agree.
        if not imdb_id and len({row["imdb_id"] for row in valid if row["imdb_id"]}) > 1:
            return {"status": "cache_conflict"}
        return max(valid, key=lambda row: row.get("cache_updated_at", 0))
    for status in ("identity_mismatch", "media_type_mismatch", "imdb_id_mismatch", "douban_id_mismatch", "invalid_json",
                   "json_without_rating", "invalid_cache_path"):
        if status in failures:
            return {"status": status, **mismatch_metadata}
    return {"status": "missing_cache_file"}


