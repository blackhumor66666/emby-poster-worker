"""Strict source-specific normalization and identity checks (no network)."""
import math
import re

TRANSIENT_STATUSES = {"lookup_error", "http_error", "invalid_payload", "network_error", "rate_limited", "circuit_open", "invalid_api_key", "no_api_key", "not_configured"}

def decimal_score(value, maximum=10):
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(?:\s*/\s*10)?", text)
    if not match:
        return None
    number = float(match[1])
    return number if math.isfinite(number) and 0 < number <= maximum else None

def critic_percent(value, explicit_percent=False):
    if value is None or isinstance(value, bool):
        return None
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(%)?", str(value).strip())
    if not match:
        return None
    score = float(match[1])
    if not 0 <= score <= 100:
        return None
    # MDBList's dedicated tomatoes endpoint returns integer percentages.
    # Reject ambiguous bare decimal 0-10 values rather than multiplying them.
    if score <= 10 and not match[2] and not explicit_percent and not score.is_integer():
        return None
    return f"{round(score):d}%"

def normalized_title(value):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", str(value or "").casefold())

def cache_identity_review(item, payload):
    problems = []
    item_type = item.get("Type")
    if isinstance(payload.get("is_tv"), bool):
        if (item_type == "Movie" and payload["is_tv"]) or (item_type in {"Series", "Season"} and not payload["is_tv"]):
            return {"blocked": True, "warnings": ["media_type_mismatch"]}
    try:
        gap = abs(int(item.get("ProductionYear")) - int(payload.get("year")))
    except (ValueError, TypeError):
        gap = 0
    names = {normalized_title(item.get(key)) for key in ("Name", "OriginalTitle")}
    names.discard("")
    title = normalized_title(payload.get("title"))
    agrees = not names or not title or title in names
    if not agrees:
        problems.append("title_difference")
    if gap:
        problems.append("year_difference")
    # Distribution dates may differ by one/two years; never block on those.
    # A different title AND >=3-year gap is strong evidence of season/remake mixup.
    blocked = item_type != "Season" and gap >= 3 and not agrees
    return {"blocked": blocked, "warnings": problems}
