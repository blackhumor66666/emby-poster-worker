"""Read-only accuracy report and offline ID suggestions; never changes Emby."""
import json
from collections import Counter, defaultdict
from pathlib import Path
import rating_policy
import media_match

def cache_index(root):
    index = defaultdict(list)
    if root is None or not Path(root).is_dir():
        return index
    for base in (Path(root), Path(root) / "cache"):
        for category, filename, kind in (("douban-tv", "series.json", "Series"), ("douban-movies", "all.json", "Movie")):
            directory = base / category
            if not directory.is_dir():
                continue
            for path in directory.glob(f"*/{filename}"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8-sig"))
                    did = str(data.get("id") or "")
                    # A cached item may be proposed only if its embedded id and directory agree.
                    parts = path.parent.name.split("_", 1)
                    if not did.isdigit() or parts[0] != did:
                        continue
                    key = (kind, rating_policy.normalized_title(data.get("title")), str(data.get("year") or ""))
                    row = {"douban_id": did, "imdb_id": parts[1] if len(parts) > 1 else None,
                           "title": data.get("title"), "year": data.get("year")}
                    if row not in index[key]:
                        index[key].append(row)
                except (OSError, ValueError, TypeError, AttributeError):
                    continue
    return index

def build_report(items, states, resolve, root, generated_at, stale_days=90, overrides=None):
    index = cache_index(root)
    counts = Counter()
    source_counts = {name: Counter() for name in ("豆瓣", "IMDb", "RT")}
    findings = []
    for item in items:
        iid = str(item.get("Id") or "")
        state = states(iid)
        scores = state.get("ratings") or {}
        details = state.get("rating_details") or {}
        result = resolve(item)
        flags = []
        for name in source_counts:
            source_counts[name]["has_value"] += bool(scores.get(name))
            source_counts[name]["missing"] += not bool(scores.get(name))
            info = details.get(name) or {}
            source_counts[name][str(info.get("status") or "unknown")] += 1
            if info.get("status") == "ok" and not scores.get(name):
                flags.append("legacy_success_without_score")
        counts["total"] += 1
        if result.get("status") in {"identity_mismatch", "media_type_mismatch", "imdb_id_mismatch", "douban_id_mismatch", "cache_conflict"}:
            flags.append("identity_conflict")
        if result.get("identity_warnings"):
            flags.append("identity_review")
        if (result.get("cache_age_days") or 0) > stale_days:
            flags.append("old_cache")
        if details.get("豆瓣", {}).get("source") == "rating_overrides":
            flags.append("manual_fixed_score")
        binding=overrides(item) if overrides else {}
        ids=media_match.effective_ids(item,binding)
        flags.extend(media_match.id_gaps(item,binding))
        suggestions = []
        if not ids.get("douban") or not ids.get("imdb"):
            flags.append("missing_external_id")
            for title in (item.get("Name"), item.get("OriginalTitle")):
                key = (item.get("Type"), rating_policy.normalized_title(title), str(item.get("ProductionYear") or ""))
                for row in index.get(key, []):
                    # Never suggest a different IMDb id for an already identified item.
                    if ids.get("imdb") and row.get("imdb_id") and ids["imdb"].casefold() != row["imdb_id"].casefold():
                        continue
                    if row not in suggestions:
                        suggestions.append(row)
            if suggestions:
                flags.append("offline_id_candidate")
        if result.get("status") != "ok" and not scores.get("豆瓣"):
            flags.append("douban_unresolved")
            if not media_match.id_gaps(item,binding):flags.append("ids_without_score")
        for flag in set(flags):
            counts[flag] += 1
        if flags:
            findings.append({"id": iid, "name": item.get("Name"), "year": item.get("ProductionYear"),
                             "type": item.get("Type"), "provider_ids": item.get("ProviderIds") or {},
                             "flags": sorted(set(flags)), "reason": result.get("status"),
                             "cache_title": result.get("cache_title"), "cache_year": result.get("cache_year"),
                             "cache_age_days": result.get("cache_age_days"), "suggestions": suggestions})
    return {"generated_at": generated_at, "counts": dict(counts),
            "source_coverage": {k: dict(v) for k, v in source_counts.items()},
            "findings": findings, "automatic_id_changes": 0,
            "note": "候选来自本地缓存的同名同年匹配，仍需人工确认；缺失不等于平台无评分。"}
