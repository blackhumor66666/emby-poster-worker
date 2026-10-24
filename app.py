import base64
import email
import gzip
import hashlib
import hmac
import io
import json
import logging
import os
import re
import threading
import time
import secrets
import tarfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from http.cookies import SimpleCookie
from urllib.parse import parse_qsl, parse_qs, quote, unquote, urlparse

import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter

import douban_source
import rating_policy
import rating_review
import media_match
import search_sources
import issue_policy
import render_queue
import rendering  # noqa: E402
from storage import atomic_write_text, load_json_file as _load_json_file, read_report as _read_report  # noqa: E402
import admin_snapshot
import boxset_covers


class BeijingFormatter(logging.Formatter):
    """Render application timestamps in the configured troubleshooting zone."""

    def __init__(self, fmt: str, timezone_name: str) -> None:
        super().__init__(fmt)
        self.timezone_name = timezone_name
        try:
            self.zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            # Windows development hosts may not ship the IANA tz database.
            # Beijing and Singapore share a fixed UTC+8 offset, so retain the
            # requested troubleshooting timezone even when ZoneInfo is absent.
            if timezone_name in {"Asia/Shanghai", "Asia/Singapore", "Etc/GMT-8"}:
                self.zone = timezone(timedelta(hours=8), name="UTC+08:00")
            else:
                self.zone = timezone.utc
                self.timezone_name = "UTC"

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        current = datetime.fromtimestamp(record.created, tz=self.zone)
        if datefmt:
            return current.strftime(datefmt)
        return current.strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]


def env(name: str, default: str = "") -> str:
    value = os.environ.get(name, default).strip()
    # The initial local config used the Chinese placeholder "可选". Treat it
    # as empty instead of sending it as an API key.
    return "" if value in {"可选", "optional", "replace-with-an-emby-api-key"} else value


def split_api_keys(value: str) -> list[str]:
    """Parse a comma/newline/semicolon separated API-key list safely."""
    return list(dict.fromkeys(
        token.strip()
        for token in re.split(r"[,;\r\n]+", value or "")
        if token.strip() and token.strip() not in {"可选", "optional"}
    ))


LOG_TIMEZONE = os.environ.get("LOG_TIMEZONE", "Asia/Shanghai").strip() or "Asia/Shanghai"
LOG_HTTP_REQUESTS = os.environ.get("LOG_HTTP_REQUESTS", "false").strip().lower() in {"1", "true", "yes"}
LOG_ITEM_UPDATES = os.environ.get("LOG_ITEM_UPDATES", "true").strip().lower() in {"1", "true", "yes"}
SCAN_PROGRESS_EVERY = max(1, int(os.environ.get("SCAN_PROGRESS_EVERY", "50")))
_handler = logging.StreamHandler()
_handler.setFormatter(BeijingFormatter("%(asctime)s %(levelname)s %(message)s", LOG_TIMEZONE))
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
LOG = logging.getLogger("emby-poster-worker")


EMBY_URL = env("EMBY_URL").rstrip("/")
EMBY_API_KEY = env("EMBY_API_KEY")
OMDB_API_KEYS = split_api_keys(env("OMDB_API_KEYS", env("OMDB_API_KEY")))
OMDB_API_KEY = OMDB_API_KEYS[0] if OMDB_API_KEYS else ""
MDBLIST_API_KEYS = split_api_keys(env("MDBLIST_API_KEYS", env("MDBLIST_API_KEY")))
MDBLIST_API_BASE = env("MDBLIST_API_BASE", "https://api.mdblist.com").rstrip("/")
MDBLIST_CACHE_DAYS = max(1, int(env("MDBLIST_CACHE_DAYS", "7")))
MDBLIST_ERROR_CACHE_MINUTES = max(5, int(env("MDBLIST_ERROR_CACHE_MINUTES", "60")))
MDBLIST_MIN_INTERVAL_SECONDS = max(0.0, float(os.environ.get("MDBLIST_MIN_INTERVAL_MS", "250")) / 1000)
MDBLIST_RETRY_COUNT = max(0, int(os.environ.get("MDBLIST_RETRY_COUNT", "2")))
MDBLIST_BACKOFF_SECONDS = max(0.1, float(os.environ.get("MDBLIST_BACKOFF_SECONDS", "2")))
RATING_EXTERNAL_PROVIDER_ORDER = tuple(
    value.strip().lower()
    for value in os.environ.get("RATING_EXTERNAL_PROVIDER_ORDER", "mdblist,omdb").split(",")
    if value.strip().lower() in {"mdblist", "omdb"}
) or ("mdblist", "omdb")
CACHE_DIR = Path(env("CACHE_DIR", "/data/cache"))
CACHE_GC_ENABLED = os.environ.get("CACHE_GC_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
CACHE_GC_DAYS = max(1, int(os.environ.get("CACHE_GC_DAYS", "30")))
SCAN_REPORT_FILE = Path(os.environ.get("SCAN_REPORT_FILE", str(CACHE_DIR / "reports" / "last-scan.json")))
REVIEW_REPORT_FILE = Path(os.environ.get("REVIEW_REPORT_FILE", str(CACHE_DIR / "reports" / "review-needed.json")))
# 合集封面补齐：Emby 建 BoxSet 时不会下载封面，需要一次图片刷新才会去 TMDB 拉图。
BOXSET_COVERS_ENABLED = env("BOXSET_COVERS_ENABLED", "true").lower() in {"1", "true", "yes"}
BOXSET_COVERS_RETRY_DAYS = max(1, int(env("BOXSET_COVERS_RETRY_DAYS", "7")))
BOXSET_COVERS_STATE_FILE = Path(env("BOXSET_COVERS_STATE_FILE", str(CACHE_DIR / "state" / "boxset-covers.json")))
RATING_ACCURACY_FILE = CACHE_DIR / "reports" / "rating-accuracy.json"
DOUBAN_AUDIT_FILE = Path(os.environ.get("DOUBAN_AUDIT_FILE", str(CACHE_DIR / "reports" / "douban-audit.json")))
OMDB_MIN_INTERVAL_SECONDS = max(0.0, float(os.environ.get("OMDB_MIN_INTERVAL_MS", "250")) / 1000)
OMDB_RETRY_COUNT = max(0, int(os.environ.get("OMDB_RETRY_COUNT", "2")))
OMDB_BACKOFF_SECONDS = max(0.1, float(os.environ.get("OMDB_BACKOFF_SECONDS", "2")))
OMDB_CIRCUIT_BREAK_SECONDS = max(60, int(os.environ.get("OMDB_CIRCUIT_BREAK_MINUTES", "15")) * 60)
OMDB_SKIP_GENRES = {value.strip().casefold() for value in os.environ.get("OMDB_SKIP_GENRES", "Concert,Music,Live,综艺,演唱会").split(",") if value.strip()}
POLL_SECONDS = max(300, int(env("POLL_SECONDS", "3600")))
OMDB_CACHE_DAYS = max(1, int(env("OMDB_CACHE_DAYS", "7")))
OMDB_ERROR_CACHE_MINUTES = max(5, int(env("OMDB_ERROR_CACHE_MINUTES", "60")))
RATING_OVERRIDES_FILE = Path(env("RATING_OVERRIDES_FILE", str(CACHE_DIR / "rating-overrides.json")))
RENDER_QUEUE_FILE = Path(env("RENDER_QUEUE_FILE", str(CACHE_DIR / "render-queue.json")))
SERIES_MEDIA_CACHE_DAYS = max(1, int(env("SERIES_MEDIA_CACHE_DAYS", "7")))
WORKER_IMAGE_MARKER = "emby-poster-worker:"
HTTP_PORT = int(env("HTTP_PORT", "8080"))
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", "http://localhost:8098").rstrip("/")
LIBRARY_IDS = {x.strip() for x in env("LIBRARY_IDS").split(",") if x.strip()}
# Scope switch: False keeps the historical "process every library" behaviour.
# Setting LIBRARY_IDS in the environment implies the operator wants a scope.
LIBRARY_SCOPE_ENABLED = bool(LIBRARY_IDS)
ITEM_IDS = {x.strip() for x in env("ITEM_IDS").split(",") if x.strip()}
FORCE_RENDER = env("FORCE_RENDER", "false").lower() in {"1", "true", "yes"}
SEASON_POSTERS_ENABLED = env("SEASON_POSTERS_ENABLED", "false").lower() in {"1", "true", "yes"}
REQUIRE_ALL_RATINGS = env("REQUIRE_ALL_RATINGS", "true").lower() in {"1", "true", "yes"}
RATING_DISPLAY_STRATEGY = env("RATING_DISPLAY_STRATEGY", "compact").lower()
TWO_SOURCE_SECOND_LEFT_RATIO = min(0.70, max(0.45, float(env("TWO_SOURCE_SECOND_LEFT_RATIO", "0.53"))))
IMAGE_DELIVERY_MODE = env("IMAGE_DELIVERY_MODE", "url").lower()
# 试运行：完整走「读 Emby → 取原图 → 评分 → 渲染」，但绝不写回 Emby 图片。
# 渲染结果留在 cache/rendered/ 里，可直接打开检查；状态文件不写入，因此关掉
# DRY_RUN 之后的一次真实扫描仍会把全库当成待处理，不会漏掉任何条目。
DRY_RUN = env("DRY_RUN", "false").lower() in {"1", "true", "yes", "on"}
MEDIA_HEALTH_REPORT_FILE = Path(env("MEDIA_HEALTH_REPORT_FILE", str(CACHE_DIR / "reports" / "media-health.json")))
COMMUNITY_RATING_IS_DOUBAN = env("COMMUNITY_RATING_IS_DOUBAN", "true").lower() in {"1", "true", "yes"}
APP_VERSION = env("APP_VERSION", "1.0.0")
RENDER_VERSION = env("RENDER_VERSION", "2.5")
RATING_REFRESH_DAYS = max(1, int(env("RATING_REFRESH_DAYS", "3")))
DOUBAN_REFRESH_DAYS = max(1, int(env("DOUBAN_REFRESH_DAYS", str(RATING_REFRESH_DAYS))))
DOUBAN_CACHE_DIR = Path(env("DOUBAN_CACHE_DIR", "/data/douban")) if env("DOUBAN_CACHE_DIR", "/data/douban") else None
# Cache outcomes that are definitive (not transient I/O hiccups): a cached
# score must not survive them, otherwise the poster shows a number whose
# source identity no longer exists.
DOUBAN_HARD_MISSES = {
    "missing_ids",
    "negative_placeholder",
    "identity_mismatch",
    "media_type_mismatch",
    "douban_id_mismatch",
    "imdb_id_mismatch",
    "json_without_rating",
}
DOUBAN_REQUIRE_VERIFIED_CACHE = env("DOUBAN_REQUIRE_VERIFIED_CACHE", "true").lower() in {"1", "true", "yes"}
COMMUNITY_RATING_REQUIRES_DOUBAN_ID = env("COMMUNITY_RATING_REQUIRES_DOUBAN_ID", "true").lower() in {"1", "true", "yes"}
DOUBAN_CACHE_STALE_DAYS = max(1, int(env("DOUBAN_CACHE_STALE_DAYS", "90")))
DOUBAN_MISSING_CONFIRMATIONS = max(1, int(env("DOUBAN_MISSING_CONFIRMATIONS", "3")))
CRITIC_RATING_IS_RT = env("CRITIC_RATING_IS_RT", "true").lower() in {"1", "true", "yes"}
IMDB_REFRESH_DAYS = max(1, int(env("IMDB_REFRESH_DAYS", str(RATING_REFRESH_DAYS))))
RT_REFRESH_DAYS = max(1, int(env("RT_REFRESH_DAYS", str(RATING_REFRESH_DAYS))))
RATING_REFRESH_SECONDS_BY_SOURCE = {
    "豆瓣": DOUBAN_REFRESH_DAYS * 86400,
    "IMDb": IMDB_REFRESH_DAYS * 86400,
    "RT": RT_REFRESH_DAYS * 86400,
}
WEB_DIR = Path(__file__).parent / "web"
ADMIN_DATA_DIR = Path(env("ADMIN_DATA_DIR", str(CACHE_DIR / "admin")))
LEGACY_ADMIN_DIR = CACHE_DIR / "admin"
ADMIN_DIR = ADMIN_DATA_DIR
ADMIN_SETTINGS_FILE = ADMIN_DIR / "settings.json"
ADMIN_SECRETS_FILE = ADMIN_DIR / "secrets.json"
ADMIN_AUTH_FILE = ADMIN_DIR / "auth.json"
ADMIN_SESSION_TTL = max(900, int(env("ADMIN_SESSION_TTL", "86400")))
EMBY_REQUEST_TIMEOUT = max(5, int(env("EMBY_REQUEST_TIMEOUT", "30")))
ADMIN_API_TIMEOUT = max(5, int(env("ADMIN_API_TIMEOUT", "12")))
EMBY_PAGE_SIZE = max(50, min(500, int(env("EMBY_PAGE_SIZE", "200"))))

DEFAULT_RENDER_SETTINGS: dict[str, Any] = {
    "badge_position": "top-left",
    "badge_offset_x": 0,
    "badge_offset_y": 20,
    "badge_scale": 1.0,
    "rating_position": "bottom",
    "rating_offset_x": 0,
    "rating_offset_y": 0,
    "rating_scale": 1.0,
    "rating_layouts": {str(index): {"offset_x": 0, "offset_y": 0, "scale": 1.0} for index in (1, 2, 3)},
    "badge_layouts": {str(index): {"offset_x": 0, "offset_y": 20, "scale": 1.0} for index in (1, 2, 3, 4)},
    # Drop zero-information badges (SDR, lossy audio codecs) so the row only
    # advertises what makes this copy stand out.
    "badge_minimal": True,
}
DEFAULT_ADMIN_SETTINGS: dict[str, Any] = {
    "render": DEFAULT_RENDER_SETTINGS.copy(),
    "rating_display_strategy": RATING_DISPLAY_STRATEGY,
    "two_source_second_left_ratio": TWO_SOURCE_SECOND_LEFT_RATIO,
    "community_rating_is_douban": COMMUNITY_RATING_IS_DOUBAN,
    "season_posters_enabled": SEASON_POSTERS_ENABLED,
    "new_item_grace_hours": 48,
    "poll_seconds": POLL_SECONDS,
    "library_ids": sorted(LIBRARY_IDS),
    # Off by default: an existing deployment keeps processing every library
    # until the scope switch is turned on from the settings page.
    "library_scope_enabled": bool(LIBRARY_IDS),
    "item_ids": sorted(ITEM_IDS),
    "image_delivery_mode": IMAGE_DELIVERY_MODE,
    "public_base_url": PUBLIC_BASE_URL,
    # Ops knobs that used to be .env-only. Values here win over the env
    # defaults, so the settings page can change them without a restart.
    "maintenance": {
        "alert_webhook_url": env("ALERT_WEBHOOK_URL", ""),
        "alert_min_failed": max(1, int(env("ALERT_MIN_FAILED", "1"))),
        "backup_enabled": str(env("BACKUP_ENABLED", "true")).lower() in {"1", "true", "yes", "on"},
        "backup_keep": max(1, int(env("BACKUP_KEEP", "2"))),
        "backup_hour": min(23, max(0, int(env("BACKUP_HOUR", "3")))),
        "backup_interval_days": max(1, int(env("BACKUP_INTERVAL_DAYS", "7"))),
        "cache_gc_enabled": str(env("CACHE_GC_ENABLED", "false")).lower() in {"1", "true", "yes", "on"},
        "cache_gc_days": max(1, int(env("CACHE_GC_DAYS", "30"))),
        "rating_refresh_days": {
            "豆瓣": max(1, int(env("DOUBAN_REFRESH_DAYS", "3"))),
            "IMDb": max(1, int(env("IMDB_REFRESH_DAYS", "7"))),
            "RT": max(1, int(env("RT_REFRESH_DAYS", "7"))),
        },
    },
}

def _maint(key: str, default: Any) -> Any:
    """Effective ops setting: admin value first, env-derived default second."""
    section = ADMIN_SETTINGS.get("maintenance") if isinstance(ADMIN_SETTINGS, dict) else None
    if isinstance(section, dict) and key in section:
        return section[key]
    return default


def rating_refresh_seconds() -> dict[str, int]:
    """Per-source refresh window in seconds, honouring the settings page."""
    result = dict(RATING_REFRESH_SECONDS_BY_SOURCE)
    days = _maint("rating_refresh_days", {})
    if isinstance(days, dict):
        for source, value in days.items():
            try:
                result[str(source)] = max(1, int(value)) * 86400
            except (TypeError, ValueError):
                continue
    return result


def _normalise_maintenance(values: dict[str, Any], current: dict[str, Any] | None) -> dict[str, Any]:
    """Validate the ops settings coming from the admin UI."""
    base = dict(DEFAULT_ADMIN_SETTINGS.get("maintenance") or {})
    base.update(current or {})
    if "alert_webhook_url" in values:
        url = str(values.get("alert_webhook_url") or "").strip()
        if url and not url.startswith(("http://", "https://")):
            raise ValueError("告警地址必须是 http:// 或 https:// 链接")
        base["alert_webhook_url"] = url
    for key, low, high in (("alert_min_failed", 1, 100), ("backup_keep", 1, 10),
                           ("backup_hour", 0, 23), ("backup_interval_days", 1, 90),
                           ("cache_gc_days", 1, 365)):
        if key in values:
            try:
                number = int(values[key])
            except (TypeError, ValueError):
                raise ValueError(f"{key} 必须是整数") from None
            base[key] = min(high, max(low, number))
    for key in ("backup_enabled", "cache_gc_enabled"):
        if key in values:
            base[key] = bool(values[key])
    days = values.get("rating_refresh_days")
    if isinstance(days, dict):
        merged = dict(base.get("rating_refresh_days") or {})
        for source in ("豆瓣", "IMDb", "RT"):
            if source in days:
                try:
                    merged[source] = min(365, max(1, int(days[source])))
                except (TypeError, ValueError):
                    raise ValueError("评分刷新天数必须是整数") from None
        base["rating_refresh_days"] = merged
    return base


def _merge_dict(default: dict[str, Any], current: Any) -> dict[str, Any]:
    result = dict(default)
    if isinstance(current, dict):
        for key, value in current.items():
            if isinstance(result.get(key), dict) and isinstance(value, dict):
                result[key] = _merge_dict(result[key], value)
            else:
                result[key] = value
    return result


def _migrate_admin_files() -> None:
    """Move admin state to the dedicated config volume without overwriting it."""
    if ADMIN_DIR == LEGACY_ADMIN_DIR:
        return
    try:
        ADMIN_DIR.mkdir(parents=True, exist_ok=True)
        for name in ("auth.json", "settings.json", "secrets.json"):
            source = LEGACY_ADMIN_DIR / name
            target = ADMIN_DIR / name
            if source.exists() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                try:
                    target.chmod(0o600)
                except OSError:
                    pass
    except OSError as exc:
        LOG.warning("admin state migration failed: %s", exc)


_migrate_admin_files()


def _load_admin_settings() -> dict[str, Any]:
    return _merge_dict(DEFAULT_ADMIN_SETTINGS, _load_json_file(ADMIN_SETTINGS_FILE, {}))

def _load_admin_secrets() -> dict[str, str]:
    payload = _load_json_file(ADMIN_SECRETS_FILE, {})
    payload = payload if isinstance(payload, dict) else {}
    # Keep environment-provided credentials available if an older settings-only
    # save left the persisted secrets file empty.
    for key, value in ((
        ("emby_url", EMBY_URL),
        ("emby_api_key", EMBY_API_KEY),
        ("omdb_api_keys", ",".join(OMDB_API_KEYS)),
        ("mdblist_api_keys", ",".join(MDBLIST_API_KEYS)),
    )):
        if not payload.get(key) and value:
            payload[key] = value
    # Migrate the original single-key setting without losing an existing key.
    if not payload.get("omdb_api_keys") and payload.get("omdb_api_key"):
        payload["omdb_api_keys"] = payload["omdb_api_key"]
    if not payload.get("mdblist_api_keys") and payload.get("mdblist_api_key"):
        payload["mdblist_api_keys"] = payload["mdblist_api_key"]
    return payload

ADMIN_SETTINGS = _load_admin_settings()
_ADMIN_SECRETS = _load_admin_secrets()
if _ADMIN_SECRETS.get("emby_url"):
    EMBY_URL = str(_ADMIN_SECRETS["emby_url"]).rstrip("/")
if _ADMIN_SECRETS.get("emby_api_key"):
    EMBY_API_KEY = str(_ADMIN_SECRETS["emby_api_key"])
if _ADMIN_SECRETS.get("omdb_api_keys") or _ADMIN_SECRETS.get("omdb_api_key"):
    OMDB_API_KEYS = split_api_keys(str(_ADMIN_SECRETS.get("omdb_api_keys") or _ADMIN_SECRETS.get("omdb_api_key") or ""))
    OMDB_API_KEY = OMDB_API_KEYS[0] if OMDB_API_KEYS else ""
if _ADMIN_SECRETS.get("mdblist_api_keys") or _ADMIN_SECRETS.get("mdblist_api_key"):
    MDBLIST_API_KEYS = split_api_keys(str(_ADMIN_SECRETS.get("mdblist_api_keys") or _ADMIN_SECRETS.get("mdblist_api_key") or ""))

RENDER_SETTINGS: dict[str, Any] = _merge_dict(DEFAULT_RENDER_SETTINGS, ADMIN_SETTINGS.get("render"))
RATING_DISPLAY_STRATEGY = str(ADMIN_SETTINGS.get("rating_display_strategy") or RATING_DISPLAY_STRATEGY).lower()
TWO_SOURCE_SECOND_LEFT_RATIO = min(0.70, max(0.45, float(ADMIN_SETTINGS.get("two_source_second_left_ratio") or TWO_SOURCE_SECOND_LEFT_RATIO)))
COMMUNITY_RATING_IS_DOUBAN = bool(ADMIN_SETTINGS.get("community_rating_is_douban", COMMUNITY_RATING_IS_DOUBAN))
SEASON_POSTERS_ENABLED = bool(ADMIN_SETTINGS.get("season_posters_enabled", SEASON_POSTERS_ENABLED))
if ADMIN_SETTINGS.get("poll_seconds"):
    POLL_SECONDS = max(300, int(ADMIN_SETTINGS["poll_seconds"]))
if isinstance(ADMIN_SETTINGS.get("library_ids"), list):
    LIBRARY_IDS = {str(value) for value in ADMIN_SETTINGS["library_ids"] if str(value).strip()}
LIBRARY_SCOPE_ENABLED = bool(ADMIN_SETTINGS.get("library_scope_enabled", LIBRARY_SCOPE_ENABLED))
if isinstance(ADMIN_SETTINGS.get("item_ids"), list):
    ITEM_IDS = {str(value) for value in ADMIN_SETTINGS["item_ids"] if str(value).strip()}
if ADMIN_SETTINGS.get("image_delivery_mode"):
    IMAGE_DELIVERY_MODE = str(ADMIN_SETTINGS["image_delivery_mode"]).lower()
if ADMIN_SETTINGS.get("public_base_url"):
    PUBLIC_BASE_URL = str(ADMIN_SETTINGS["public_base_url"]).rstrip("/")

SESSION = requests.Session()
SESSION.headers.update({"X-Emby-Token": EMBY_API_KEY, "Accept": "application/json"})
# MDBList uses a separate session: the NAS proxy may not route this API, while
# other deployments may require it. Default to direct; opt in with
# MDBLIST_USE_PROXY=true to honor HTTP_PROXY/HTTPS_PROXY/NO_PROXY.
MDBLIST_SESSION = requests.Session()
MDBLIST_SESSION.trust_env = env("MDBLIST_USE_PROXY", "false").lower() in {"1", "true", "yes"}
MDBLIST_SESSION.headers.update({"Accept": "application/json"})

SCAN_STATUS: dict[str, Any] = {
    "status": "starting",
    "version": APP_VERSION,
    "timezone": LOG_TIMEZONE,
    "started_at": None,
    "finished_at": None,
    "processed": 0,
    "total": 0,
    "updated": 0,
    "failed": 0,
    "skipped": 0,
    "last_error": None,
    "current_item": None,
    "last_progress_at": None,
    "elapsed_seconds": 0,
}
SCAN_STATUS_LOCK = threading.Lock()
SCAN_RUN_LOCK = threading.Lock()
SCAN_START_LOCK = threading.Lock()
SCAN_WAKE_EVENT = threading.Event()
OMDB_RATE_LOCK = threading.Lock()
OMDB_NEXT_REQUEST_AT = 0.0
OMDB_CONSECUTIVE_FAILURES = 0
OMDB_CIRCUIT_OPEN_UNTIL = 0.0
OMDB_KEY_INDEX = 0
MDBLIST_RATE_LOCK = threading.Lock()
MDBLIST_NEXT_REQUEST_AT = 0.0
MDBLIST_KEY_INDEX = 0
MDBLIST_KEY_COOLDOWN_UNTIL: dict[int, float] = {}
MDBLIST_KEY_POOL_SIGNATURE = ""
MDBLIST_PREFETCH_LOCK = threading.Lock()
THUMB_LOCK = threading.Lock()
API_KEY_STATUS_LOCK = threading.Lock()
API_KEY_STATUS: dict[str, dict[str, dict[str, Any]]] = {}
RATING_STATS_LOCK = threading.Lock()
REVIEW_ITEMS_LOCK = threading.Lock()
ADMIN_SESSIONS: dict[str, dict[str, Any]] = {}
ADMIN_SESSIONS_LOCK = threading.Lock()
LIBRARY_CACHE: list[dict[str, Any]] = []
LIBRARY_CACHE_AT = 0.0
LIBRARY_CACHE_LOCK = threading.Lock()
STYLE_PREVIEW_LOCK = threading.Lock()
RENDER_LOCK = threading.Lock()
STYLE_PREVIEW_ITEM_ID: str | None = None
ITEM_CACHE: list[dict[str, Any]] = []
ITEM_CACHE_AT = 0.0
ITEM_CACHE_LOCK = threading.Lock()
MATCHING_QUEUE_LOCK = threading.RLock()
MATCHING_QUEUE_CACHE = None
MATCHING_QUEUE_TTL = 3600
MATCHING_QUEUE_CACHE_AT = 0.0
DOUBAN_RESOLVE_CONTEXT = threading.local()



def _save_admin_settings(settings: dict[str, Any]) -> None:
    global ADMIN_SETTINGS, RENDER_SETTINGS, RATING_DISPLAY_STRATEGY
    global TWO_SOURCE_SECOND_LEFT_RATIO, COMMUNITY_RATING_IS_DOUBAN, SEASON_POSTERS_ENABLED
    global POLL_SECONDS, LIBRARY_IDS, ITEM_IDS, IMAGE_DELIVERY_MODE, PUBLIC_BASE_URL
    global ITEM_CACHE_AT, LIBRARY_CACHE_AT, LIBRARY_SCOPE_ENABLED
    previous_seasons = SEASON_POSTERS_ENABLED
    ADMIN_SETTINGS = _merge_dict(DEFAULT_ADMIN_SETTINGS, settings)
    atomic_write_text(ADMIN_SETTINGS_FILE, json.dumps(ADMIN_SETTINGS, ensure_ascii=False, indent=2))
    RENDER_SETTINGS = _merge_dict(DEFAULT_RENDER_SETTINGS, ADMIN_SETTINGS.get("render"))
    # One-time migration to the reference badge layout. Only touch the old
    # defaults, so a deliberately customised position/offset is preserved.
    if int(RENDER_SETTINGS.get("badge_style_version") or 1) < 5:
        RENDER_SETTINGS["badge_position"] = "top-center"
        RENDER_SETTINGS["badge_offset_x"] = 0
        RENDER_SETTINGS["badge_offset_y"] = 20
        for layout in (RENDER_SETTINGS.get("badge_layouts") or {}).values():
            if isinstance(layout, dict):
                layout["offset_x"] = 0
                layout["offset_y"] = 20
        RENDER_SETTINGS["badge_style_version"] = 5
    RATING_DISPLAY_STRATEGY = str(ADMIN_SETTINGS.get("rating_display_strategy") or "compact").lower()
    TWO_SOURCE_SECOND_LEFT_RATIO = min(0.70, max(0.45, float(ADMIN_SETTINGS.get("two_source_second_left_ratio") or 0.53)))
    COMMUNITY_RATING_IS_DOUBAN = bool(ADMIN_SETTINGS.get("community_rating_is_douban", True))
    SEASON_POSTERS_ENABLED = bool(ADMIN_SETTINGS.get("season_posters_enabled", False))
    POLL_SECONDS = max(300, int(ADMIN_SETTINGS.get("poll_seconds") or 3600))
    LIBRARY_IDS = {str(value) for value in ADMIN_SETTINGS.get("library_ids", []) if str(value).strip()}
    LIBRARY_SCOPE_ENABLED = bool(ADMIN_SETTINGS.get("library_scope_enabled", False))
    # A changed scope must apply to the next request, not after the 5-minute
    # library cache expires.
    LIBRARY_CACHE_AT = 0
    ITEM_IDS = {str(value) for value in ADMIN_SETTINGS.get("item_ids", []) if str(value).strip()}
    IMAGE_DELIVERY_MODE = str(ADMIN_SETTINGS.get("image_delivery_mode") or "url").lower()
    PUBLIC_BASE_URL = str(ADMIN_SETTINGS.get("public_base_url") or PUBLIC_BASE_URL).rstrip("/")
    if previous_seasons != SEASON_POSTERS_ENABLED:
        ITEM_CACHE_AT = 0
    _invalidate_matching_queue()


def _save_admin_secrets(secrets_payload: dict[str, str]) -> None:
    global _ADMIN_SECRETS, EMBY_URL, EMBY_API_KEY, OMDB_API_KEY, OMDB_API_KEYS, MDBLIST_API_KEYS, MDBLIST_KEY_INDEX, MDBLIST_KEY_COOLDOWN_UNTIL
    _ADMIN_SECRETS = {str(key): str(value) for key, value in secrets_payload.items() if value is not None}
    atomic_write_text(ADMIN_SECRETS_FILE, json.dumps(_ADMIN_SECRETS, ensure_ascii=False, indent=2))
    EMBY_URL = str(_ADMIN_SECRETS.get("emby_url") or "").rstrip("/")
    EMBY_API_KEY = str(_ADMIN_SECRETS.get("emby_api_key") or "")
    OMDB_API_KEYS = split_api_keys(str(_ADMIN_SECRETS.get("omdb_api_keys") or _ADMIN_SECRETS.get("omdb_api_key") or ""))
    OMDB_API_KEY = OMDB_API_KEYS[0] if OMDB_API_KEYS else ""
    MDBLIST_API_KEYS = split_api_keys(str(_ADMIN_SECRETS.get("mdblist_api_keys") or _ADMIN_SECRETS.get("mdblist_api_key") or ""))
    MDBLIST_KEY_INDEX = 0
    MDBLIST_KEY_COOLDOWN_UNTIL = {}
    SESSION.headers["X-Emby-Token"] = EMBY_API_KEY


def _hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260_000)
    return base64.b64encode(salt).decode("ascii"), base64.b64encode(digest).decode("ascii")


def _ensure_admin_auth() -> dict[str, Any]:
    payload = _load_json_file(ADMIN_AUTH_FILE, {})
    if isinstance(payload, dict) and payload.get("username") and payload.get("salt") and payload.get("password_hash"):
        return payload
    username = env("ADMIN_USERNAME", "admin") or "admin"
    bootstrap_password = env("ADMIN_PASSWORD")
    generated = False
    if not bootstrap_password:
        bootstrap_password = secrets.token_urlsafe(12)
        generated = True
    salt, password_hash = _hash_password(bootstrap_password)
    payload = {"username": username, "salt": salt, "password_hash": password_hash, "force_change": True}
    try:
        atomic_write_text(ADMIN_AUTH_FILE, json.dumps(payload, ensure_ascii=False, indent=2))
        try:
            ADMIN_AUTH_FILE.chmod(0o600)
        except OSError:
            pass
    except OSError as exc:
        LOG.warning("admin auth file could not be saved: %s", exc)
    if generated:
        LOG.warning("Admin bootstrap credentials: username=%s password=%s; change this password after first login", username, bootstrap_password)
    return payload


def _verify_password(password: str, payload: dict[str, Any]) -> bool:
    try:
        salt = base64.b64decode(str(payload.get("salt")))
        expected = base64.b64decode(str(payload.get("password_hash")))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260_000)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _new_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    with ADMIN_SESSIONS_LOCK:
        ADMIN_SESSIONS[token] = {"username": username, "created_at": time.time(), "last_seen": time.time()}
    return token


def _session_username(handler: SimpleHTTPRequestHandler) -> str | None:
    cookie = SimpleCookie()
    try:
        cookie.load(handler.headers.get("Cookie", ""))
    except (ValueError, KeyError):
        return None
    token = cookie.get("pw_session")
    if token is None:
        return None
    with ADMIN_SESSIONS_LOCK:
        session = ADMIN_SESSIONS.get(token.value)
        if not session:
            return None
        if time.time() - float(session.get("last_seen", 0)) > ADMIN_SESSION_TTL:
            ADMIN_SESSIONS.pop(token.value, None)
            return None
        session["last_seen"] = time.time()
        return str(session.get("username") or "admin")


def _destroy_session(handler: SimpleHTTPRequestHandler) -> None:
    cookie = SimpleCookie()
    try:
        cookie.load(handler.headers.get("Cookie", ""))
    except (ValueError, KeyError):
        return
    token = cookie.get("pw_session")
    if token:
        with ADMIN_SESSIONS_LOCK:
            ADMIN_SESSIONS.pop(token.value, None)


def _json_body(handler: SimpleHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length > 2 * 1024 * 1024:
        raise ValueError("request body is too large")
    raw = handler.rfile.read(length) if length else b"{}"
    handler._body_consumed = True  # noqa: SLF001 - keep-alive needs this flag
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


def _wants_gzip(handler: SimpleHTTPRequestHandler) -> bool:
    return "gzip" in (handler.headers.get("Accept-Encoding") or "").lower()


def _maybe_gzip(handler: SimpleHTTPRequestHandler, body: bytes, content_type: str) -> tuple[bytes, bool]:
    """Compress text payloads when the client supports it and it actually helps."""
    compressible = (
        content_type.startswith("text/")
        or "javascript" in content_type
        or "json" in content_type
        or "svg" in content_type
    )
    if not compressible or len(body) < 1024 or not _wants_gzip(handler):
        return body, False
    packed = gzip.compress(body, compresslevel=6)
    if len(packed) >= len(body) * 0.92:  # not worth the transfer overhead
        return body, False
    return packed, True


def _json_response(handler: SimpleHTTPRequestHandler, payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    body, compressed = _maybe_gzip(handler, raw, "application/json; charset=utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Vary", "Accept-Encoding")
    if compressed:
        handler.send_header("Content-Encoding", "gzip")
    if headers:
        for key, value in headers.items():
            handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)


def _drain_request_body(handler: SimpleHTTPRequestHandler) -> None:
    """Consume an unread request body so HTTP/1.1 keep-alive stays in sync.

    Rejecting a POST before reading its body leaves the payload in the socket;
    the next request on the same connection would then be parsed from those
    bytes and answered with a spurious 400.
    """
    if getattr(handler, "_body_consumed", False):
        return
    try:
        length = int(handler.headers.get("Content-Length", "0") or 0)
    except (TypeError, ValueError):
        length = 0
    if 0 < length <= 4 * 1024 * 1024:
        try:
            handler.rfile.read(length)
        except OSError:
            pass
    handler._body_consumed = True  # noqa: SLF001


def _error_response(handler: SimpleHTTPRequestHandler, message: str, status: int = 400) -> None:
    _drain_request_body(handler)
    _json_response(handler, {"error": message}, status)


def _require_admin(handler: SimpleHTTPRequestHandler) -> bool:
    if _session_username(handler):
        return True
    _error_response(handler, "需要管理员登录", 401)
    return False


def _safe_item_state(item_id: str) -> dict[str, Any]:
    state_file = CACHE_DIR / "state" / f"{item_id}.json"
    payload = _load_json_file(state_file, {})
    return payload if isinstance(payload, dict) else {}


def _poster_url(item_id: str, version: str | None = None) -> str:
    version = version or str(RENDER_VERSION)
    return f"/{quote(str(item_id))}.jpg?v={quote(version)}"


RATING_DIAGNOSTIC_META: dict[str, dict[str, str]] = {
    "missing_both_ids": {"label":"豆瓣 / IMDb ID 都缺失","severity":"warning","description":"搜索并确认作品，可一次补齐已核实的关联 ID。","filter":"missing_both_ids"},
    "missing_douban_id": {"label":"豆瓣 ID 缺失","severity":"info","description":"可能已通过 IMDb 定位缓存，仍可确认豆瓣条目。","filter":"missing_douban_id"},
    "missing_imdb_id": {"label":"IMDb ID 缺失","severity":"info","description":"确认对应 IMDb 作品，用于补充 IMDb / 烂番茄。","filter":"missing_imdb_id"},
    "ids_without_score": {"label":"ID 已齐但豆瓣评分未获取","severity":"info","description":"查看缓存状态、上游限流或平台暂无评分；无需重复匹配。","filter":"ids_without_score"},

    "douban_identity": {"label": "豆瓣作品/季别需核对", "severity": "critical", "description": "缓存作品类型或标题与年份有明显冲突，已阻止错误评分。", "filter": "rating_mismatch"},
    "douban_cache_old": {"label": "豆瓣缓存较旧", "severity": "info", "description": "最后已知评分仍可显示，但不等于平台当前分数。", "filter": "issues"},
    "douban_missing": {"label": "豆瓣评分缺失", "severity": "warning", "description": "没有可通过身份校验的豆瓣缓存评分；通用 CommunityRating 不会被冒充为豆瓣。", "filter": "missing_rating"},
    "douban_stale": {"label": "豆瓣评分未同步", "severity": "critical", "description": "海报缓存的豆瓣分数与当前优先数据源不一致。", "filter": "rating_mismatch"},
    "imdb_missing": {"label": "IMDb 评分缺失", "severity": "info", "description": "没有获取到 IMDb 评分，不会用其他来源冒充。", "filter": "missing_rating"},
    "rt_missing": {"label": "烂番茄评分缺失", "severity": "info", "description": "没有获取到烂番茄评分，不会用其他来源冒充。", "filter": "missing_rating"},
    "omdb_api_invalid": {"label": "OMDb API 无效", "severity": "critical", "description": "OMDb 返回 Invalid API key，IMDb/RT 在线查询会失败。", "filter": "rating_mismatch"},
    "omdb_unavailable": {"label": "OMDb 暂不可用", "severity": "warning", "description": "OMDb 查询失败或触发限流，评分可能暂时不完整。", "filter": "rating_mismatch"},
    "omdb_not_configured": {"label": "OMDb 未配置", "severity": "info", "description": "未配置 OMDb，IMDb/RT 只能使用 Emby 已有字段或手工覆盖。", "filter": "missing_rating"},
    "poster_missing": {"label": "评分海报缺失", "severity": "warning", "description": "Worker 尚未生成该资源的评分海报。", "filter": "issues"},
    "media_issue": {"label": "媒体资源异常", "severity": "warning", "description": "媒体流、画质或字幕信息存在待检查项。", "filter": "issues"},
}



def audit_douban_ratings(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Report mutually exclusive cache-resolution reasons and overlapping flags."""
    counts: Counter[str] = Counter()
    findings: list[dict[str, Any]] = []
    for item in items:
        result = _douban_cache_result(item)
        cache_score = result.get("value")
        age = result.get("cache_age_days")
        emby_score = _format_douban(item.get("CommunityRating"))
        status = str(result["status"])
        counts["reason_" + status] += 1
        if result.get("identity_warnings"):
            counts["identity_review"] += 1
        if cache_score:
            counts["verified_cache"] += 1
            if result.get("match") == "imdb_id":
                counts["verified_by_imdb_only"] += 1
            if emby_score is None:
                classification = "emby_missing"
            elif emby_score != cache_score:
                classification = "score_conflict"
            else:
                classification = "score_match"
            if age is not None and age > DOUBAN_CACHE_STALE_DAYS:
                counts["cache_older_than_threshold"] += 1
        else:
            classification = "missing_ids" if status == "missing_ids" else "missing_or_invalid_cache"
            if emby_score is not None:
                counts["unverified_emby_score"] += 1
        counts[classification] += 1
        if classification != "score_match" or (age is not None and age > DOUBAN_CACHE_STALE_DAYS):
            findings.append({
                "id": str(item.get("Id") or ""), "name": item.get("Name"), "year": item.get("ProductionYear"),
                "douban_id": provider_id(item.get("ProviderIds") or {}, "Douban"),
                "resolved_douban_id": result.get("douban_id"),
                "imdb_id": provider_id(item.get("ProviderIds") or {}, "Imdb"),
                "classification": classification, "reason": status,
                "cache_rating": cache_score, "emby_rating": emby_score,
                "cache_age_days": age, "cache_updated_at": result.get("cache_updated_at"),
                "cache_title": result.get("cache_title"), "cache_year": result.get("cache_year"),
                "identity_warnings": result.get("identity_warnings", []),
            })
    report = {
        "generated_at": current_time_iso(), "total": len(items),
        "strict_cache_required": DOUBAN_REQUIRE_VERIFIED_CACHE,
        "stale_after_days": DOUBAN_CACHE_STALE_DAYS,
        "counts": dict(sorted(counts.items())), "findings": findings,
    }
    atomic_write_text(DOUBAN_AUDIT_FILE, json.dumps(report, ensure_ascii=False, indent=2))
    LOG.info("Douban audit: %s", report["counts"])
    return report


def _rating_diagnostic_codes(
    item: dict[str, Any],
    values: dict[str, Any],
    details: dict[str, Any],
    has_rendered: bool,
    media_issues: list[dict[str, Any]],
) -> list[str]:
    """Return actionable data-integrity findings for one library item.

    Scores are never substituted between providers. Prefer the matched local
    Douban cache when configured, otherwise inspect the Emby fallback value.
    """
    codes: list[str] = media_match.id_gaps(item,_rating_override(item))
    resolved = _douban_cache_result(item)
    if not codes and not values.get("豆瓣"):
        codes.append("ids_without_score")
    if resolved.get("status") in {"identity_mismatch", "media_type_mismatch"}:
        codes.append("douban_identity")
    if (resolved.get("cache_age_days") or 0) > DOUBAN_CACHE_STALE_DAYS:
        codes.append("douban_cache_old")
    douban_detail = details.get("豆瓣") if isinstance(details.get("豆瓣"), dict) else {}
    douban_is_override = douban_detail.get("source") == "rating_overrides"
    live_douban, _live_source = _douban_rating(item)
    poster_douban = _format_douban(values.get("豆瓣"))
    if not douban_is_override:
        if live_douban is None:
            codes.append("douban_stale" if poster_douban is not None else "douban_missing")
        elif poster_douban != live_douban:
            codes.append("douban_stale")

    for source, missing_code in (("IMDb", "imdb_missing"), ("RT", "rt_missing")):
        if values.get(source):
            continue
        detail = details.get(source) if isinstance(details.get(source), dict) else {}
        status = str(detail.get("status") or "")
        if status == "invalid_api_key":
            codes.append("omdb_api_invalid")
        elif status in {"lookup_error", "http_error", "rate_limited", "circuit_open"}:
            codes.append("omdb_unavailable")
        elif status in {"no_api_key", "not_configured"}:
            codes.append("omdb_not_configured")
        else:
            codes.append(missing_code)

    if not has_rendered:
        codes.append("poster_missing")
    if media_issues:
        codes.append("media_issue")
    return list(dict.fromkeys(codes))


def _rating_provenance(item: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    result = {k: dict(v) for k, v in (state.get("rating_details") or {}).items() if isinstance(v, dict)}
    checked = state.get("rating_checked_at") or {}
    imdb_id = media_match.effective_ids(item,_rating_override(item)).get("imdb")
    for source, info in result.items():
        info["worker_checked_at"] = checked.get(source)
        if source == "豆瓣" and info.get("source") == "douban.local_cache":
            live = _douban_cache_result(item)
            info["cache_updated_at"] = live.get("cache_updated_at")
            info["cache_age_days"] = live.get("cache_age_days")
            info["stale"] = (live.get("cache_age_days") or 0) > DOUBAN_CACHE_STALE_DAYS
            continue
        if not imdb_id:
            continue
        if str(info.get("source") or "").startswith("mdblist"):
            path = _mdblist_cache_file("show" if item.get("Type") == "Series" else "movie", imdb_id)
            data = _load_json_file(path, {})
            times = data.get("checked_at") or {}
            info["provider_checked_at"] = times.get("imdb" if source == "IMDb" else "tomatoes") if isinstance(times, dict) else times
        elif str(info.get("source") or "").startswith("omdb"):
            path = CACHE_DIR / "omdb" / f"{imdb_id}.json"
            if path.is_file():
                info["provider_checked_at"] = path.stat().st_mtime
        info["stale"] = info.get("status") in {"temporarily_unavailable", "temporarily_missing"}
    return result


def _item_summary(item: dict[str, Any]) -> dict[str, Any]:
    emby_type = item.get("Type")
    item = _effective_item_type(item)
    item_id = str(item.get("Id") or "")
    state = _safe_item_state(item_id)
    override = _rating_override(item)
    details = _rating_provenance(item, state)
    values = state.get("ratings") if isinstance(state.get("ratings"), dict) else {}
    primary = bool((item.get("ImageTags") or {}).get("Primary"))
    rendered = (CACHE_DIR / "rendered" / f"{item_id}.jpg").exists()
    issues = media_health(item).get("issues", [])
    diagnostic_codes = _rating_diagnostic_codes(item, values, details, rendered, issues)
    library = _library_for_item(item)
    triage = _item_triage(item,state)
    return {
        "id": item_id,
        "name": item.get("Name") or "未命名",
        "type": item.get("Type"),
        "emby_type": emby_type,
        "task": _task_state(item_id),
        "cache_status": _douban_cache_result(item),
        "douban_gap": issue_policy.douban_gap(item,state,_douban_cache_result(item),media_match.effective_ids(item,override),triage,override),
        "year": item.get("ProductionYear"),
        "original_title": item.get("OriginalTitle"),
        "genres": item.get("Genres") or [],
        "provider_ids": item.get("ProviderIds") or {},
        "poster_url": _poster_url(
            item_id,
            f"{RENDER_VERSION}-{str(state.get('signature') or '')[:12]}" if state.get("signature") else (state.get("render_version") or RENDER_VERSION),
        ) if rendered else None,
        "original_poster_url": f"/original/{quote(item_id)}.jpg" if (CACHE_DIR / "original" / f"{item_id}.jpg").exists() else None,
        "has_primary": primary,
        "has_rendered": rendered,
        "ratings": values,
        "rating_details": details,
        "rating_checked_at": state.get("rating_checked_at", {}),
        "effective_ids": media_match.effective_ids(item,override),
        "matching_flags": _match_queue_flags(item),
        "triage": triage,
        "provider_ids": item.get("ProviderIds") or {},
        "override": override,
        "render_version": state.get("render_version"),
        "last_rendered_at": state.get("rendered_at"),
        "issues": issues,
        "diagnostic_codes": diagnostic_codes,
        "attention_count": triage["counts"]["action"],
        "quality_labels": quality_labels(item),
        "date_modified": item.get("DateModified"),
        "library_id": library.get("id") if library else None,
        "library_name": library.get("name") if library else "未分类",
        "library_collection_type": library.get("collection_type") if library else None,
    }


def _get_item_by_id(item_id: str) -> dict[str, Any] | None:
    if not EMBY_URL or not EMBY_API_KEY:
        return None
    fields = "ProviderIds,MediaSources,MediaStreams,Path,OriginalTitle,Genres,CommunityRating,CriticRating,ProductionYear,DateCreated,DateLastMediaAdded,DateModified,ImageTags,ParentId,SeriesId,IndexNumber,Type,Name,Overview"
    try:
        result = emby("GET", "/Items", params={"Ids": item_id, "Fields": fields}, timeout=ADMIN_API_TIMEOUT).json().get("Items", [])
        return result[0] if result else None
    except requests.RequestException:
        return None




def _library_definitions(force: bool = False) -> list[dict[str, Any]]:
    global LIBRARY_CACHE, LIBRARY_CACHE_AT
    with LIBRARY_CACHE_LOCK:
        if not force and LIBRARY_CACHE and time.time() - LIBRARY_CACHE_AT < 300:
            return [dict(item) for item in LIBRARY_CACHE]
    if not EMBY_URL or not EMBY_API_KEY:
        return []
    try:
        payload = emby("GET", "/Library/VirtualFolders", timeout=ADMIN_API_TIMEOUT).json()
    except requests.RequestException as exc:
        LOG.warning("library definitions unavailable: %s", exc)
        return []
    definitions: list[dict[str, Any]] = []
    for library in payload if isinstance(payload, list) else []:
        if not isinstance(library, dict) or not library.get("ItemId"):
            continue
        definitions.append({
            "id": str(library.get("ItemId")),
            "name": str(library.get("Name") or "未命名影视库"),
            "collection_type": str(library.get("CollectionType") or ""),
            "locations": [str(path) for path in library.get("Locations") or [] if path],
        })
    with LIBRARY_CACHE_LOCK:
        LIBRARY_CACHE = definitions
        LIBRARY_CACHE_AT = time.time()
    return [dict(item) for item in definitions]


def _library_for_item(item: dict[str, Any], libraries: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    path = str(item.get("Path") or "").replace("\\", "/").rstrip("/").casefold()
    if not path:
        return None
    candidates: list[tuple[int, dict[str, Any]]] = []
    for library in libraries or _library_definitions():
        for location in library.get("locations") or []:
            prefix = str(location).replace("\\", "/").rstrip("/").casefold()
            if prefix and (path == prefix or path.startswith(prefix + "/")):
                candidates.append((len(prefix), library))
    if not candidates:
        return None
    return dict(max(candidates, key=lambda entry: entry[0])[1])


def _enabled_library_ids() -> set[str] | None:
    """Allow-list of libraries the worker may touch.

    ``None`` means "no restriction": an empty setting keeps the historical
    behaviour of processing every library. A non-empty setting is an explicit
    allow-list, so a library created later is not processed until it is added.
    """
    if not LIBRARY_SCOPE_ENABLED:
        return None
    known = {str(library.get("id")) for library in _library_definitions()}
    if not known:
        # Emby is unreachable. Refusing every item here would look like a total
        # outage, so fall back to the unrestricted behaviour for this pass.
        LOG.warning("library allow-list ignored: Emby library list unavailable")
        return None
    if not LIBRARY_IDS:
        # Scope enabled with nothing selected means "process no library".
        return set()
    matched = LIBRARY_IDS & known
    if not matched:
        LOG.warning("library allow-list %s matches no Emby library; restriction ignored",
                    sorted(LIBRARY_IDS))
        return None
    return matched


def library_enabled(item: dict[str, Any]) -> bool:
    """Whether this item belongs to a library selected for poster production.

    Resolution is path-based because Emby's ``ParentIds`` filter is ignored for
    some virtual/mixed libraries. An item whose library cannot be resolved is
    skipped while a restriction is active, so an unmapped path never receives a
    poster by accident.
    """
    allowed = _enabled_library_ids()
    if allowed is None:
        return True
    library = _library_for_item(item, _library_definitions())
    if library is None:
        LOG.warning("skipping %s: no library resolved for path %s",
                    item.get("Name"), str(item.get("Path") or "")[:120])
        return False
    return str(library.get("id")) in allowed


CONNECTION_CACHE_TTL = 60.0
_CONNECTION_CACHE: dict[str, Any] = {}
_CONNECTION_CACHE_LOCK = threading.Lock()
_CONNECTION_CACHE_AT = 0.0


def _connection_status(force: bool = False) -> dict[str, Any]:
    """Emby reachability, cached briefly so 5s polling never hammers the server."""
    global _CONNECTION_CACHE, _CONNECTION_CACHE_AT
    now = time.time()
    with _CONNECTION_CACHE_LOCK:
        if not force and _CONNECTION_CACHE and now - _CONNECTION_CACHE_AT < CONNECTION_CACHE_TTL:
            return dict(_CONNECTION_CACHE)
    if not EMBY_URL or not EMBY_API_KEY:
        result = {"configured": False, "ok": False, "message": "尚未配置 Emby"}
    else:
        try:
            system = emby("GET", "/System/Info", timeout=ADMIN_API_TIMEOUT).json()
            result = {"configured": True, "ok": True, "server_name": system.get("ServerName"), "version": system.get("Version")}
        except requests.RequestException as exc:
            result = {"configured": True, "ok": False, "message": str(exc)}
    with _CONNECTION_CACHE_LOCK:
        _CONNECTION_CACHE = result
        _CONNECTION_CACHE_AT = now
    return dict(result)



def _mask_api_key(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:3]}{'*' * max(4, len(text) - 6)}{text[-3:]}"


def _api_key_fingerprint(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _set_api_key_status(provider: str, index: int, key: str, status: str, message: str = "") -> dict[str, Any]:
    checked_at = current_time_iso()
    entry = {
        "index": index,
        "masked": _mask_api_key(key),
        "status": status,
        "message": message,
        "checked_at": checked_at,
    }
    with API_KEY_STATUS_LOCK:
        API_KEY_STATUS.setdefault(provider, {})[_api_key_fingerprint(key)] = {
            **entry,
            "key_fingerprint": _api_key_fingerprint(key),
        }
    return entry


def _api_key_status_payload() -> dict[str, Any]:
    configured = {
        "emby": [EMBY_API_KEY] if EMBY_API_KEY else [],
        "omdb": list(OMDB_API_KEYS),
        "mdblist": list(MDBLIST_API_KEYS),
    }
    providers: dict[str, dict[str, Any]] = {}
    with API_KEY_STATUS_LOCK:
        saved = {provider: dict(values) for provider, values in API_KEY_STATUS.items()}
    for provider, keys in configured.items():
        rows = []
        for index, key in enumerate(keys, 1):
            previous = saved.get(provider, {}).get(_api_key_fingerprint(key), {})
            rows.append({
                "index": index,
                "masked": _mask_api_key(key),
                "status": previous.get("status", "unchecked"),
                "message": previous.get("message", "尚未检测"),
                "checked_at": previous.get("checked_at"),
            })
        providers[provider] = {"configured": bool(keys), "count": len(keys), "keys": rows}
    return {"generated_at": current_time_iso(), "providers": providers}


def _request_with_retry(session: requests.Session, method: str, url: str, **kwargs: Any) -> requests.Response:
    """Overseas rating APIs occasionally stall for a few seconds.

    A single retry keeps one hiccup from being reported to the operator as a
    permanent network error, while still surfacing a genuinely unreachable
    endpoint.
    """
    last_exc: requests.Timeout | None = None
    for attempt in range(2):
        try:
            return session.request(method, url, **kwargs)
        except requests.Timeout as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(1.5)
    assert last_exc is not None
    raise last_exc


def _check_single_api_key(provider: str, index: int, key: str) -> dict[str, Any]:
    try:
        if provider == "emby":
            response = SESSION.get(
                f"{EMBY_URL}/System/Info",
                headers={"X-Emby-Token": key, "Accept": "application/json"},
                timeout=ADMIN_API_TIMEOUT,
            )
            if response.status_code in {401, 403}:
                return _set_api_key_status(provider, index, key, "invalid", "Emby API Key 无效或权限不足")
            response.raise_for_status()
            return _set_api_key_status(provider, index, key, "available", "连接正常")
        if provider == "omdb":
            response = _request_with_retry(
                SESSION,
                "GET",
                "https://www.omdbapi.com/",
                params={"apikey": key, "i": "tt0111161"},
                headers={"Accept": "application/json"},
                timeout=ADMIN_API_TIMEOUT,
            )
            if response.status_code == 429:
                return _set_api_key_status(provider, index, key, "rate_limited", "OMDb 已触发限流")
            if response.status_code in {401, 403}:
                return _set_api_key_status(provider, index, key, "invalid", "OMDb API Key 无效")
            response.raise_for_status()
            payload = response.json()
            error = str(payload.get("Error") or "") if isinstance(payload, dict) else ""
            folded = error.casefold()
            if "invalid api key" in folded:
                return _set_api_key_status(provider, index, key, "invalid", error)
            if "request limit" in folded or "too many" in folded or "rate" in folded and "limit" in folded:
                return _set_api_key_status(provider, index, key, "rate_limited", error)
            return _set_api_key_status(provider, index, key, "available", "连接正常")
        if provider == "mdblist":
            response = _request_with_retry(
                MDBLIST_SESSION,
                "POST",
                f"{MDBLIST_API_BASE}/rating/movie/imdb",
                params={"apikey": key},
                json={"ids": ["tt0111161"], "provider": "imdb"},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=ADMIN_API_TIMEOUT,
            )
            if response.status_code == 429:
                return _set_api_key_status(provider, index, key, "rate_limited", response.headers.get("Retry-After") and "MDBList 已触发限流" or "MDBList 已触发限流")
            if response.status_code in {401, 403}:
                return _set_api_key_status(provider, index, key, "invalid", "MDBList API Key 无效")
            response.raise_for_status()
            payload = response.json()
            error = str(payload.get("error") or payload.get("Error") or "") if isinstance(payload, dict) else ""
            if error:
                folded = error.casefold()
                if "limit" in folded or "rate" in folded:
                    return _set_api_key_status(provider, index, key, "rate_limited", error)
                if "invalid" in folded or "unauthorized" in folded:
                    return _set_api_key_status(provider, index, key, "invalid", error)
            return _set_api_key_status(provider, index, key, "available", "连接正常")
        return _set_api_key_status(provider, index, key, "unknown", "未知服务")
    except requests.Timeout:
        return _set_api_key_status(provider, index, key, "network_error", "请求超时（已自动重试 1 次）")
    except requests.RequestException as exc:
        return _set_api_key_status(provider, index, key, "network_error", str(exc)[:180])
    except (ValueError, TypeError) as exc:
        return _set_api_key_status(provider, index, key, "invalid_response", str(exc)[:180])



def _provider_key_list(provider: str) -> list[str]:
    if provider == "emby":
        return [EMBY_API_KEY] if EMBY_API_KEY else []
    if provider == "omdb":
        return list(OMDB_API_KEYS)
    if provider == "mdblist":
        return list(MDBLIST_API_KEYS)
    raise ValueError("不支持的 API Key 类型")


def _update_provider_key(provider: str, index: int, value: str | None = None, remove: bool = False) -> dict[str, Any]:
    """Replace one key in the persisted pool without exposing existing secrets."""
    global _ADMIN_SECRETS
    if provider not in {"emby", "omdb", "mdblist"}:
        raise ValueError("不支持的 API Key 类型")
    if index < 1:
        raise ValueError("API Key 序号无效")
    keys = _provider_key_list(provider)
    if remove:
        if index > len(keys):
            raise ValueError("API Key 不存在")
        keys.pop(index - 1)
    else:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("请输入 API Key")
        if index > len(keys) + 1:
            raise ValueError("API Key 序号不连续")
        if index == len(keys) + 1:
            keys.append(cleaned)
        else:
            keys[index - 1] = cleaned

    secrets_payload = dict(_ADMIN_SECRETS)
    if provider == "emby":
        secrets_payload["emby_api_key"] = keys[0] if keys else ""
        if not keys:
            secrets_payload.pop("emby_api_key", None)
    elif provider == "omdb":
        secrets_payload["omdb_api_keys"] = ",".join(keys)
        secrets_payload.pop("omdb_api_key", None)
    else:
        secrets_payload["mdblist_api_keys"] = ",".join(keys)
        secrets_payload.pop("mdblist_api_key", None)
    _save_admin_secrets(secrets_payload)
    return _api_key_status_payload()


def _check_api_keys() -> dict[str, Any]:
    results = {"emby": [], "omdb": [], "mdblist": []}
    if EMBY_API_KEY:
        results["emby"].append(_check_single_api_key("emby", 1, EMBY_API_KEY))
    for index, key in enumerate(OMDB_API_KEYS, 1):
        results["omdb"].append(_check_single_api_key("omdb", index, key))
    for index, key in enumerate(MDBLIST_API_KEYS, 1):
        results["mdblist"].append(_check_single_api_key("mdblist", index, key))
    return {"generated_at": current_time_iso(), "providers": {provider: {"configured": bool(rows), "count": len(rows), "keys": rows} for provider, rows in results.items()}}

def _overview_payload() -> dict[str, Any]:
    items = get_items_cached(timeout=ADMIN_API_TIMEOUT) if EMBY_URL and EMBY_API_KEY else []
    diagnostic = _matching_queue_payload()
    triage_by_id = {row['id']: row['triage'] for row in diagnostic['items']}
    summaries = []
    for item in items:
        item_id = str(item['Id'])
        saved = _safe_item_state(item_id)
        summaries.append({'ratings': saved.get('ratings') or {}, 'rating_details': saved.get('rating_details') or {},
                          'has_rendered': (CACHE_DIR/'rendered'/f'{item_id}.jpg').is_file(),
                          'issues': saved.get('media_issues') or [],
                          'triage': triage_by_id.get(item_id, {'status':'healthy','issues':[]})})
    total = len(summaries)
    ratings_covered = sum(bool(s["ratings"]) for s in summaries)
    all_sources = {"豆瓣": 0, "IMDb": 0, "RT": 0}
    for summary in summaries:
        for source in all_sources:
            if summary["ratings"].get(source):
                all_sources[source] += 1
    issue_items = sum(bool(s["issues"]) for s in summaries)
    rendered = sum(bool(s["has_rendered"]) for s in summaries)
    diagnostic_counts: Counter[str]=Counter()
    buckets=Counter();douban_verified=0
    for summary in summaries:
        triage=summary['triage'];buckets[triage['status']]+=1
        for issue in triage['issues']:
            if issue['bucket']=='action':diagnostic_counts[issue['code']]+=1
        if (summary.get('rating_details') or {}).get('豆瓣',{}).get('source') in {'douban.local_cache','rating_overrides'} and (summary.get('ratings') or {}).get('豆瓣'):douban_verified+=1
    diagnostic_items=buckets['action']
    diagnostic_rows=[{'code':code,'count':value,'label':issue_policy.LABELS.get(code,code),'severity':'critical' if code=='identity_conflict' else 'warning','description':'进入影视库查看原因并使用对应操作。','filter':'issues'} for code,value in diagnostic_counts.most_common()]
    critical_count=sum(row['count'] for row in diagnostic_rows if row['severity']=='critical')
    warning_count=sum(row['count'] for row in diagnostic_rows if row['severity']=='warning')
    diagnostic_status='action' if critical_count else 'attention' if warning_count else 'healthy'
    report = _read_report(SCAN_REPORT_FILE, {})
    health_report = _read_report(MEDIA_HEALTH_REPORT_FILE, {})
    return {
        "total": total,
        "rendered": rendered,
        "rendered_percent": round(rendered * 100 / total, 1) if total else 0,
        "rating_covered": ratings_covered,
        "rating_covered_percent": round(ratings_covered * 100 / total, 1) if total else 0,
        "source_coverage": {key: {"count": value, "percent": round(value * 100 / total, 1) if total else 0} for key, value in all_sources.items()},
        "issue_items": issue_items,
        "diagnostics": {
            "status": diagnostic_status,
            "items_with_diagnostics": diagnostic_items,
            "triage_counts": dict(buckets),
            "critical_count": critical_count,
            "warning_count": warning_count,
            "douban_verified": douban_verified,
            "douban_verified_percent": round(douban_verified * 100 / total, 1) if total else 0,
            "rows": diagnostic_rows,
            "douban_gaps": diagnostic.get("douban_gaps", {}),
        },
        "scan": report,
        "rating_activity": {
            "last_scan_at": report.get("finished_at") if isinstance(report, dict) else None,
            "scan_status": report.get("status") if isinstance(report, dict) else None,
            "rating_results": report.get("rating_results", {}) if isinstance(report, dict) else {},
            "rating_prefetch": report.get("rating_prefetch", {}) if isinstance(report, dict) else {},
            "source_coverage": {key: {"count": value, "percent": round(value * 100 / total, 1) if total else 0} for key, value in all_sources.items()},
        },
        "health": health_report.get("summary", health_report if isinstance(health_report, dict) else {}),
        "connection": _connection_status(),
        "status": scan_status_snapshot(),
    }


def _update_rating_override(item_id: str, values: dict[str, Any]) -> dict[str, Any]:
    overrides = _load_rating_overrides()
    previous = overrides.get(str(item_id)) or {}
    cleaned: dict[str, Any] = {key:previous[key] for key in ("match", "binding_verified_at", "douban_id", "imdb_id") if key in previous}
    allowed = {"douban", "豆瓣", "imdb", "IMDb", "rt", "rottentomatoes", "烂番茄", "imdb_id", "imdbid", "douban_id", "reason", "verified_at"}
    for key, value in values.items():
        if str(key) in allowed and value not in (None, ""):
            cleaned[str(key)] = str(value).strip()
    modes=values.get("score_modes") or {}
    if modes:
        for key in ("douban","imdb","rt"):
            if key not in modes and previous.get(key):cleaned[key]=previous[key]
    if modes:
        if not isinstance(modes,dict) or any(k not in {"douban","imdb","rt"} or v not in {"automatic","manual"} for k,v in modes.items()):
            raise ValueError("评分模式无效")
        for key,mode in modes.items():
            if mode == "automatic":cleaned.pop(key,None)
            elif not cleaned.get(key):raise ValueError("人工固定模式必须填写评分")
    for key,formatter in (("douban",_format_douban),("imdb",_format_imdb),("rt",_format_rt)):
        if cleaned.get(key) and formatter(cleaned[key]) is None:raise ValueError("评分数值无效")
    if cleaned.get("douban_id") and not re.fullmatch(r"[1-9][0-9]*", cleaned["douban_id"]):
        raise ValueError("豆瓣 ID 必须为有效数字 ID")
    if cleaned.get("imdb_id") and not re.fullmatch(r"tt[0-9]+", cleaned["imdb_id"], re.I):
        raise ValueError("IMDb ID 必须为 tt 开头的 ID")
    if cleaned:
        if any(key in values for key in ("douban","imdb","rt")):
            cleaned["verified_at"] = current_time_iso()
        elif previous.get("verified_at"):
            cleaned["verified_at"] = previous["verified_at"]
        overrides[str(item_id)] = cleaned
    else:
        overrides.pop(str(item_id), None)
    payload = {"items": overrides}
    atomic_write_text(RATING_OVERRIDES_FILE, json.dumps(payload, ensure_ascii=False, indent=2))
    global _RATING_OVERRIDES_MTIME, _RATING_OVERRIDES_CACHE
    _RATING_OVERRIDES_CACHE = overrides
    try:
        _RATING_OVERRIDES_MTIME = RATING_OVERRIDES_FILE.stat().st_mtime
    except OSError:
        _RATING_OVERRIDES_MTIME = -1.0
    return cleaned


def _scan_is_busy() -> bool:
    status = str(scan_status_snapshot().get("status") or "")
    return SCAN_RUN_LOCK.locked() or status in {"queued", "loading_library", "scanning", "rendering_item"}


def _start_scan_thread() -> bool:
    with SCAN_START_LOCK:
        if _scan_is_busy():
            return False
        update_scan_status(status="queued", queued_at=current_time_iso(), current_item="等待扫描线程启动", last_error=None)
        # Manual scans always run: the operator explicitly asked for one.
        thread = threading.Thread(target=run_scan_once, kwargs={"force": True}, name="poster-scan-manual", daemon=True)
        thread.start()
        return True


WORK_QUEUE = None
WORK_QUEUE_INIT_LOCK = threading.Lock()


def _work_queue():
    global WORK_QUEUE
    with WORK_QUEUE_INIT_LOCK:
        if WORK_QUEUE is None:
            WORK_QUEUE = render_queue.RenderQueue(RENDER_QUEUE_FILE, SCAN_RUN_LOCK, _execute_item_job, _invalidate_matching_queue)
        return WORK_QUEUE


def _enqueue_item(item, action='render', refresh=False):
    queue = _work_queue()
    job = queue.enqueue(str(item['Id']), _queue_item_name(item), action=action, refresh=refresh,
                        context={'identity': _work_identity(item), 'cache_stamp': _douban_cache_result(item).get('cache_updated_at')})
    queue.start()
    _invalidate_matching_queue()
    return job


def _start_item_render(item: dict[str, Any]) -> bool:
    if not item.get('Id'):
        return False
    _enqueue_item(item)
    return True


def _execute_item_job(job):
    item_id = job['item_id']
    item = _get_item_by_id(item_id)
    if not item:
        raise ValueError('资源已删除或 Emby 暂不可用，请检查后重试')
    started = time.monotonic()
    update_scan_status(status='rendering_item', started_at=current_time_iso(), finished_at=None,
                       total=1, processed=0, current_item=item.get('Name'), last_error=None)
    try:
        if job['action'] == 'restore':
            original = CACHE_DIR / 'original' / f'{item_id}.jpg'
            if not original.is_file():
                raise ValueError('没有可恢复的原图缓存')
            if DRY_RUN:
                LOG.info('dry-run: would restore the original image for %s; Emby untouched', item_id)
                result = 'dry-run'
            else:
                if IMAGE_DELIVERY_MODE == 'upload':
                    upload(item_id, original)
                else:
                    set_image_url(item_id, original, public_name=f'original/{quote(item_id)}.jpg')
                result = 'restored'
        else:
            if job.get('refresh'):
                state = _safe_item_state(item_id)
                state['rating_checked_at'] = {source: 0 for source in ('豆瓣', 'IMDb', 'RT')}
                state['ratings_checked_at'] = 0
                atomic_write_text(CACHE_DIR / 'state' / f'{item_id}.json', json.dumps(state, ensure_ascii=False))
            result = process(item)
            # "ignored" means the library is outside the configured scope and
            # "skipped" means Emby has no source image. Neither is a job
            # failure, so a manual render or a webhook for such an item must
            # finish quietly instead of raising.
            if result not in {'updated', 'unchanged', 'skipped', 'ignored', 'preview', 'dry-run'}:
                raise ValueError('资源尚无可用原海报或媒体信息，未能更新封面；请补齐后重试')
        update_scan_status(status='ok', finished_at=current_time_iso(), processed=1, current_item=None,
                           elapsed_seconds=round(time.monotonic() - started, 1))
        return result
    except Exception as exc:
        update_scan_status(status='failed', finished_at=current_time_iso(), processed=1, current_item=None,
                           last_error=str(exc), elapsed_seconds=round(time.monotonic() - started, 1))
        LOG.exception('queued task failed for %s', item_id)
        raise


def _bulk_enqueue(raw_ids: Any, action: str) -> dict[str, Any]:
    """Queue several items for the serial worker; never touches Emby in parallel."""
    action = str(action or "render").strip().lower()
    if action not in {"render", "refresh", "restore"}:
        raise ValueError("不支持的批量操作")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ValueError("请先选择条目")
    # Refresh hits the rating providers, so it gets the tighter cap.
    limit = 100 if action == "refresh" else 200
    cleaned: list[str] = []
    for raw in raw_ids[:limit]:
        item_id = str(raw).strip()
        if re.fullmatch(r"[A-Za-z0-9_-]+", item_id):
            cleaned.append(item_id)
    queued = 0
    skipped = 0
    for item_id in dict.fromkeys(cleaned):
        item = _get_item_by_id(item_id)
        if not item:
            skipped += 1
            continue
        _enqueue_item(item, action="restore" if action == "restore" else "render",
                      refresh=(action == "refresh"))
        queued += 1
    _invalidate_matching_queue()
    return {"ok": True, "queued": queued, "skipped": skipped, "action": action,
            "requested": len(raw_ids)}


def _task_state(item_id):
    return _work_queue().latest_for(item_id)


def _work_identity(item):
    effective = _effective_item_type(item)
    ids = media_match.effective_ids(effective, _rating_override(effective))
    return hashlib.sha256(json.dumps({'ids': ids, 'type': effective.get('Type'),
                                      'name': item.get('Name'), 'year': item.get('ProductionYear')}, sort_keys=True).encode()).hexdigest()


def _queue_item_name(item, lookup=None):
    if item.get('Type') != 'Season':
        return item.get('Name') or '未命名作品'
    parent_id = str(item.get('SeriesId') or item.get('ParentId') or '')
    parent = (lookup or {}).get(parent_id) or {}
    name = item.get('SeriesName') or parent.get('Name')
    if not name:
        path = str(item.get('Path') or '').replace('\\', '/')
        name = re.sub(r'\s*\((?:19|20)[0-9]{2}\)$', '', path.rstrip('/').split('/')[-2]) if '/' in path.rstrip('/') else '所属剧集未记录'
    season = '特别篇' if item.get('IndexNumber') == 0 else item.get('Name') or '季'
    return f'{name} · {season}'


def _reconcile_cache_jobs():
    """Local-cache follow-up after explicit work; never scrape or rebind media."""
    queue = _work_queue()
    receipts = queue.latest_items()
    overrides = _load_rating_overrides()
    eligible = set(receipts) | {str(k) for k,v in overrides.items() if v.get('match')}
    if not eligible:
        return 0
    items = {str(i['Id']): i for i in get_items_cached(timeout=ADMIN_API_TIMEOUT)}
    queued = 0
    for item_id in eligible:
        item = items.get(item_id)
        if not item:
            continue
        item = _effective_item_type(item)
        latest = receipts.get(item_id) or {}
        if latest.get('status') in {'queued','running'} or latest.get('action') == 'restore':
            continue
        result = _douban_cache_result(item)
        state = _safe_item_state(item_id)
        manual = _rating_override(item).get('douban')
        if manual or result.get('status') != 'ok' or not community_rating_changed(item, state.get('ratings') or {}, state.get('rating_details') or {}):
            continue
        # Failed attempts remain explicit; do not silently retry forever.
        if latest.get('status') == 'failed':
            continue
        _enqueue_item(item)
        queued += 1
    return queued


def _cache_followup_loop():
    while True:
        try:
            _reconcile_cache_jobs()
        except Exception:
            LOG.exception('cache follow-up check failed')
        time.sleep(30)


def _effective_item_type(item):
    kind = (_rating_override(item).get('match') or {}).get('type')
    return {**item, 'Type': kind} if kind in {'Movie', 'Series'} and item.get('Type') != 'Season' else item


def update_scan_status(**values: Any) -> None:
    with SCAN_STATUS_LOCK:
        SCAN_STATUS.update(values)


def scan_status_snapshot() -> dict[str, Any]:
    with SCAN_STATUS_LOCK:
        result = dict(SCAN_STATUS)
    if WORK_QUEUE is not None:
        snapshot = WORK_QUEUE.snapshot()
        result["task_queue"] = {"revision": snapshot["revision"], "counts": snapshot["counts"]}
    result["connection"] = _connection_status()
    result["dry_run"] = DRY_RUN
    return result


def emby(method: str, path: str, **kwargs: Any) -> requests.Response:
    request_timeout = kwargs.pop("timeout", EMBY_REQUEST_TIMEOUT)
    response = SESSION.request(method, f"{EMBY_URL}{path}", timeout=request_timeout, **kwargs)
    response.raise_for_status()
    return response


def number(value: Any) -> float | None:
    if value is None:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)", str(value))
    return float(match.group(1)) if match else None


def provider_id(provider_ids: Any, name: str) -> str | None:
    """Return an Emby provider id without depending on key capitalization."""
    if not isinstance(provider_ids, dict):
        return None
    wanted = name.casefold()
    for key, value in provider_ids.items():
        if str(key).casefold() == wanted and value:
            return str(value)
    return None


def image_url(item_id: str) -> str:
    return f"{EMBY_URL}/Items/{item_id}/Images/Primary?quality=95"


def is_worker_rendered_image(data: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as image:
            comment = image.info.get("comment", b"")
            if isinstance(comment, str):
                comment = comment.encode("utf-8", errors="ignore")
            return WORKER_IMAGE_MARKER.encode("ascii") in comment
    except (OSError, ValueError):
        return False


def get_original(item_id: str, refresh: bool = False, fallback_item_id: str | None = None) -> Path:
    """Fetch the clean source image used as the poster base.

    Emby answers a Season that owns no Primary image with 404, or with 500
    (a NullReferenceException inside ImageService.GetImage) right after a
    restart. Such a season still deserves a poster, so fall back to the parent
    series' clean original rather than failing the whole item.
    """
    target = CACHE_DIR / "original" / f"{item_id}.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    if refresh or not target.exists():
        response = SESSION.get(image_url(item_id), timeout=EMBY_REQUEST_TIMEOUT)
        if response.status_code in {404, 500} and fallback_item_id:
            parent_original = CACHE_DIR / "original" / f"{fallback_item_id}.jpg"
            if not parent_original.exists():
                parent_response = SESSION.get(image_url(fallback_item_id), timeout=EMBY_REQUEST_TIMEOUT)
                if parent_response.status_code == 404:
                    raise FileNotFoundError(
                        f"no Primary image for Emby item {item_id} or its parent {fallback_item_id}")
                parent_response.raise_for_status()
                if is_worker_rendered_image(parent_response.content):
                    raise FileNotFoundError(
                        f"only a Worker render is available for Emby item {item_id} and its parent")
                parent_original.write_bytes(parent_response.content)
            target.write_bytes(parent_original.read_bytes())
            LOG.info("using parent image %s for item %s (Emby reported no own image)", fallback_item_id, item_id)
            return target
        if response.status_code == 404:
            raise FileNotFoundError(f"no Primary image for Emby item {item_id}")
        response.raise_for_status()
        if is_worker_rendered_image(response.content):
            message = f"Emby Primary image for {item_id} is already a Worker render, but no clean original is available"
            if target.exists():
                LOG.warning("%s; keeping cached original", message)
                return target
            raise FileNotFoundError(message)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            temporary.write_bytes(response.content)
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return target


def first_stream(item: dict[str, Any], stream_type: str) -> dict[str, Any]:
    sources = item.get("MediaSources") or []
    streams: list[dict[str, Any]] = []
    for source in sources:
        streams.extend(source.get("MediaStreams") or [])
    if not streams:
        streams = item.get("MediaStreams") or []
    candidates = [x for x in streams if x.get("Type") == stream_type]
    if stream_type == "Video":
        return max(candidates, key=lambda x: int(x.get("Width") or 0) * int(x.get("Height") or 0), default={})
    if stream_type == "Audio":
        return next((x for x in candidates if "atmos" in str(x).lower()), candidates[0] if candidates else {})
    return candidates[0] if candidates else {}


def _series_media_signature(item: dict[str, Any]) -> str:
    payload = {
        "id": item.get("Id"),
        "aggregation_version": 2,
        "date_modified": item.get("DateModified"),
        "date_last_media_added": item.get("DateLastMediaAdded"),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


SERIES_MEDIA_SOURCE_LIMIT = max(4, int(env("SERIES_MEDIA_SOURCE_LIMIT", "12")))


def _stream_key(stream: dict[str, Any]) -> tuple:
    """Identity of a stream as far as badges are concerned."""
    return (
        str(stream.get("Type") or "").lower(),
        int(stream.get("Width") or 0),
        int(stream.get("Height") or 0),
        str(stream.get("VideoRangeType") or stream.get("VideoRange") or "").lower(),
        str(stream.get("Codec") or "").lower(),
        str(stream.get("Profile") or "").lower(),
        str(stream.get("ChannelLayout") or "").lower(),
    )


def _is_immersive_audio(stream: dict[str, Any]) -> bool:
    text = " ".join(str(stream.get(key) or "") for key in
                    ("Codec", "Profile", "DisplayTitle", "Title", "ChannelLayout")).lower()
    return "atmos" in text or "dts:x" in text or "dts-x" in text


def _compact_series_media(media_sources: list[dict[str, Any]],
                          media_streams: list[dict[str, Any]],
                          limit: int | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collapse a series' episodes into the stream layouts that can change a badge.

    A 15,000-episode series repeats the same handful of layouts, while badges
    only read the best video and the first Atmos/DTS:X audio. Keeping every
    episode's full MediaSources costs gigabytes and seconds of JSON parsing
    without changing a single pixel.
    """
    limit = max(4, int(limit or SERIES_MEDIA_SOURCE_LIMIT))
    kept: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    best_source: dict[str, Any] | None = None
    best_area = -1
    immersive_source: dict[str, Any] | None = None

    valid = [source for source in media_sources if isinstance(source, dict)]
    # First pass: find the two sources badge selection depends on, wherever they are.
    for source in valid:
        streams = [stream for stream in (source.get("MediaStreams") or []) if isinstance(stream, dict)]
        videos = [stream for stream in streams if str(stream.get("Type") or "").lower() == "video"]
        audios = [stream for stream in streams if str(stream.get("Type") or "").lower() == "audio"]
        video = max(videos, key=lambda stream: int(stream.get("Width") or 0) * int(stream.get("Height") or 0),
                    default={})
        area = int(video.get("Width") or 0) * int(video.get("Height") or 0)
        if area > best_area:
            best_area, best_source = area, source
        if immersive_source is None and any(_is_immersive_audio(stream) for stream in audios):
            immersive_source = source

    # Second pass: keep one source per distinct layout, capped at the limit.
    for source in valid:
        streams = [stream for stream in (source.get("MediaStreams") or []) if isinstance(stream, dict)]
        key = tuple(sorted({_stream_key(stream) for stream in streams}))
        if key in seen:
            continue
        seen.add(key)
        kept.append(source)
        if len(kept) >= limit:
            break

    # The two sources that actually drive badge selection must always survive.
    for must in (immersive_source, best_source):
        if must is not None and not any(must is item for item in kept):
            kept.insert(0, must)
    kept = kept[:limit]

    kept_streams: list[dict[str, Any]] = []
    stream_seen: set[tuple] = set()
    for stream in media_streams:
        if not isinstance(stream, dict):
            continue
        key = _stream_key(stream)
        if key in stream_seen:
            continue
        stream_seen.add(key)
        kept_streams.append(stream)
        if len(kept_streams) >= limit * 3:
            break
    return kept, kept_streams


def _read_series_media_cache(item: dict[str, Any]) -> dict[str, Any] | None:
    cache_file = CACHE_DIR / "series-media" / f"{item.get('Id')}.json"
    if not cache_file.exists():
        return None
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        if payload.get("signature") != _series_media_signature(item):
            return None
        if time.time() - float(payload.get("fetched_at", 0) or 0) > SERIES_MEDIA_CACHE_DAYS * 86400:
            return None
        if not isinstance(payload.get("media_sources"), list) or not isinstance(payload.get("media_streams"), list):
            return None
        if len(payload["media_sources"]) > SERIES_MEDIA_SOURCE_LIMIT:
            # One-time migration of caches written before compaction existed.
            sources, streams = _compact_series_media(payload["media_sources"], payload["media_streams"])
            payload["media_sources"], payload["media_streams"] = sources, streams
            _write_series_media_cache(item, sources, streams, int(payload.get("episode_count") or 0),
                                      fetched_at=payload.get("fetched_at"))
        return payload
    except (OSError, ValueError, TypeError):
        return None


def _write_series_media_cache(item: dict[str, Any], media_sources: list[dict[str, Any]],
                              media_streams: list[dict[str, Any]], episode_count: int,
                              fetched_at: float | None = None) -> None:
    cache_file = CACHE_DIR / "series-media" / f"{item.get('Id')}.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    sources, streams = _compact_series_media(media_sources, media_streams)
    atomic_write_text(cache_file, json.dumps({
        "signature": _series_media_signature(item),
        "fetched_at": fetched_at or time.time(),
        "episode_count": episode_count,
        "media_sources": sources,
        "media_streams": streams,
    }, ensure_ascii=False))


def enrich_series(item: dict[str, Any]) -> dict[str, Any]:
    """Aggregate only episodes belonging to this series; never cache a broad Emby response."""
    cached = _read_series_media_cache(item)
    if cached is not None:
        enriched = dict(item)
        enriched["MediaSources"] = cached["media_sources"]
        enriched["MediaStreams"] = cached["media_streams"]
        enriched["_SeriesEpisodeCount"] = cached.get("episode_count", 0)
        return enriched

    media_sources: list[dict[str, Any]] = []
    media_streams: list[dict[str, Any]] = []
    episode_count = 0
    start_index = 0
    page_size = 500
    lookup_succeeded = False
    try:
        while True:
            payload = emby("GET", "/Items", params={
                # ParentId is singular on this Emby endpoint. ParentIds is
                # accepted but ignored by this server, returning episodes from
                # the entire library and contaminating quality badges.
                "ParentId": str(item["Id"]),
                "Recursive": "true",
                "IncludeItemTypes": "Episode",
                "Fields": "MediaSources,MediaStreams,Path,SeriesId,SeasonId",
                "StartIndex": start_index,
                "Limit": page_size,
            }).json()
            episodes = payload.get("Items") or []
            if not isinstance(episodes, list):
                raise ValueError("Emby returned an invalid episode list")
            owner_field = "SeasonId" if item.get("Type") == "Season" else "SeriesId"
            foreign = [episode for episode in episodes
                       if episode.get(owner_field) and str(episode[owner_field]) != str(item["Id"])]
            if foreign:
                raise ValueError("Emby returned episodes from another series")
            episode_count += len(episodes)
            for episode in episodes:
                media_sources.extend(episode.get("MediaSources") or [])
                media_streams.extend(episode.get("MediaStreams") or [])
            total = int(payload.get("TotalRecordCount") or 0)
            if not episodes or len(episodes) < page_size or (total and start_index + len(episodes) >= total):
                break
            start_index += len(episodes)
        lookup_succeeded = True
    except (requests.RequestException, ValueError, TypeError) as exc:
        LOG.warning("series media lookup failed for %s: %s", item.get("Name"), exc)
        # Old aggregation caches may contain another series. Never fall back
        # to them if they lack the new scope signature.
        cache_file = CACHE_DIR / "series-media" / f"{item.get('Id')}.json"
        if cache_file.exists():
            try:
                stale = json.loads(cache_file.read_text(encoding="utf-8"))
                if stale.get("signature") != _series_media_signature(item):
                    return item
                media_sources = stale.get("media_sources") or []
                media_streams = stale.get("media_streams") or []
                episode_count = int(stale.get("episode_count") or 0)
            except (OSError, ValueError, TypeError):
                return item
        else:
            return item

    if lookup_succeeded:
        _write_series_media_cache(item, media_sources, media_streams, episode_count)
    enriched = dict(item)
    enriched["MediaSources"] = media_sources
    enriched["MediaStreams"] = media_streams
    enriched["_SeriesEpisodeCount"] = episode_count
    return enriched

from badges import (  # noqa: E402  - split out of the monolith
    _audio_badge,
    _badge_audio,
    _badge_is_high_value,
    _badge_path_text,
    _badge_streams,
    _badge_video,
    _hdr_badge,
    _hdr_sub_label,
    _resolution_badge,
    _source_badge,
    quality_badges as _quality_badges,
)


def quality_badges(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Badges for one item using the live admin render settings."""
    return _quality_badges(item, RENDER_SETTINGS)



def quality_labels(item: dict[str, Any]) -> list[str]:
    return [badge["label"] for badge in quality_badges(item)]

def media_health(item: dict[str, Any]) -> dict[str, Any]:
    """Return actionable media-library quality checks for one item."""
    issues: list[dict[str, str]] = []
    item_type = str(item.get("Type") or "")
    if not (item.get("ImageTags") or {}).get("Primary"):
        issues.append({"code": "missing_primary_image", "severity": "warning", "message": "missing Primary image"})
    video = first_stream(item, "Video")
    audio = first_stream(item, "Audio")
    streams = []
    for source in item.get("MediaSources") or []:
        streams.extend(source.get("MediaStreams") or [])
    streams.extend(item.get("MediaStreams") or [])
    path_text = " ".join(
        [str(item.get(key, "")) for key in ("Name", "FileName", "Path")]
        + [str(source.get(key, "")) for source in item.get("MediaSources") or [] for key in ("Name", "Path", "Container")]
    )
    path_lower = path_text.casefold()
    quality = quality_labels(item)
    width = int(video.get("Width") or 0)
    height = int(video.get("Height") or 0)
    if width >= 3000 or height >= 2000:
        actual_resolution = "4K"
    elif width >= 1800 or height >= 1000:
        actual_resolution = "1080P"
    elif width >= 1200 or height >= 700:
        actual_resolution = "720P"
    elif width >= 700 or height >= 400:
        actual_resolution = "576P" if height >= 560 else "480P"
    elif width or height:
        actual_resolution = "SD"
    else:
        actual_resolution = None
    declared_resolution = None
    if re.search(r"(?:2160p|2160i|4k|uhd|ultra[ ._-]?hd)", path_lower):
        declared_resolution = "4K"
    elif re.search(r"(?:1080p|1080i|fhd)", path_lower):
        declared_resolution = "1080P"
    elif re.search(r"(?:720p|720i)", path_lower):
        declared_resolution = "720P"
    elif re.search(r"(?:576p|576i)", path_lower):
        declared_resolution = "576P"
    elif re.search(r"(?:480p|480i)", path_lower):
        declared_resolution = "480P"
    if declared_resolution and actual_resolution and declared_resolution != actual_resolution:
        issues.append({"code": "resolution_mismatch", "severity": "warning", "message": f"filename suggests {declared_resolution}, stream resolves as {actual_resolution}"})
    declared_hdr = bool(re.search(r"hdr|dolby[ ._-]?vision|(?:^|[^a-z])dv(?:[^a-z]|$)", path_lower))
    stream_text = " ".join(str(video.get(key, "")) for key in ("VideoRange", "VideoRangeType", "ColorTransfer", "DisplayTitle", "Codec")).casefold()
    actual_hdr = bool(re.search(r"hdr|pq|smpte2084|dolby[ ._-]?vision|(?:^|[^a-z])dv(?:[^a-z]|$)", stream_text))
    if declared_hdr and not actual_hdr:
        issues.append({"code": "hdr_mismatch", "severity": "warning", "message": "filename suggests HDR/Dolby Vision, but video stream does not confirm it"})
    # Emby does not always return decoded stream metadata for STRM, ISO and
    # remote-mounted sources. A usable MediaSource/Path means the media exists;
    # absence of decoded streams is then a metadata limitation, not a broken file.
    media_source_available = bool(
        item.get("Path")
        or any(str(source.get("Path") or source.get("Container") or "").strip() for source in item.get("MediaSources") or [])
    )
    stream_metadata_available = bool(streams)
    if item_type in {"Movie", "Series"} and not video and not media_source_available and stream_metadata_available:
        issues.append({"code": "missing_video_stream", "severity": "error", "message": "Emby returned media metadata without a video stream"})
    if item_type in {"Movie", "Series"} and not audio and not media_source_available and stream_metadata_available:
        issues.append({"code": "missing_audio_stream", "severity": "warning", "message": "Emby returned media metadata without an audio stream"})
    subtitle_streams = [stream for stream in streams if str(stream.get("Type") or "").casefold() == "subtitle"]
    chinese_subtitle = any(
        re.search(r"中文|简中|繁中|中字|chinese|zh[-_]?hans|zh[-_]?hant|\bchi\b", " ".join(str(stream.get(key, "")) for key in ("Language", "DisplayTitle", "Title")), re.I)
        for stream in subtitle_streams
    )
    if subtitle_streams and not chinese_subtitle:
        issues.append({"code": "no_chinese_subtitle", "severity": "info", "message": "subtitle streams exist, but no Chinese subtitle was identified"})
    localized_messages = {
        "missing_primary_image": "缺少主海报图片",
        "missing_video_stream": "Emby 没有返回视频流详情，但不一定代表资源无法播放",
        "missing_audio_stream": "Emby 没有返回音频流详情，但不一定代表资源无法播放",
        "resolution_mismatch": "文件名标注的分辨率与媒体信息不一致",
        "hdr_mismatch": "文件名标注了 HDR / 杜比视界，但媒体流没有确认",
        "no_chinese_subtitle": "检测到字幕，但没有识别到中文字幕",
    }
    for issue in issues:
        issue["message_zh"] = localized_messages.get(str(issue.get("code")), str(issue.get("message") or "未知问题"))
    return {
        "id": str(item.get("Id") or ""), "name": item.get("Name"), "type": item_type,
        "quality": quality, "video_codec": video.get("Codec") or video.get("DisplayTitle") or None,
        "audio_codec": audio.get("Codec") or audio.get("DisplayTitle") or None,
        "subtitle_count": len(subtitle_streams), "issues": issues,
    }


def _normalized_title(value: Any) -> str:
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())
    return text


_RATING_OVERRIDES_CACHE: dict[str, Any] = {}
_RATING_OVERRIDES_MTIME = -1.0


def _omdb_status(payload: dict[str, Any] | None) -> str:
    if not payload:
        return "lookup_error"
    worker_status = payload.get("_worker_status")
    if worker_status:
        return str(worker_status)
    if payload.get("Response") == "False":
        error = str(payload.get("Error") or "").casefold()
        if "invalid api key" in error:
            return "invalid_api_key"
        if "too many requests" in error or "rate" in error and "limit" in error:
            return "rate_limited"
        return "no_result"
    return "ok"


def _omdb_circuit_status() -> str | None:
    with OMDB_RATE_LOCK:
        if time.time() < OMDB_CIRCUIT_OPEN_UNTIL:
            return "circuit_open"
    return None


def _omdb_record_result(status: str) -> None:
    global OMDB_CONSECUTIVE_FAILURES, OMDB_CIRCUIT_OPEN_UNTIL
    with OMDB_RATE_LOCK:
        if status in {"rate_limited", "lookup_error", "invalid_payload", "http_error"}:
            OMDB_CONSECUTIVE_FAILURES += 1
            if OMDB_CONSECUTIVE_FAILURES >= 5:
                OMDB_CIRCUIT_OPEN_UNTIL = time.time() + OMDB_CIRCUIT_BREAK_SECONDS
                LOG.warning("OMDb circuit opened for %ss after repeated failures", OMDB_CIRCUIT_BREAK_SECONDS)
        else:
            OMDB_CONSECUTIVE_FAILURES = 0
            OMDB_CIRCUIT_OPEN_UNTIL = 0.0


def _omdb_wait_for_slot() -> None:
    global OMDB_NEXT_REQUEST_AT
    with OMDB_RATE_LOCK:
        delay = max(0.0, OMDB_NEXT_REQUEST_AT - time.time())
        if delay:
            time.sleep(delay)
        OMDB_NEXT_REQUEST_AT = time.time() + OMDB_MIN_INTERVAL_SECONDS


def _omdb_interactive_request(cache_key,params):
    pool=list(dict.fromkeys([OMDB_API_KEY]+list(OMDB_API_KEYS)))
    pool=[key for key in pool if key]
    if not pool:return {'Response':'False','_worker_status':'no_api_key'}
    path=CACHE_DIR/'omdb'/(cache_key+'.json')
    cached=_load_json_file(path,{})
    if cached and path.is_file():
        status=_omdb_status(cached)
        ttl=OMDB_CACHE_DAYS*86400 if status=='ok' else 30 if status in {'lookup_error','http_error','invalid_api_key','rate_limited'} else 120
        if time.time()-path.stat().st_mtime<ttl:return cached
    last={'Response':'False','_worker_status':'lookup_error'}
    for key in pool[:2]:
        try:
            _omdb_wait_for_slot()
            response=SESSION.get('https://www.omdbapi.com/',params={'apikey':key,**params},timeout=6)
            data=response.json()
            if not isinstance(data,dict):data={'Response':'False','_worker_status':'invalid_payload'}
            if response.status_code==429:data['_worker_status']='rate_limited'
            elif response.status_code in {401,403}:data['_worker_status']='invalid_api_key'
            elif response.status_code>=400:data['_worker_status']='http_error'
            last=data
            if _omdb_status(data) not in {'invalid_api_key','rate_limited','http_error','lookup_error','invalid_payload'}:break
        except (requests.RequestException,ValueError):last={'Response':'False','_worker_status':'lookup_error'}
    atomic_write_text(path,json.dumps(last,ensure_ascii=False))
    return last


def _omdb_cached_request(cache_key: str, params: dict[str, str], interactive: bool = False) -> dict[str, Any] | None:
    if interactive:
        return _omdb_interactive_request(cache_key,params)
    if not OMDB_API_KEY:
        return {"Response": "False", "Error": "OMDb API key is not configured", "_worker_status": "no_api_key"}
    cache_file = CACHE_DIR / "omdb" / f"{cache_key}.json"
    payload: dict[str, Any] | None = None
    cache_ttl = OMDB_CACHE_DAYS * 86400
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("Response") == "False":
                cache_ttl = OMDB_ERROR_CACHE_MINUTES * 60
            if time.time() - cache_file.stat().st_mtime < cache_ttl:
                payload = cached
        except (OSError, ValueError):
            payload = None
    if payload is not None:
        return payload
    circuit_status = _omdb_circuit_status()
    if circuit_status:
        return {"Response": "False", "Error": "OMDb circuit breaker is open", "_worker_status": circuit_status}

    last_payload: dict[str, Any] = {"Response": "False", "Error": "OMDb lookup failed", "_worker_status": "lookup_error"}
    for attempt in range((0 if interactive else OMDB_RETRY_COUNT) + 1):
        try:
            _omdb_wait_for_slot()
            response = SESSION.get("https://www.omdbapi.com/", params={"apikey": OMDB_API_KEY, **params}, timeout=12 if interactive else 30)
            try:
                payload = response.json()
            except ValueError:
                payload = {"Response": "False", "Error": "OMDb returned invalid JSON", "_worker_status": "invalid_payload"}
            if response.status_code == 401:
                payload.update({"Response": "False", "_worker_status": "invalid_api_key"})
            elif response.status_code == 429:
                payload.update({"Response": "False", "_worker_status": "rate_limited"})
            elif response.status_code >= 500:
                payload.update({"Response": "False", "_worker_status": "http_error"})
            elif response.status_code >= 400:
                payload.update({"Response": "False", "_worker_status": "http_error"})
            last_payload = payload
            status = _omdb_status(payload)
            if status in {"rate_limited", "lookup_error", "invalid_payload", "http_error"} and not interactive and attempt < OMDB_RETRY_COUNT:
                time.sleep(OMDB_BACKOFF_SECONDS * (2 ** attempt))
                continue
            atomic_write_text(cache_file, json.dumps(payload, ensure_ascii=False))
            _omdb_record_result(status)
            if response.status_code >= 400 or status not in {"ok", "no_result"}:
                LOG.warning("OMDb lookup failed for %s: HTTP %s (%s)", cache_key, response.status_code, status)
            return payload
        except (OSError, requests.RequestException, ValueError) as exc:
            last_payload = {"Response": "False", "Error": str(exc), "_worker_status": "lookup_error"}
            if attempt < OMDB_RETRY_COUNT:
                time.sleep(OMDB_BACKOFF_SECONDS * (2 ** attempt))
                continue
            LOG.warning("OMDb lookup failed for %s: %s", cache_key, exc)
    _omdb_record_result(_omdb_status(last_payload))
    atomic_write_text(cache_file, json.dumps(last_payload, ensure_ascii=False))
    return last_payload


def _mdblist_status(payload: dict[str, Any] | None) -> str:
    if not payload:
        return "lookup_error"
    worker_status = payload.get("_worker_status")
    if worker_status:
        return str(worker_status)
    error = str(payload.get("error") or payload.get("Error") or "").casefold()
    if error:
        if "invalid" in error or "unauthorized" in error or "authentication" in error:
            return "invalid_api_key"
        if "limit" in error or "rate" in error or "too many" in error:
            return "rate_limited"
        return "lookup_error"
    return "ok"


def _mdblist_cache_file(media_type: str, imdb_id: str) -> Path:
    safe_type = "show" if media_type == "show" else "movie"
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", imdb_id)
    return CACHE_DIR / "mdblist" / f"{safe_type}-{safe_id}.json"


def _mdblist_wait_for_slot() -> None:
    global MDBLIST_NEXT_REQUEST_AT
    with MDBLIST_RATE_LOCK:
        delay = max(0.0, MDBLIST_NEXT_REQUEST_AT - time.time())
        if delay:
            time.sleep(delay)
        MDBLIST_NEXT_REQUEST_AT = time.time() + MDBLIST_MIN_INTERVAL_SECONDS


def _mdblist_request(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """Call MDBList with per-key cooldown and failover.

    Keys are rotated only for authentication/rate-limit failures. A missing title
    or missing rating is a content result and must not burn another key.
    """
    global MDBLIST_KEY_INDEX, MDBLIST_KEY_POOL_SIGNATURE, MDBLIST_KEY_COOLDOWN_UNTIL
    pool_signature = "|".join(_api_key_fingerprint(key) for key in MDBLIST_API_KEYS)
    if pool_signature != MDBLIST_KEY_POOL_SIGNATURE:
        MDBLIST_KEY_POOL_SIGNATURE = pool_signature
        MDBLIST_KEY_INDEX = 0
        MDBLIST_KEY_COOLDOWN_UNTIL = {}
    if not MDBLIST_API_KEYS:
        return {"_worker_status": "no_api_key", "error": "MDBList API key is not configured"}

    now = time.time()
    with MDBLIST_RATE_LOCK:
        count = len(MDBLIST_API_KEYS)
        ordered = [(MDBLIST_KEY_INDEX + offset) % count for offset in range(count)]
        available = [index for index in ordered if MDBLIST_KEY_COOLDOWN_UNTIL.get(index, 0.0) <= now]
    if not available:
        retry_at = min(MDBLIST_KEY_COOLDOWN_UNTIL.values(), default=now + 60)
        return {"_worker_status": "rate_limited", "error": "all MDBList API keys are cooling down", "retry_after": max(1, int(retry_at - now))}

    last_payload: dict[str, Any] = {"_worker_status": "lookup_error", "error": "MDBList lookup failed"}
    for key_index in available:
        key = MDBLIST_API_KEYS[key_index]
        for attempt in range(MDBLIST_RETRY_COUNT + 1):
            try:
                _mdblist_wait_for_slot()
                response = MDBLIST_SESSION.post(
                    f"{MDBLIST_API_BASE}{path}",
                    params={"apikey": key},
                    json=body,
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    timeout=30,
                )
                try:
                    payload = response.json()
                except ValueError:
                    payload = {"_worker_status": "invalid_payload", "error": "MDBList returned invalid JSON"}
                if not isinstance(payload, dict):
                    payload = {"_worker_status": "invalid_payload", "error": "MDBList returned a non-object response"}
                if response.status_code in {401, 403}:
                    payload.update({"_worker_status": "invalid_api_key"})
                    MDBLIST_KEY_COOLDOWN_UNTIL[key_index] = time.time() + 86400
                elif response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", "300") or 300)
                    payload.update({"_worker_status": "rate_limited", "retry_after": retry_after})
                    MDBLIST_KEY_COOLDOWN_UNTIL[key_index] = time.time() + max(60, retry_after)
                elif response.status_code >= 500:
                    payload.update({"_worker_status": "http_error"})
                elif response.status_code >= 400:
                    payload.update({"_worker_status": "http_error"})
                status = _mdblist_status(payload)
                last_payload = payload
                if status in {"invalid_api_key", "rate_limited"}:
                    # A bad/limited key must not mask a later configured key.
                    # The account-level cooldown is recorded above; move on to
                    # the next key immediately.
                    break
                if status in {"lookup_error", "invalid_payload", "http_error"} and attempt < MDBLIST_RETRY_COUNT:
                    time.sleep(MDBLIST_BACKOFF_SECONDS * (2 ** attempt))
                    continue
                with MDBLIST_RATE_LOCK:
                    MDBLIST_KEY_INDEX = (key_index + 1) % len(MDBLIST_API_KEYS)
                return payload
            except (OSError, requests.RequestException, ValueError) as exc:
                last_payload = {"_worker_status": "lookup_error", "error": str(exc)}
                if attempt < MDBLIST_RETRY_COUNT:
                    time.sleep(MDBLIST_BACKOFF_SECONDS * (2 ** attempt))
                    continue
                LOG.warning("MDBList lookup failed for %s: %s", path, exc)
        # Try the next configured key after a key-specific failure.
    return last_payload


def _mdblist_entry_id(entry: dict[str, Any]) -> str | None:
    for key in ("provider_id", "providerId", "imdb_id", "imdbId", "id"):
        value = entry.get(key)
        if isinstance(value, dict):
            value = value.get("imdb") or value.get("id")
        if value:
            return str(value)
    ids = entry.get("ids")
    if isinstance(ids, dict) and ids.get("imdb"):
        return str(ids["imdb"])
    return None


def _mdblist_entry_value(entry: dict[str, Any]) -> Any:
    for key in ("provider_rating", "providerRating", "rating", "value", "score"):
        if entry.get(key) is not None:
            return entry.get(key)
    rating = entry.get("ratings")
    if isinstance(rating, dict):
        for key in ("value", "rating", "score"):
            if rating.get(key) is not None:
                return rating.get(key)
    return None


def _mdblist_rating_entries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    if not isinstance(payload, dict):
        return []
    ratings = payload.get("ratings")
    if isinstance(ratings, list):
        return [entry for entry in ratings if isinstance(entry, dict)]
    if _mdblist_entry_id(payload) or _mdblist_entry_value(payload) is not None:
        return [payload]
    return []


def _format_mdblist_rating(source: str, value: Any) -> str | None:
    if source == "imdb":
        return _format_imdb(value)
    if source == "tomatoes":
        return rating_policy.critic_percent(value)
    return None



def _mdblist_write_values(media_type: str, imdb_id: str, source: str, values: dict[str, str], status: str) -> None:
    path = _mdblist_cache_file(media_type, imdb_id)
    current: dict[str, Any] = _load_json_file(path, {})
    if not isinstance(current, dict):
        current = {}
    merged = dict(current.get("values") or {})
    if status == "no_rating":
        # A successful MDBList lookup can legitimately have no rating. Clear a
        # stale/invalid value instead of preserving a provider label such as
        # "imdb" from the response envelope.
        merged.pop("IMDb" if source == "imdb" else "RT", None)
    merged.update(values)
    statuses = dict(current.get("statuses") or {})
    statuses[source] = status
    checked_at = current.get("checked_at")
    if not isinstance(checked_at, dict):
        checked_at = {}
    checked_at[source] = time.time()
    payload = {
        "media_type": media_type,
        "imdb_id": imdb_id,
        "values": merged,
        "statuses": statuses,
        "checked_at": checked_at,
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _mdblist_fetch_batch(media_type: str, source: str, imdb_ids: list[str]) -> dict[str, Any]:
    if not imdb_ids:
        return {"status": "no_ids", "count": 0}
    return_rating = "imdb" if source == "imdb" else "tomatoes"
    payload = _mdblist_request(
        f"/rating/{media_type}/{return_rating}",
        {"ids": imdb_ids, "provider": "imdb"},
    )
    status = _mdblist_status(payload)
    found: dict[str, str] = {}
    entries = _mdblist_rating_entries(payload)
    for entry in entries:
        identifier = _mdblist_entry_id(entry)
        value = _format_mdblist_rating(source, _mdblist_entry_value(entry))
        if identifier and identifier in imdb_ids and value:
            found[identifier] = value
    # A single-ID response may omit the provider id; the request identity is
    # unambiguous in that case.
    if len(imdb_ids) == 1 and not found and status == "ok" and not _mdblist_entry_id(payload):
        value = _format_mdblist_rating(source, _mdblist_entry_value(payload))
        if value:
            found[imdb_ids[0]] = value
    for imdb_id in imdb_ids:
        value = found.get(imdb_id)
        _mdblist_write_values(
            media_type,
            imdb_id,
            source,
            {"IMDb" if source == "imdb" else "RT": value} if value else {},
            "ok" if value else ("no_rating" if status == "ok" else status),
        )
    return {"status": status, "count": len(found), "ids": found}


def _mdblist_cached_values(item: dict[str, Any], imdb_id: str, requested: set[str]) -> tuple[dict[str, str], dict[str, str]]:
    media_type = "show" if item.get("Type") == "Series" else "movie"
    cache_file = _mdblist_cache_file(media_type, imdb_id)
    cached = _load_json_file(cache_file, {})
    cached = cached if isinstance(cached, dict) else {}
    if cached.get("imdb_id") and str(cached["imdb_id"]).casefold() != imdb_id.casefold():
        cached = {}
    if cached.get("media_type") and cached["media_type"] != media_type:
        cached = {}
    values = {str(key): str(value) for key, value in (cached.get("values") or {}).items()}
    values = {key: value for key, value in values.items() if
              (key == "IMDb" and _format_imdb(value) is not None) or
              (key == "RT" and _format_rt(value) is not None)}
    statuses = {str(key): str(value) for key, value in (cached.get("statuses") or {}).items()}
    checked_at_raw = cached.get("checked_at")
    checked_at_by_source = checked_at_raw if isinstance(checked_at_raw, dict) else {}
    legacy_checked_at = float(checked_at_raw or 0) if isinstance(checked_at_raw, (int, float, str)) else 0.0
    ttl = MDBLIST_CACHE_DAYS * 86400
    error_ttl = MDBLIST_ERROR_CACHE_MINUTES * 60
    for display_source, api_source in (("IMDb", "imdb"), ("RT", "tomatoes")):
        if display_source not in requested:
            continue
        prior_status = statuses.get(api_source)
        checked_at = float(checked_at_by_source.get(api_source, legacy_checked_at) or 0)
        source_ttl = error_ttl if prior_status and prior_status != "ok" else ttl
        if checked_at and time.time() - checked_at < source_ttl:
            continue
        result = _mdblist_fetch_batch(media_type, api_source, [imdb_id])
        refreshed = _load_json_file(cache_file, {})
        refreshed = refreshed if isinstance(refreshed, dict) else {}
        values.update({str(key): str(value) for key, value in (refreshed.get("values") or {}).items()})
        statuses.update({str(key): str(value) for key, value in (refreshed.get("statuses") or {}).items()})
        if result.get("status") in {"rate_limited", "invalid_api_key"}:
            break
    return values, statuses


def rating_identity(item: dict[str, Any]) -> dict[str, Any]:
    ids = item.get("ProviderIds") or {}
    return {
        "type": item.get("Type"),
        "douban": provider_id(ids, "Douban"),
        "imdb": provider_id(ids, "Imdb"),
        "override_imdb": _override_value(_rating_override(item), "imdb_id", "imdbid"),
        "override_douban": _override_value(_rating_override(item), "douban_id"),
        **({"confirmed_match": _rating_override(item)["match"]} if _rating_override(item).get("match") else {}),
    }


def rating_identity_matches(previous: Any, current: dict[str, Any]) -> bool:
    """New optional identity keys with no value must not invalidate old state."""
    return (isinstance(previous, dict) and bool(previous)
            and all(previous.get(key) == value for key, value in current.items())
            and all(current.get(key) == value for key, value in previous.items()))


def rating_provider_signature(normalizer_version: int = 5) -> str:
    """Stable non-secret fingerprint; credential changes trigger a one-time refresh."""
    payload = {
        "normalizer_version": normalizer_version,
        "order": list(RATING_EXTERNAL_PROVIDER_ORDER),
        "mdblist": [_api_key_fingerprint(key) for key in MDBLIST_API_KEYS],
        "omdb": [_api_key_fingerprint(key) for key in OMDB_API_KEYS],
        # Changing the Douban trust/fallback policy must invalidate the
        # per-item rating cache; otherwise old no_rating results persist.
        "douban_require_verified_cache": DOUBAN_REQUIRE_VERIFIED_CACHE,
        "community_rating_is_douban": COMMUNITY_RATING_IS_DOUBAN,
        "community_rating_requires_douban_id": COMMUNITY_RATING_REQUIRES_DOUBAN_ID,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:20]


def _rating_prefetch_due(item: dict[str, Any], source: str, now: float) -> bool:
    item_id = str(item.get("Id") or "")
    if not item_id:
        return False
    state = _load_json_file(CACHE_DIR / "state" / f"{item_id}.json", {})
    state = state if isinstance(state, dict) else {}
    details = state.get("rating_details") if isinstance(state.get("rating_details"), dict) else {}
    checked = state.get("rating_checked_at") if isinstance(state.get("rating_checked_at"), dict) else {}
    if state.get("rating_provider_signature") not in {rating_provider_signature(), rating_provider_signature(4)}:
        return True
    if source in {"IMDb", "RT"} and source not in details:
        return True
    if source in checked:
        return now - float(checked.get(source) or 0) >= rating_refresh_seconds()[source]
    return not state.get("ratings")


def prefetch_mdblist_ratings(items: list[dict[str, Any]]) -> dict[str, int]:
    """Warm MDBList's batch cache before item rendering.

    The free MDBList tier accepts batches of ten IDs. A full scan therefore uses
    roughly 1/10 as many requests as per-item lookups and only refreshes sources
    whose worker state is due.
    """
    if not MDBLIST_API_KEYS:
        return {"requested": 0, "found": 0, "failed": 0}
    now = time.time()
    groups: dict[tuple[str, str], set[str]] = {}
    for item in items:
        if item.get("Type") == "Season":
            continue  # Season IMDb/RT are never inferred from series-wide IDs.
        imdb_id = media_match.effective_ids(item,_rating_override(item)).get("imdb")
        if not imdb_id:
            continue
        media_type = "show" if item.get("Type") == "Series" else "movie"
        override = _rating_override(item)
        for display_source, api_source in (("IMDb", "imdb"), ("RT", "tomatoes")):
            if display_source == "RT" and CRITIC_RATING_IS_RT and _format_rt(item.get("CriticRating")) is not None:
                continue
            if _override_value(override, "imdb" if display_source == "IMDb" else "rt", display_source):
                continue
            if _rating_prefetch_due(item, display_source, now):
                groups.setdefault((media_type, api_source), set()).add(str(imdb_id))
    requested = found = failed = 0
    with MDBLIST_PREFETCH_LOCK:
        for (media_type, api_source), ids in sorted(groups.items()):
            id_list = sorted(ids)
            for start in range(0, len(id_list), 10):
                chunk = id_list[start:start + 10]
                result = _mdblist_fetch_batch(media_type, api_source, chunk)
                requested += len(chunk)
                found += int(result.get("count") or 0)
                if result.get("status") not in {"ok", "no_ids"}:
                    failed += len(chunk)
                    if result.get("status") in {"rate_limited", "invalid_api_key"}:
                        break
    if requested:
        LOG.info("MDBList rating prefetch: requested=%d found=%d failed=%d", requested, found, failed)
    return {"requested": requested, "found": found, "failed": failed}

def _should_skip_omdb_title_search(item: dict[str, Any]) -> bool:
    if provider_id(item.get("ProviderIds") or {}, "Imdb"):
        return False
    genres = item.get("Genres") or []
    if isinstance(genres, str):
        genres = [genres]
    text = " ".join(str(value).casefold() for value in genres)
    return bool(text and any(token in text for token in OMDB_SKIP_GENRES))


def _find_omdb_id_info(item: dict[str, Any], override_imdb_id: str | None = None) -> tuple[str | None, str]:
    if item.get("Type") == "Season":
        return None, "season_provider_id_required"
    if override_imdb_id:
        return (str(override_imdb_id), "override") if re.fullmatch(r"tt[0-9]+", str(override_imdb_id), re.I) else (None, "invalid_imdb_id")
    binding = _rating_override(item)
    if binding.get("match") and "imdb_id" in binding and not binding["imdb_id"]:
        return None, "unmatched_provider_id"
    provider_ids = item.get("ProviderIds") or {}
    imdb_id = provider_id(provider_ids, "Imdb")
    if imdb_id:
        return (str(imdb_id), "provider_id") if re.fullmatch(r"tt[0-9]+", str(imdb_id), re.I) else (None, "invalid_imdb_id")
    if item.get("Type") == "Season":
        # Never copy whole-show IMDb/RT ratings onto a season. A distinct
        # provider identifier and season-aware API are needed for those.
        return None, "season_provider_id_required"
    if _should_skip_omdb_title_search(item):
        return None, "policy_skip"
    if not OMDB_API_KEY:
        return None, "no_api_key"
    year = str(item.get("ProductionYear") or "").strip()
    titles: list[str] = []
    for value in (item.get("Name"), item.get("OriginalTitle")):
        title = str(value or "").strip()
        if title and title not in titles:
            titles.append(title)
    if not titles:
        return None, "no_title"
    media_type = "series" if item.get("Type") == "Series" else "movie"
    saw_search_result = False
    for title in titles:
        cache_key = "search-" + hashlib.sha1(f"{title}|{year}|{item.get('Type','')}".encode("utf-8")).hexdigest()
        payload = _omdb_cached_request(cache_key, {"s": title, **({"y": year} if year.isdigit() else {}), "type": media_type})
        status = _omdb_status(payload)
        if status != "ok":
            if status in {"no_api_key", "invalid_api_key", "rate_limited", "lookup_error", "invalid_payload"}:
                return None, status
            continue
        saw_search_result = True
        target = _normalized_title(title)
        # Title search only supplies an ID when the exact title, year and
        # media type agree, and the match is unambiguous. Similarity is a
        # review hint, never sufficient evidence for poster scores.
        if not year.isdigit() or not target:
            continue
        candidates = {
            str(candidate.get("imdbID"))
            for candidate in payload.get("Search", [])
            if isinstance(candidate, dict)
            and candidate.get("Type") == media_type
            and _normalized_title(candidate.get("Title")) == target
            and str(candidate.get("Year") or "") == year
            and re.fullmatch(r"tt[0-9]+", str(candidate.get("imdbID") or ""), re.IGNORECASE)
        }
        if len(candidates) == 1:
            return candidates.pop(), "title_search"
    return None, "no_match" if saw_search_result else "no_result"


def _find_omdb_id(item: dict[str, Any]) -> str | None:
    imdb_id, _ = _find_omdb_id_info(item)
    return imdb_id


def _load_rating_overrides() -> dict[str, Any]:
    global _RATING_OVERRIDES_CACHE, _RATING_OVERRIDES_MTIME
    try:
        mtime = RATING_OVERRIDES_FILE.stat().st_mtime
    except OSError:
        _RATING_OVERRIDES_CACHE = {}
        _RATING_OVERRIDES_MTIME = -1.0
        return _RATING_OVERRIDES_CACHE
    if mtime == _RATING_OVERRIDES_MTIME:
        return _RATING_OVERRIDES_CACHE
    try:
        payload = json.loads(RATING_OVERRIDES_FILE.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("items"), dict):
            payload = payload["items"]
        _RATING_OVERRIDES_CACHE = payload if isinstance(payload, dict) else {}
    except (OSError, ValueError) as exc:
        LOG.warning("rating overrides could not be loaded from %s: %s", RATING_OVERRIDES_FILE, exc)
        _RATING_OVERRIDES_CACHE = {}
    _RATING_OVERRIDES_MTIME = mtime
    return _RATING_OVERRIDES_CACHE


def _rating_override(item: dict[str, Any]) -> dict[str, Any]:
    overrides = _load_rating_overrides()
    item_id = str(item.get("Id") or "")
    name_key = f"{item.get('Name','')}|{item.get('ProductionYear','')}"
    value = overrides.get(item_id) or overrides.get(name_key) or {}
    return value if isinstance(value, dict) else {}


def rating_override_signature(item: dict[str, Any]) -> str:
    override = _rating_override(item)
    if not override:
        return ""
    return hashlib.sha256(json.dumps(override, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _override_value(override: dict[str, Any], *names: str) -> Any:
    folded = {str(key).casefold(): value for key, value in override.items()}
    for name in names:
        if name.casefold() in folded:
            return folded[name.casefold()]
    return None



def _douban_cache_result(item: dict[str, Any]) -> dict[str, Any]:
    memo = getattr(DOUBAN_RESOLVE_CONTEXT, "memo", None)
    if memo is None:
        return _resolve_douban_cache_result(item)
    key = (str(item.get("Id")), json.dumps(item.get("ProviderIds") or {}, sort_keys=True), str(item.get("Type")))
    if key not in memo:
        memo[key] = _resolve_douban_cache_result(item)
    return memo[key]


def _resolve_douban_cache_result(item: dict[str, Any]) -> dict[str, Any]:
    item = _effective_item_type(item)
    override = _rating_override(item)
    douban_id = _override_value(override, "douban_id")
    imdb_id = _override_value(override, "imdb_id", "imdbid")
    if douban_id or imdb_id or override.get("match"):
        replaced = {key for key, value in (("douban", douban_id), ("imdb", imdb_id)) if value or override.get("match") and key + "_id" in override}
        ids = {key: value for key, value in (item.get("ProviderIds") or {}).items() if str(key).casefold() not in replaced}
        if douban_id:
            ids["Douban"] = str(douban_id)
        if imdb_id:
            ids["Imdb"] = str(imdb_id)
        item = {**item, "ProviderIds": ids}
    if item.get("Type") == "Season" and not provider_id(item.get("ProviderIds") or {}, "Douban"):
        series_id = item.get("SeriesId") or item.get("ParentId")
        parent = _get_item_by_id(str(series_id)) if series_id else None
        if not parent:
            return {"status": "missing_ids"}
        subject = douban_source.resolve(DOUBAN_CACHE_DIR, parent)
        if subject.get("status") != "ok":
            return {"status": "unverified_season_parent", "parent_status": subject.get("status")}
        did = None
        if item.get("IndexNumber") == 1:
            did = subject.get("douban_id")
        elif subject.get("path"):
            season_map = _load_json_file(Path(subject["path"]).parent / "seasons.json", {})
            did = season_map.get(str(item.get("IndexNumber"))) if isinstance(season_map, dict) else None
        if not did:
            return {"status": "missing_season_mapping"}
        item = {**item, "ProviderIds": {"Douban": str(did)}}
    return douban_source.resolve(DOUBAN_CACHE_DIR, item)


def _douban_cache_file(item: dict[str, Any]) -> Path | None:
    result = _douban_cache_result(item)
    return Path(result["path"]) if result.get("status") == "ok" else None


def _douban_cache_rating(item: dict[str, Any]) -> str | None:
    return _douban_cache_result(item).get("value")


def _douban_rating(item: dict[str, Any]) -> tuple[str | None, str]:
    result = _douban_cache_result(item)
    if result.get("value"):
        return str(result["value"]), "douban.local_cache"
    if DOUBAN_REQUIRE_VERIFIED_CACHE:
        return None, "douban.verified_cache_required"
    if COMMUNITY_RATING_IS_DOUBAN and (not COMMUNITY_RATING_REQUIRES_DOUBAN_ID or provider_id(item.get("ProviderIds") or {}, "Douban")):
        community = _format_douban(item.get("CommunityRating"))
        if community:
            return community, "emby.community_rating"
        return None, "emby.community_rating"
    return None, "configuration"

def _format_douban(value: Any) -> str | None:
    score = rating_policy.decimal_score(value)
    return f"{score:.1f}" if score is not None else None


def community_rating_changed(item: dict[str, Any], cached_ratings: Any, cached_details: Any) -> bool:
    """Immediately recover a newly verified cache or a corrected verified score."""
    if not isinstance(cached_ratings, dict) or not isinstance(cached_details, dict):
        return False
    detail = cached_details.get("豆瓣")
    if not isinstance(detail, dict) or detail.get("source") == "rating_overrides":
        return False
    if detail.get("source") == "emby.community_rating" and not COMMUNITY_RATING_IS_DOUBAN:
        return False
    result = _douban_cache_result(item)
    current = result.get("value")
    cached = _format_douban(cached_ratings.get("豆瓣"))
    if current:
        return (current != cached or detail.get("source") != "douban.local_cache"
                or result.get("cache_updated_at") != detail.get("cache_updated_at"))
    if result.get("status") in {"identity_mismatch", "media_type_mismatch", "imdb_id_mismatch", "douban_id_mismatch", "cache_conflict"}:
        return bool(cached)
    if DOUBAN_REQUIRE_VERIFIED_CACHE:
        return bool(cached and detail.get("source") == "emby.community_rating")
    if detail.get("source") == "emby.community_rating" and COMMUNITY_RATING_IS_DOUBAN:
        emby_score = _format_douban(item.get("CommunityRating"))
        return emby_score is not None and emby_score != cached
    return False


def _format_imdb(value: Any) -> str | None:
    score = rating_policy.decimal_score(value)
    return f"{score:.1f}" if score is not None else None


def _format_rt(value: Any) -> str | None:
    return rating_policy.critic_percent(value, explicit_percent=True)



def _omdb_rating_values(imdb_id: str) -> tuple[dict[str, str], str]:
    payload = _omdb_cached_request(f"{imdb_id}", {"i": imdb_id})
    status = _omdb_status(payload)
    values: dict[str, str] = {}
    if status == "ok" and isinstance(payload, dict):
        returned_id = str(payload.get("imdbID") or "")
        if returned_id and returned_id.casefold() != imdb_id.casefold():
            return {}, "identity_mismatch"
        direct = _format_imdb(payload.get("imdbRating"))
        if direct:
            values["IMDb"] = direct
        for rating in payload.get("Ratings", []):
            if not isinstance(rating, dict):
                continue
            source = rating.get("Source")
            value = rating.get("Value")
            if source == "Internet Movie Database" and value:
                formatted = _format_imdb(value)
                if formatted:
                    values["IMDb"] = formatted
            elif source == "Rotten Tomatoes" and value:
                formatted = _format_rt(value)
                if formatted:
                    values["RT"] = formatted
    return values, status


def _external_rating_values(
    item: dict[str, Any],
    imdb_id: str,
    id_source: str,
    requested: set[str],
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Resolve missing IMDb/RT scores through MDBList first, then OMDb."""
    pending = set(requested).intersection({"IMDb", "RT"})
    values: dict[str, str] = {}
    details: dict[str, dict[str, str]] = {}
    if not pending:
        return values, details
    for provider in RATING_EXTERNAL_PROVIDER_ORDER:
        provider_values: dict[str, str] = {}
        provider_statuses: dict[str, str] = {}
        if provider == "mdblist":
            provider_values, raw_statuses = _mdblist_cached_values(item, imdb_id, pending)
            provider_statuses = {
                "IMDb": raw_statuses.get("imdb", "no_rating"),
                "RT": raw_statuses.get("tomatoes", "no_rating"),
            }
            provider_values = {key: value for key, value in provider_values.items()
                               if provider_statuses.get(key) == "ok"}
        elif provider == "omdb":
            provider_values, status = _omdb_rating_values(imdb_id)
            cached_omdb = _load_json_file(CACHE_DIR / "omdb" / f"{imdb_id}.json", {})
            response_type = str(cached_omdb.get("Type") or "").casefold()
            expected_type = "series" if item.get("Type") == "Series" else "movie"
            if response_type and response_type != expected_type:
                provider_values, status = {}, "identity_mismatch"
            provider_statuses = {source: ("no_rating" if status == "ok" and not provider_values.get(source) else status) for source in pending}
        for source in list(pending):
            value = provider_values.get(source)
            if value:
                values[source] = value
                source_suffix = id_source if provider == "omdb" else "provider_id"
                details[source] = {"status": "ok", "source": f"{provider}.{source_suffix}"}
                pending.remove(source)
            else:
                # Keep the last actionable provider status for diagnostics. A
                # later provider may still fill the value.
                source_suffix = id_source if provider == "omdb" else "provider_id"
                next_status = provider_statuses.get(source, "no_rating")
                previous_attempt = details.get(source) or {}
                if next_status in {"no_rating", "not_configured"} and previous_attempt.get("status") in rating_policy.TRANSIENT_STATUSES:
                    continue
                details[source] = {
                    "status": next_status,
                    "source": f"{provider}.{source_suffix}",
                }
        if not pending:
            break
    return values, details

def rating_details(
    item: dict[str, Any],
    refresh_sources: set[str] | None = None,
    cached_values: dict[str, str] | None = None,
    cached_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requested = set({"豆瓣", "IMDb", "RT"} if refresh_sources is None else refresh_sources)
    values: dict[str, str] = {str(key): str(value) for key, value in (cached_values or {}).items()}
    details: dict[str, dict[str, str]] = {
        str(key): value for key, value in (cached_details or {}).items() if isinstance(value, dict)
    }
    override = _rating_override(item)
    matched_ids_changed = bool(override.get("match") and media_match.effective_ids(item,override) != media_match.effective_ids(item,{}))
    identity_blocked = (_douban_cache_result(item).get("status") in {"identity_mismatch", "media_type_mismatch"}
                        and not _override_value(override, "imdb_id", "imdbid"))

    if "豆瓣" in requested:
        douban_override = _format_douban(_override_value(override, "douban", "豆瓣"))
        if douban_override:
            values["豆瓣"] = douban_override
            details["豆瓣"] = {"status": "override", "source": "rating_overrides", "verified_at": override.get("verified_at"), "reason": override.get("reason")}
        else:
            result = _douban_cache_result(item)
            ids = item.get("ProviderIds") or {}
            effective = media_match.effective_ids(item,override)
            current_douban = effective["douban"]
            current_imdb = effective["imdb"]
            if result.get("value"):
                values["豆瓣"] = str(result["value"])
                details["豆瓣"] = {
                    "status": "ok", "source": "douban.local_cache",
                    "douban_id": result.get("douban_id"), "emby_douban_id": current_douban,
                    "imdb_id": current_imdb, "match": result.get("match"),
                    "cache_age_days": result.get("cache_age_days"), "missing_attempts": 0,
                    "cache_updated_at": result.get("cache_updated_at"),
                    "cache_title": result.get("cache_title"), "cache_year": result.get("cache_year"),
                    "identity_warnings": result.get("identity_warnings", []),
                    "score_scope": "season" if item.get("Type") == "Season" else ("subject/first_season" if item.get("Type") == "Series" else "movie"),
                    "last_check_at": time.time(),
                }
            else:
                previous = details.get("豆瓣") if isinstance(details.get("豆瓣"), dict) else {}
                previous_value = _format_douban(values.get("豆瓣"))
                reason = str(result.get("status") or "unknown")
                same_identity = (
                    previous.get("source") in {"douban.local_cache", "douban.cached_previous"}
                    and previous.get("douban_id")
                    and previous.get("emby_douban_id") == current_douban
                    and previous.get("imdb_id") == current_imdb
                )
                transient = reason in {"cache_unavailable", "missing_cache_file", "invalid_json"}
                attempts = int(previous.get("missing_attempts") or 0) + 1
                if previous_value and same_identity and transient and attempts < DOUBAN_MISSING_CONFIRMATIONS:
                    values["豆瓣"] = previous_value
                    details["豆瓣"] = {
                        "status": "temporarily_missing", "source": "douban.cached_previous",
                        "reason": reason, "douban_id": previous["douban_id"],
                        "emby_douban_id": current_douban, "imdb_id": current_imdb,
                        "missing_attempts": attempts,
                    }
                elif (not DOUBAN_REQUIRE_VERIFIED_CACHE and COMMUNITY_RATING_IS_DOUBAN
                      and (not COMMUNITY_RATING_REQUIRES_DOUBAN_ID or current_douban)): 
                    # Explicit opt-in only: CommunityRating has no intrinsic
                    # Douban provenance and is never considered verified.
                    community = _format_douban(item.get("CommunityRating"))
                    if community:
                        values["豆瓣"] = community
                        details["豆瓣"] = {"status": "unverified", "source": "emby.community_rating", "reason": reason}
                    else:
                        values.pop("豆瓣", None)
                        details["豆瓣"] = {"status": "no_rating", "source": "douban.cache", "reason": reason}
                else:
                    values.pop("豆瓣", None)
                    status = "identity_mismatch" if reason in {"identity_mismatch", "media_type_mismatch", "imdb_id_mismatch", "douban_id_mismatch", "cache_conflict"} else "no_rating"
                    details["豆瓣"] = {"status": status, "source": "douban.cache", "reason": reason}

    # Douban values come from a local file lookup, so re-validate them on
    # every render instead of trusting a weeks-old cached number. This keeps
    # the poster identical to the current Shenyi cache and clears values whose
    # identity disappeared upstream.
    if "豆瓣" in values and "豆瓣" not in requested:
        result = _douban_cache_result(item)
        reason = str(result.get("status") or "unknown")
        if result.get("value"):
            values["豆瓣"] = str(result["value"])
            details["豆瓣"] = {
                "status": "ok", "source": "douban.local_cache",
                "douban_id": result.get("douban_id"),
                "cache_age_days": result.get("cache_age_days"),
                "cache_updated_at": result.get("cache_updated_at"),
                "cache_title": result.get("cache_title"), "cache_year": result.get("cache_year"),
                "identity_warnings": result.get("identity_warnings", []),
            }
        elif reason in DOUBAN_HARD_MISSES:
            values.pop("豆瓣", None)
            details["豆瓣"] = {"status": "no_rating", "source": "douban.cache", "reason": reason}

    if "RT" in requested:
        values.pop("RT", None)
        rt_override = _format_rt(_override_value(override, "rt", "rottentomatoes", "烂番茄"))
        if rt_override:
            values["RT"] = rt_override
            details["RT"] = {"status": "override", "source": "rating_overrides"}
        else:
            critic = number(item.get("CriticRating"))
            if not identity_blocked and not matched_ids_changed and item.get("Type") != "Season" and CRITIC_RATING_IS_RT and critic is not None and 0 <= critic <= 100:
                values["RT"] = f"{round(critic):d}%"
                details["RT"] = {"status": "ok", "source": "emby.critic_rating"}
            else:
                values.pop("RT", None)
                details.pop("RT", None)

    imdb_override = _format_imdb(_override_value(override, "imdb", "IMDb")) if "IMDb" in requested else None
    if imdb_override:
        values["IMDb"] = imdb_override
        details["IMDb"] = {"status": "override", "source": "rating_overrides"}
    elif "IMDb" in requested:
        values.pop("IMDb", None)

    if requested.intersection({"IMDb", "RT"}):
        override_imdb_id = _override_value(override, "imdb_id", "imdbid")
        imdb_id, id_source = ((None, "identity_mismatch") if identity_blocked else
                              _find_omdb_id_info(item, str(override_imdb_id) if override_imdb_id else None))
        externally_requested = {
            source for source in requested.intersection({"IMDb", "RT"})
            if not values.get(source)
        }
        if imdb_id and externally_requested:
            external_values, external_details = _external_rating_values(item, str(imdb_id), id_source, externally_requested)
            for source in externally_requested:
                if source in external_values:
                    values[source] = external_values[source]
                else:
                    values.pop(source, None)
                details[source] = external_details.get(
                    source,
                    {"status": "no_rating", "source": "rating_lookup"},
                )
        elif externally_requested:
            for source in externally_requested:
                values.pop(source, None)
                details[source] = {"status": id_source, "source": "rating_lookup"}

    # Same guarantee for IMDb, but only when the cached detail actually recorded
    # an IMDb id that has since disappeared (legacy states without a recorded id
    # keep their value so manual imports are not wiped).
    if "IMDb" in values and "IMDb" not in requested:
        has_imdb_id = bool(media_match.effective_ids(item, override).get("imdb"))
        recorded_id = str((details.get("IMDb") or {}).get("imdb_id") or "")
        if not has_imdb_id and recorded_id and not _format_imdb(_override_value(override, "imdb", "IMDb")):
            values.pop("IMDb", None)
            details["IMDb"] = {"status": "missing_ids", "source": "rating_lookup"}

    # Retain only identity-bound last successful scores on transient provider failures.
    # Genuine no_rating responses clear scores; an HTTP success without a score is not ok.
    now = time.time()
    for source in ("IMDb", "RT"):
        if source not in requested:
            continue
        info = details.get(source, {})
        previous = (cached_details or {}).get(source, {})
        old = (cached_values or {}).get(source)
        if not values.get(source) and info.get("status") in rating_policy.TRANSIENT_STATUSES and old and previous.get("status") in {"ok", "temporarily_unavailable"}:
            values[source] = old
            details[source] = {**previous, "status": "temporarily_unavailable",
                               "reason": info.get("status"), "last_check_at": now}
        elif values.get(source):
            info = details[source]
            provider_time = None
            iid = media_match.effective_ids(item,override).get("imdb")
            if iid and str(info.get("source") or "").startswith("mdblist"):
                data = _load_json_file(_mdblist_cache_file("show" if item.get("Type") == "Series" else "movie", str(iid)), {})
                times = data.get("checked_at") or {}
                provider_time = times.get("imdb" if source == "IMDb" else "tomatoes") if isinstance(times, dict) else times
            elif iid and str(info.get("source") or "").startswith("omdb"):
                path = CACHE_DIR / "omdb" / f"{iid}.json"
                provider_time = path.stat().st_mtime if path.is_file() else None
            info["last_success_at"] = provider_time
            info["last_check_at"] = now
    for source in ("豆瓣", "IMDb", "RT"):
        details.setdefault(source, {"status": "no_rating", "source": "unknown"})
    return {"values": values, "details": details}

def ratings(item: dict[str, Any]) -> dict[str, str]:
    return {str(key): str(value) for key, value in rating_details(item)["values"].items()}

ASSETS_DIR = Path(__file__).parent / "assets"

normalize_poster_canvas = rendering.normalize_poster_canvas
load_font = rendering.load_font


def _rating_sources(scores: dict[str, str], strategy: str | None = None) -> list[tuple[str, str, str, tuple[int, int, int]]]:
    """Rating groups for one item using the live display strategy."""
    return rendering.rating_sources(scores, RATING_DISPLAY_STRATEGY if strategy is None else strategy)


def _render_context() -> rendering.RenderContext:
    return rendering.RenderContext(
        settings=RENDER_SETTINGS,
        strategy=RATING_DISPLAY_STRATEGY,
        assets_dir=ASSETS_DIR,
        render_version=RENDER_VERSION,
        image_marker=WORKER_IMAGE_MARKER,
        ratings=ratings,
    )


def render(item: dict[str, Any], source: Path, output: Path, scores: dict[str, str] | None = None) -> None:
    """Composite one poster using the current admin settings."""
    rendering.render(item, source, output, scores, _render_context())

def _style_preview_item() -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Choose a rendered sample that exposes the full overlay surface.

    The style editor must show all four quality badges and all three rating
    slots whenever the cache contains such an item; otherwise adjustments to
    the corresponding layouts cannot be verified visually.
    """
    global STYLE_PREVIEW_ITEM_ID
    if STYLE_PREVIEW_ITEM_ID:
        item = _get_item_by_id(STYLE_PREVIEW_ITEM_ID)
        if item:
            item_state = _safe_item_state(STYLE_PREVIEW_ITEM_ID)
            item_ratings = item_state.get("ratings") if isinstance(item_state.get("ratings"), dict) else {}
            if len(quality_labels(item)) >= 4 and len(item_ratings) >= 3:
                return item, item_state
        STYLE_PREVIEW_ITEM_ID = None

    rendered_dir = CACHE_DIR / "rendered"
    best: tuple[tuple[int, int, int], dict[str, Any], dict[str, Any]] | None = None
    for rendered in sorted(rendered_dir.glob("*.jpg")) if rendered_dir.exists() else []:
        item_id = rendered.stem
        item = _get_item_by_id(item_id)
        if not item:
            continue
        item_state = _safe_item_state(item_id)
        # Do not trigger OMDb requests while scanning candidate samples.
        score_values = item_state.get("ratings") if isinstance(item_state.get("ratings"), dict) else {}
        quality_count = len(quality_labels(item))
        rating_count = len(score_values or {})
        rank = (int(quality_count >= 4 and rating_count >= 3), quality_count, rating_count)
        if best is None or rank > best[0]:
            best = (rank, item, item_state)
        if rank[0] == 1:
            break
    if best:
        _, item, item_state = best
        STYLE_PREVIEW_ITEM_ID = str(item.get("Id"))
        return item, item_state
    return None

def render_style_preview(settings: dict[str, Any], strategy: str | None = None) -> dict[str, Any]:
    sample = _style_preview_item()
    if not sample:
        raise FileNotFoundError("当前没有可用于预览的已生成海报")
    item, state = sample
    item_id = str(item.get("Id"))
    original = CACHE_DIR / "original" / f"{item_id}.jpg"
    if not original.exists():
        original = get_original(item_id)
    scores = state.get("ratings") if isinstance(state.get("ratings"), dict) else ratings(item)
    output = CACHE_DIR / "previews" / "style-preview.jpg"
    global RENDER_SETTINGS, RATING_DISPLAY_STRATEGY
    with STYLE_PREVIEW_LOCK:
        old_settings = RENDER_SETTINGS
        old_strategy = RATING_DISPLAY_STRATEGY
        try:
            RENDER_SETTINGS = _merge_dict(DEFAULT_RENDER_SETTINGS, settings)
            RATING_DISPLAY_STRATEGY = str(strategy or old_strategy).lower()
            with RENDER_LOCK:
                render(item, original, output, scores)
        finally:
            RENDER_SETTINGS = old_settings
            RATING_DISPLAY_STRATEGY = old_strategy
    return {"image_url": f"/api/style-preview/image?v={int(time.time() * 1000)}", "item_id": item_id, "name": item.get("Name"), "year": item.get("ProductionYear"), "quality_labels": quality_labels(item), "quality_count": len(quality_labels(item)), "rating_count": len(scores or {})}


def _signature(item: dict[str, Any], rating_data: dict[str, str] | None, quality: list[str]) -> str:
    payload = {
        # Keep the legacy field stable for unchanged artwork. Code-only releases
        # must not redraw every movie; render_version remains the artwork key.
        "app_version": "2.20",
        "render_version": RENDER_VERSION,
        "rating_display_strategy": RATING_DISPLAY_STRATEGY,
        "render_settings": RENDER_SETTINGS,
        "image_delivery_mode": IMAGE_DELIVERY_MODE,
        "name": item.get("Name"),
        "provider_ids": item.get("ProviderIds"),
        "date_last_media_added": item.get("DateLastMediaAdded"),
        "date_modified": item.get("DateModified"),
        "media_sources": item.get("MediaSources"),
        "ratings": rating_data if rating_data is not None else ratings(item),
        "rating_override": _rating_override(item),
        "quality": quality,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def signature(item: dict[str, Any], rating_data: dict[str, str] | None = None) -> str:
    return _signature(item, rating_data, quality_labels(item))


def media_signature(item: dict[str, Any]) -> str:
    """Fingerprint media identity so a replaced file refreshes the clean original."""
    payload = {
        "id": item.get("Id"),
        "type": item.get("Type"),
        "path": item.get("Path"),
        "date_modified": item.get("DateModified"),
        "date_last_media_added": item.get("DateLastMediaAdded"),
        "media_sources": [
            {
                "id": source.get("Id"),
                "path": source.get("Path"),
                "container": source.get("Container"),
                "size": source.get("Size"),
                "date_modified": source.get("DateModified"),
            }
            for source in item.get("MediaSources") or []
            if isinstance(source, dict)
        ],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def rating_input_summary(items: list[dict[str, Any]]) -> dict[str, int]:
    """Summarize raw Emby rating fields without making external lookups."""
    return {
        "items": len(items),
        "community_rating": sum(number(item.get("CommunityRating")) is not None for item in items),
        "douban_provider_id": sum(provider_id(item.get("ProviderIds"), "Douban") is not None for item in items),
        "critic_rating": sum(number(item.get("CriticRating")) is not None for item in items),
        "imdb_provider_id": sum(provider_id(item.get("ProviderIds"), "Imdb") is not None for item in items),
    }


def update_rating_stats(stats: Counter[str] | None, details: dict[str, Any], values: dict[str, str] | None = None) -> None:
    if stats is None:
        return
    with RATING_STATS_LOCK:
        for source, info in (details or {}).items():
            status = str((info or {}).get("status") or "unknown")
            if status in {"ok", "override"} and values is not None and not values.get(source):
                status = "no_rating"
            stats[f"{source}:{status}"] += 1


def current_image_tag(item_id: str) -> str:
    try:
        payload = emby("GET", "/Items", params={"Ids": item_id, "Fields": "ImageTags"}).json()
        return str((payload.get("Items") or [{}])[0].get("ImageTags", {}).get("Primary", ""))
    except requests.RequestException:
        return ""


def upload(item_id: str, image_path: Path) -> None:
    # Emby 4.10's image endpoint expects the request body to be base64 text,
    # despite the OpenAPI description calling it an octet stream.
    encoded = base64.b64encode(image_path.read_bytes())
    response = SESSION.post(
        f"{EMBY_URL}/Items/{item_id}/Images/Primary?index=0",
        data=encoded,
        headers={"Content-Type": "image/jpeg"},
        timeout=EMBY_REQUEST_TIMEOUT,
    )
    response.raise_for_status()


def deliver_image(item_id: str, image_path: Path) -> str:
    """Publish a rendered image using the selected resilient delivery mode."""
    mode = IMAGE_DELIVERY_MODE if IMAGE_DELIVERY_MODE in {"url", "upload", "dual"} else "url"
    if mode == "upload":
        upload(item_id, image_path)
        return "upload"
    if mode == "dual":
        upload(item_id, image_path)
        if set_image_url(item_id, image_path):
            return "dual"
        return "upload"
    if set_image_url(item_id, image_path):
        return "url"
    upload(item_id, image_path)
    return "upload-fallback"


def set_image_url(item_id: str, image_path: Path, public_name: str | None = None) -> bool:
    """Point Emby's Primary image at the worker's LAN URL.

    This avoids writing poster.jpg into the 115 mount, which is commonly
    read-only. Emby then proxies the worker URL to all clients.
    """
    public_name = public_name or image_path.name
    try:
        content_version = f"{RENDER_VERSION}-{image_path.stat().st_mtime_ns}"
    except OSError:
        content_version = RENDER_VERSION
    public_url = f"{PUBLIC_BASE_URL}/{public_name}?v={quote(content_version)}"
    try:
        response = SESSION.post(
            f"{EMBY_URL}/Items/{item_id}/Images/Primary/0/Url",
            params={"Url": public_url},
            timeout=EMBY_REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        LOG.warning("setting image URL failed for %s: %s", item_id, exc)
        return False
    if response.status_code in {200, 204}:
        return True
    LOG.warning("setting image URL failed for %s: HTTP %s", item_id, response.status_code)
    return False


def current_time_iso() -> str:
    try:
        zone = ZoneInfo(LOG_TIMEZONE)
    except ZoneInfoNotFoundError:
        zone = timezone(timedelta(hours=8), name="UTC+08:00") if LOG_TIMEZONE in {"Asia/Shanghai", "Asia/Singapore", "Etc/GMT-8"} else timezone.utc
    return datetime.now(zone).isoformat(timespec="seconds")


def cache_gc(item_ids: set[str], expected_total: int = 0) -> dict[str, int]:
    """Report or remove orphaned item caches without touching OMDb cache entries.

    Deletion is skipped when the scan returned far fewer items than expected:
    a partial Emby response must never be read as "these films were deleted",
    because the clean originals cannot be regenerated.
    """
    roots = {
        "original": CACHE_DIR / "original",
        "rendered": CACHE_DIR / "rendered",
        "state": CACHE_DIR / "state",
        "series_media": CACHE_DIR / "series-media",
    }
    cutoff = time.time() - max(1, int(_maint("cache_gc_days", CACHE_GC_DAYS))) * 86400
    complete = not expected_total or len(item_ids) >= expected_total * 0.9
    result = {"orphaned": 0, "removed": 0, "skipped_incomplete": 0}
    for root in roots.values():
        if not root.exists():
            continue
        for path in root.iterdir():
            if not path.is_file() or path.stem in item_ids:
                continue
            result["orphaned"] += 1
            if not complete:
                result["skipped_incomplete"] += 1
                continue
            if _maint("cache_gc_enabled", CACHE_GC_ENABLED) and path.stat().st_mtime < cutoff:
                try:
                    path.unlink()
                    result["removed"] += 1
                except OSError as exc:
                    LOG.warning("cache cleanup failed for %s: %s", path, exc)
    if result["orphaned"]:
        LOG.info("cache hygiene: orphaned=%d removed=%d enabled=%s skipped_incomplete=%d",
                 result["orphaned"], result["removed"], CACHE_GC_ENABLED, result["skipped_incomplete"])
    return result


def write_media_health_report(items: list[dict[str, Any]], generated_at: str) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    severities: Counter[str] = Counter()
    for item in items:
        for issue in item.get("issues") or []:
            counts[str(issue.get("code") or "unknown")] += 1
            severities[str(issue.get("severity") or "info")] += 1
    report = {"generated_at": generated_at, "summary": {"items_with_issues": len(items), "issue_counts": dict(sorted(counts.items())), "severity_counts": dict(sorted(severities.items()))}, "items": items}
    try:
        atomic_write_text(MEDIA_HEALTH_REPORT_FILE, json.dumps(report, ensure_ascii=False, indent=2))
    except OSError as exc:
        LOG.warning("media health report write failed for %s: %s", MEDIA_HEALTH_REPORT_FILE, exc)
    return report["summary"]


def write_scan_report(report: dict[str, Any]) -> None:
    try:
        atomic_write_text(SCAN_REPORT_FILE, json.dumps(report, ensure_ascii=False, indent=2))
    except OSError as exc:
        LOG.warning("scan report write failed for %s: %s", SCAN_REPORT_FILE, exc)


"""Search, preview and confirm API helpers for the single-item matching workflow."""
MATCH_CANDIDATES: dict[str, dict[str, Any]] = {}
MATCH_CANDIDATES_LOCK = threading.Lock()
MATCH_EDIT_LOCK = threading.Lock()
MATCH_CANDIDATE_TTL = 1800


def _match_fingerprint(item):
    payload = {'id':str(item.get('Id')),'name':item.get('Name'),'year':item.get('ProductionYear'),
               'type':item.get('Type'),'ids':item.get('ProviderIds') or {},'override':_rating_override(item)}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def _match_search(item, body):
    query = str(body.get('query') or item.get('Name') or '').strip()
    kind = str(body.get('kind') or item.get('Type'))
    year = str(body.get('year') or '')
    if kind not in {'Movie', 'Series'} or item.get('Type') == 'Season':
        raise ValueError('请选择电影或剧集类型；季海报请使用已确认的独立季 ID')
    if not 2 <= len(query) <= 120:
        raise ValueError('请输入 2–120 个字符的片名')
    if year and not re.fullmatch(r'(?:19|20)[0-9]{2}', year):
        raise ValueError('年份应为四位数字，也可留空扩大搜索范围')
    subject_input = str(body.get('douban_id') or '').strip()
    direct = bool(subject_input)
    local = media_match.search_local(DOUBAN_CACHE_DIR, query, kind, year)
    if direct:
        subject_id = search_sources.parse_subject_id(subject_input)
        exact = search_sources.local_subject(DOUBAN_CACHE_DIR, subject_id)
        douban = {'status': 'ok', 'items': [exact]} if exact else search_sources.douban_subject(SESSION, subject_id)
        if not douban['items']:
            # Public detail pages may require interactive verification. Never
            # claim we read a title, rating or cross-provider link in this case.
            douban['items'] = [{'title': query, 'year': year, 'kind': kind, 'douban_id': subject_id,
                                'imdb_id': None, 'douban_score': None, 'source': 'manual_douban_id',
                                'source_label': '手动豆瓣 ID · 待人工核对', 'poster': None,
                                'overview': '', 'cache_updated_at': None, 'requires_manual_verification': True}]
        tmdb = {'status': 'not_requested', 'items': []}
        rows = douban['items']
    else:
        douban, tmdb = search_sources.gather(SESSION, emby, query, kind)
        rows = search_sources.combine(local, douban['items'], tmdb['items'], item)
    key = 'match-v223-'+hashlib.sha256(f'{query}|{kind}|{year}'.encode()).hexdigest()
    payload = _omdb_cached_request(key,{'s':query,'type':'series' if kind == 'Series' else 'movie',**({'y':year} if year else {})}, interactive=True)
    if not direct:
        rows=media_match.merge_omdb(rows,payload,kind,query,year)
    omdb_display_status=_omdb_status(payload)
    current=media_match.effective_ids(item,_rating_override(item))
    # A title lookup is optional when the resource already has a real IMDb ID.
    # Read that ID directly, preserving ID/type checks without guessing translation.
    if current['imdb'] and query in {item.get('Name'),item.get('OriginalTitle')}:
        imdb_detail=_omdb_cached_request(current['imdb'],{'i':current['imdb']},interactive=True)
        expected='series' if kind=='Series' else 'movie'
        if _omdb_status(imdb_detail)=='ok' and imdb_detail.get('Type')==expected and str(imdb_detail.get('imdbID','')).casefold()==current['imdb'].casefold():
            omdb_display_status='id_verified'
            if not subject_input:
                rows=media_match.merge_omdb(rows,{'Search':[{'Title':imdb_detail.get('Title'),'Year':imdb_detail.get('Year'),'Type':expected,'imdbID':imdb_detail.get('imdbID'),'Poster':imdb_detail.get('Poster')}]},kind,query,year)
            for row in rows:
                if row.get('imdb_id')==current['imdb']:row['imdb_title']=imdb_detail.get('Title')
            # Suggest retaining this known ID alongside a selected same-title/year
            # Douban subject, but require a separate explicit association checkbox.
            for row in rows:
                if row.get('douban_id') and not row.get('imdb_id') and (row.get('requires_manual_verification') or rating_policy.normalized_title(row['title'])==rating_policy.normalized_title(item.get('Name')) and row['year']==str(item.get('ProductionYear'))):
                    row['existing_imdb_id']=current['imdb'];row['existing_imdb_title']=imdb_detail.get('Title')
    rows=media_match.sort_candidates(rows,query,year)
    result=[]
    now=time.time()
    with MATCH_CANDIDATES_LOCK:
        for token in list(MATCH_CANDIDATES):
            if MATCH_CANDIDATES[token]['expires_at'] < now:
                MATCH_CANDIDATES.pop(token,None)
        for row in rows:
            token=secrets.token_urlsafe(24)
            review=rating_policy.cache_identity_review({**item, 'Type':row['kind']}, {'title':row['title'],'year':row['year'],'is_tv':row['kind']=='Series'})
            if row['kind'] != item.get('Type'):review['warnings'].append('type_correction')
            if row.get('type_unverified'):review['warnings'].append('type_unverified')
            if current['imdb'] and row.get('imdb_id') and current['imdb'] != row['imdb_id']:review['warnings'].append('imdb_version_difference')
            row={**row,'warnings':review['warnings'],'blocked':review['blocked'],'bindable':bool(row.get('douban_id') or row.get('imdb_id'))}
            MATCH_CANDIDATES[token]={'item_id':str(item['Id']),'fingerprint':_match_fingerprint(item),'candidate':row,'expires_at':now+MATCH_CANDIDATE_TTL}
            result.append({**row,'token':token})
        # Bound process memory even when clients repeatedly search.
        while len(MATCH_CANDIDATES)>600:
            MATCH_CANDIDATES.pop(next(iter(MATCH_CANDIDATES)))
    return {'items':result,'providers':{'douban_cache':'ok','douban_online':douban['status'],'douban_search':douban.get('search_status', 'not_requested'),'tmdb':tmdb['status'],'omdb':omdb_display_status},
            'incomplete':True,
            'note':'来源可能遗漏作品；可以直接输入豆瓣 ID 或条目链接。',
            'query':query,'kind':kind,'year':year}


def _selected_candidate(item, token):
    with MATCH_CANDIDATES_LOCK:
        entry=MATCH_CANDIDATES.get(str(token))
    if not entry or entry['expires_at']<time.time() or entry['item_id']!=str(item['Id']):
        raise ValueError('候选已过期或不属于当前资源，请重新搜索')
    if entry['fingerprint']!=_match_fingerprint(item):
        raise ValueError('资源 ID 或人工设置已经变化，请重新搜索后确认')
    if entry['candidate'].get('blocked'):
        raise ValueError('候选与资源年份及作品身份明显冲突，请先核对 Emby 片名、年份或类型')
    if not entry['candidate'].get('douban_id') and not entry['candidate'].get('imdb_id'):
        raise ValueError('候选没有可以确认的外部 ID')
    return entry['candidate']


def _match_preview(item, body):
    candidate=dict(_selected_candidate(item,body.get('token')))
    if body.get('keep_existing_imdb') is True and candidate.get('existing_imdb_id'):
        candidate['imdb_id']=candidate['existing_imdb_id']
    override=_rating_override(item)
    ids=media_match.planned_ids(item,override,candidate)
    trial={**item,'Type':candidate['kind'],'ProviderIds':{'Douban':ids['douban'],'Imdb':ids['imdb']}}
    result=douban_source.resolve(DOUBAN_CACHE_DIR,trial)
    values={}
    if result.get('value'):
        values['豆瓣']=result['value']
    omdb_status='not_requested'
    if ids['imdb'] and OMDB_API_KEY:
        payload=_omdb_cached_request(ids['imdb'],{'i':ids['imdb']},interactive=True)
        omdb_status=_omdb_status(payload)
        expected='series' if candidate['kind']=='Series' else 'movie'
        if omdb_status=='ok' and (str(payload.get('imdbID') or '').casefold()!=ids['imdb'].casefold() or payload.get('Type')!=expected):
            raise ValueError('IMDb 详情与所选 ID 或电影/剧集类型不符，无法确认绑定')
        if omdb_status=='ok':
            imdb=media_match.score(payload.get('imdbRating'))
            if imdb:values['IMDb']=imdb
            for rating in payload.get('Ratings') or []:
                if isinstance(rating,dict) and rating.get('Source')=='Rotten Tomatoes':
                    rt=_format_rt(rating.get('Value'))
                    if rt:values['RT']=rt
        with MATCH_CANDIDATES_LOCK:
            MATCH_CANDIDATES[str(body['token'])].update({'previewed_at':time.time(),'preview_ids':ids})
    else:
        with MATCH_CANDIDATES_LOCK:
            MATCH_CANDIDATES[str(body['token'])].update({'previewed_at':time.time(),'preview_ids':ids})
    old=media_match.effective_ids(item,override)
    changes=[{'source':source,'before':old[source],'after':ids[source]} for source in ids if old[source]!=ids[source]]
    return {'candidate':candidate,'effective_ids':ids,'changes':changes,'automatic_ratings':values,
            'douban_status':result.get('status'),'cache_updated_at':result.get('cache_updated_at'),
            'cache_age_days':result.get('cache_age_days'),'omdb_status':omdb_status,
            'manual_scores':{k:override[k] for k in ('douban','imdb','rt') if override.get(k)},
            'waiting_for_cache':bool(ids['douban'] and not result.get('value')),
            'type_changed':candidate['kind'] != item.get('Type'),
            'requires_manual_verification':bool(candidate.get('requires_manual_verification')),
            'type_unverified':bool(candidate.get('type_unverified')),
            'note':'仅预览，不修改 ID 或评分；绑定 ID 不等于立即取得评分。'}


def _sync_match_to_emby(item, ids):
    if DRY_RUN:
        raise ValueError('DRY_RUN 已开启：试运行不会写回 Emby 的 ProviderIds，请关闭后重试')
    users=emby('GET','/Users',timeout=ADMIN_API_TIMEOUT).json()
    admin=next((u for u in users if (u.get('Policy') or {}).get('IsAdministrator')),None)
    if not admin:
        raise ValueError('无法读取 Emby 管理员完整元数据，未写回任何 ID')
    # Retrieve a complete DTO to preserve titles, ratings, people and locks.
    full=emby('GET',f"/Users/{quote(str(admin['Id']))}/Items/{quote(str(item['Id']))}",params={'Fields':'ProviderIds,Overview,People,Studios,Genres,Tags,Taglines,LockedFields,OriginalTitle,PremiereDate,EndDate,CommunityRating,CriticRating,OfficialRating,CustomRating,ProductionYear,SortName'},timeout=ADMIN_API_TIMEOUT).json()
    if str(full.get('Id'))!=str(item['Id']) or full.get('ProviderIds') != item.get('ProviderIds'):
        raise ValueError('Emby 元数据已经变化，请重新搜索后确认')
    original={k:v for k,v in (full.get('ProviderIds') or {}).items() if str(k).casefold() not in {'douban','imdb'}}
    updated={**original,**({'Douban':ids['douban']} if ids['douban'] else {}),**({'Imdb':ids['imdb']} if ids['imdb'] else {})}
    emby('POST',f"/Items/{quote(str(item['Id']))}",json={**full,'ProviderIds':updated})
    fresh=_get_item_by_id(str(item['Id']))
    if not fresh or media_match.effective_ids(fresh,{}) != ids:
        raise RuntimeError('Emby 写入后的 ID 未通过回读校验；请刷新详情检查，Worker 尚未保存新绑定')
    return fresh


def _match_confirm(item,body):
    if body.get('confirmed') is not True:
        raise ValueError('请先核对候选及 ID 变更，再确认绑定')
    with MATCH_EDIT_LOCK:
        candidate=_selected_candidate(item,body.get('token'))
        if candidate.get('requires_manual_verification') and body.get('manual_verified') is not True:
            raise ValueError('条目未能自动读取，请先人工核对豆瓣 ID、作品版本和年份并勾选确认')
        if (candidate['kind'] != item.get('Type') or candidate.get('type_unverified')) and body.get('type_confirmed') is not True:
            raise ValueError('请明确确认这部资源的电影/剧集类型')
        with MATCH_CANDIDATES_LOCK:
            entry=MATCH_CANDIDATES[str(body['token'])]
        if not entry.get('previewed_at'):
            raise ValueError('请先预览 ID 和评分变更')
        ids=entry['preview_ids']
        sync=body.get('sync_emby',True)
        if not isinstance(sync,bool):raise ValueError('写回 Emby 选项无效')
        before=_rating_override(item)
        updated=dict(before)
        modes=body.get('score_modes') or {}
        if not isinstance(modes,dict) or any(k not in {'douban','imdb','rt'} or v not in {'keep','automatic'} for k,v in modes.items()):
            raise ValueError('评分模式无效')
        for key,mode in modes.items():
            if mode=='automatic':updated.pop(key,None)
        updated.update({'douban_id':ids['douban'],'imdb_id':ids['imdb'],
                        'match':{'title':candidate['title'],'year':candidate['year'],'type':candidate['kind'],
                                 'source':candidate['source'],'confirmed_at':current_time_iso(),
                                 'sync_emby':sync},'binding_verified_at':current_time_iso()})
        if sync:item=_sync_match_to_emby(item,ids)
        # Empty IDs are explicit tombstones: do not revive an old mismatched ID.
        overrides=dict(_load_rating_overrides());overrides[str(item['Id'])]=updated
        atomic_write_text(RATING_OVERRIDES_FILE,json.dumps({'items':overrides},ensure_ascii=False,indent=2))
        global _RATING_OVERRIDES_MTIME
        _RATING_OVERRIDES_MTIME=-1
        with MATCH_CANDIDATES_LOCK:MATCH_CANDIDATES.pop(str(body['token']),None)
        global ITEM_CACHE_AT
        ITEM_CACHE_AT=0
        _invalidate_matching_queue()
        queued=_start_item_render(item)
        return {'ok':True,'queued':queued,'task':_task_state(item['Id']),'sync_emby':sync,'override':updated,
                'effective_ids':ids,'message':'匹配已保存，封面已加入后台队列；无需重复保存评分设置' if queued else '匹配已保存'}


def _match_queue_flags(item):
    override=_rating_override(item)
    flags=media_match.id_gaps(item,override)
    result=_douban_cache_result(item)
    if result.get('status') in {'identity_mismatch','media_type_mismatch','imdb_id_mismatch','douban_id_mismatch','cache_conflict'}:
        flags.append('identity_conflict')
    state=_safe_item_state(str(item.get('Id')))
    if not flags and not (state.get('ratings') or {}).get('豆瓣'):
        flags.append('ids_without_score')
    return flags


ISSUE_DECISIONS_LOCK=threading.Lock()

def _item_triage(item,state=None,decisions=None):
    item = _effective_item_type(item)
    ids = media_match.effective_ids(item, _rating_override(item))
    provider_ids = {k:v for k,v in (item.get('ProviderIds') or {}).items() if str(k).casefold() not in {'douban', 'imdb'}}
    provider_ids.update({k:v for k,v in [('Douban', ids['douban']), ('Imdb', ids['imdb'])] if v})
    item = {**item, 'ProviderIds': provider_ids}
    state=state if state is not None else _safe_item_state(str(item.get('Id')))
    values=state.get('ratings') or {};details=state.get('rating_details') or {}
    media=media_health(item).get('issues') or []
    codes=_rating_diagnostic_codes(item,values,details,(CACHE_DIR/'rendered'/f"{item.get('Id')}.jpg").is_file(),media)
    if decisions is None:
        decisions=_load_json_file(CACHE_DIR/'issue-decisions.json',{}).get(str(item.get('Id')),{})
    item = {**item, '_binding_at': (_rating_override(item).get('match') or {}).get('confirmed_at'), '_has_original': (CACHE_DIR/'original'/f"{item['Id']}.jpg").is_file()}
    work = _task_state(item['Id'])
    if work and (work.get('context') or {}).get('identity') not in {None, _work_identity(item)}:
        work = None
    handled = bool((_rating_override(item).get('match') or {}).get('confirmed_at'))
    return issue_policy.classify(item,state,_douban_cache_result(item),media_match.id_gaps(item,_rating_override(item)),codes,time.time(),max(1,min(168,int(ADMIN_SETTINGS.get('new_item_grace_hours') or 48))),decisions,work=work,handled=handled)

def _invalidate_matching_queue():
    global MATCHING_QUEUE_CACHE, MATCHING_QUEUE_CACHE_AT
    if ADMIN_VIEW is not None:
        ADMIN_VIEW.invalidate()
    with MATCHING_QUEUE_LOCK:
        MATCHING_QUEUE_CACHE = None
        MATCHING_QUEUE_CACHE_AT = 0


def _matching_queue_payload(force=False):
    global MATCHING_QUEUE_CACHE, MATCHING_QUEUE_CACHE_AT
    # A snapshot is shared by concurrent admin requests and expires promptly.
    with MATCHING_QUEUE_LOCK:
        if not force and MATCHING_QUEUE_CACHE is not None and time.monotonic() - MATCHING_QUEUE_CACHE_AT < MATCHING_QUEUE_TTL:
            return MATCHING_QUEUE_CACHE
        DOUBAN_RESOLVE_CONTEXT.memo = {}
        try:
            payload = _build_matching_queue_payload()
        finally:
            del DOUBAN_RESOLVE_CONTEXT.memo
        MATCHING_QUEUE_CACHE = payload
        MATCHING_QUEUE_CACHE_AT = time.monotonic()
        return payload


def _build_matching_queue_payload():
    items=get_items_cached(timeout=ADMIN_API_TIMEOUT)
    rows=[];counts=Counter();libraries=_library_definitions();lookup={str(i['Id']):i for i in items}
    decisions=_load_json_file(CACHE_DIR/'issue-decisions.json',{})
    for item in items:
        item = _effective_item_type(item)
        state=_safe_item_state(str(item['Id']))
        triage=_item_triage(item,state,decisions.get(str(item['Id']),{}))
        gap=issue_policy.douban_gap(item,state,_douban_cache_result(item),media_match.effective_ids(item,_rating_override(item)),triage,_rating_override(item))
        if not triage['issues'] and not gap:continue
        library=_library_for_item(item,libraries)
        counts[triage['status']]+=1
        rows.append({'id':str(item['Id']),'name':_queue_item_name(item, lookup),'year':item.get('ProductionYear'),'type':item.get('Type'),'original_title':item.get('OriginalTitle'),
                     'flags':_match_queue_flags(item),'effective_ids':media_match.effective_ids(item,_rating_override(item)),
                     'ratings':state.get('ratings') or {},'library_name':(library or {}).get('name'),'library_id':(library or {}).get('id'),
                     'reason':_douban_cache_result(item).get('status'),'triage':triage,'douban_gap':gap,'task':_task_state(item['Id']),
                     'handled':bool((_rating_override(item).get('match') or {}).get('confirmed_at')),
                     'binding_at':(_rating_override(item).get('match') or {}).get('confirmed_at'),
                     'cache_status':_douban_cache_result(item).get('status'),
                     'poster_url':_poster_url(item['Id'],state.get('signature')) if (CACHE_DIR/'rendered'/f"{item['Id']}.jpg").exists() else f"/original/{quote(str(item['Id']))}.jpg" if (CACHE_DIR/'original'/f"{item['Id']}.jpg").is_file() else None})
    priority={'action':0,'waiting':1,'info':2,'ignored':3}
    rows.sort(key=lambda r:(priority.get(r['triage']['status'],2),r['name'] or ''))
    return {'generated_at':current_time_iso(),'counts':dict(counts),'total':len(rows),'items':rows,
            'douban_gaps':issue_policy.douban_gap_counts(rows)}

ADMIN_VIEW = None
ADMIN_VIEW_LOCK = threading.Lock()


def _admin_view():
    global ADMIN_VIEW
    with ADMIN_VIEW_LOCK:
        if ADMIN_VIEW is None:
            ADMIN_VIEW = admin_snapshot.Snapshot(CACHE_DIR/'reports'/'admin-view.json', _build_admin_view,
                                                 atomic_write_text, APP_VERSION, ttl=3600)
        return ADMIN_VIEW


def _build_admin_view():
    queue = _matching_queue_payload(force=True)
    overview = _overview_payload()
    return {'queue':queue, 'overview':overview}


def _admin_view_payload(kind, force=False):
    data, meta = _admin_view().read(force)
    if data is not None:
        payload = dict(data[kind])
    elif kind == 'queue':
        payload = {'items':[], 'counts':{}, 'total':0, 'generated_at':None}
    else:
        payload = {'total':0,'rendered':0,'rendered_percent':0,'rating_covered':0,'rating_covered_percent':0,
                   'source_coverage':{},'issue_items':0,'diagnostics':{},'scan':{},'rating_activity':{},'health':{},
                   'connection':{'configured':bool(EMBY_URL and EMBY_API_KEY),'ok':False}, 'status':scan_status_snapshot()}
    payload['snapshot'] = {**meta, 'warming':data is None}
    if kind == 'overview':
        payload['status'] = scan_status_snapshot()
    return payload


def _library_card(item):
    """Poster browsing needs saved scores, not a fresh full identity audit."""
    item_id = str(item['Id'])
    saved = _safe_item_state(item_id)
    library = _library_for_item(item)
    rendered = (CACHE_DIR/'rendered'/f'{item_id}.jpg').is_file()
    original = (CACHE_DIR/'original'/f'{item_id}.jpg').is_file()
    triage = next((r['triage'] for r in (MATCHING_QUEUE_CACHE or {}).get('items',[]) if r['id']==item_id), {})
    return {'id':item_id,'name':_queue_item_name(item),'year':item.get('ProductionYear'),'type':item.get('Type'),
            'ratings':saved.get('ratings') or {},'library_name':(library or {}).get('name'),
            'attention_count':(triage.get('counts') or {}).get('action',0),
            'poster_url':_poster_url(item_id,str(saved.get('signature') or '')[:12]) if rendered else f'/original/{quote(item_id)}.jpg' if original else None}


def _admin_warm_loop():
    while True:
        try:
            _admin_view().read()
        except Exception:
            LOG.exception('admin snapshot warm failed')
        time.sleep(300)


def _issue_action(item,body):
    action=body.get('action')
    if action=='refresh':
        job=_enqueue_item(item, refresh=True)
        return {'ok':True,'queued':True,'task':job,'message':'已加入后台队列，重新读取评分并更新封面'}
    if action not in {'snooze','dismiss','restore'}:raise ValueError('处理操作无效')
    current=_item_triage(item)
    issue=next((i for i in current['issues'] if i['code']==body.get('code')),None)
    if not issue:raise ValueError('问题状态已改变，请刷新')
    with ISSUE_DECISIONS_LOCK:
        path=CACHE_DIR/'issue-decisions.json';data=_load_json_file(path,{})
        decisions=data.setdefault(str(item['Id']),{})
        if action=='restore':decisions.pop(issue['code'],None)
        else:decisions[issue['code']]={'mode':'dismiss' if action=='dismiss' else 'snooze','until':time.time()+7*86400 if action=='snooze' else 0,'fingerprint':issue['fingerprint']}
        atomic_write_text(path,json.dumps(data,ensure_ascii=False))
    _invalidate_matching_queue()
    return {'ok':True,'message':'已更新提醒状态；来源 ID 或问题类型改变时会重新提醒'}


def _webhook_authorised(handler: "PosterHandler") -> bool:
    if not WEBHOOK_TOKEN:
        return False
    supplied = (handler.headers.get("X-Webhook-Token") or "").strip()
    if not supplied:
        query = parse_qs(urlparse(handler.path).query)
        supplied = (query.get("token") or [""])[0].strip()
    return bool(supplied) and hmac.compare_digest(supplied, WEBHOOK_TOKEN)


def _parse_multipart_fields(content_type: str, body: bytes) -> dict[str, str]:
    """Flatten a multipart/form-data body into name -> text fields."""
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    message = email.message_from_bytes(header + body)
    fields: dict[str, str] = {}
    if not message.is_multipart():
        return fields
    for part in message.get_payload():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue  # attachments (e.g. the artwork image) carry no useful event data
        try:
            content = part.get_payload(decode=True) or b""
            fields[name] = content.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - a broken part must not kill the request
            fields[name] = ""
    return fields


def _parse_webhook_payload(handler: "PosterHandler") -> dict[str, Any]:
    """Accept JSON, multipart/form-data and urlencoded webhook bodies.

    Emby's Webhooks plugin posts multipart by default (it can attach artwork),
    so the JSON-only path returned 400 the moment the user saved the config.
    """
    content_type = handler.headers.get("Content-Type") or ""
    try:
        length = int(handler.headers.get("Content-Length", "0") or 0)
    except (TypeError, ValueError):
        length = 0
    raw = handler.rfile.read(length) if length else b""
    handler._body_consumed = True  # noqa: SLF001
    lowered = content_type.lower()

    fields: dict[str, str] = {}
    if "multipart" in lowered:
        fields = _parse_multipart_fields(content_type, raw)
    elif "urlencoded" in lowered:
        fields = dict(parse_qsl(raw.decode("utf-8", "replace")))
    else:
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
            return payload if isinstance(payload, dict) else {}
        except ValueError:
            fields = {}

    # Plugins commonly wrap the JSON document in a single field.
    for key in ("payload", "Payload", "body", "data"):
        value = fields.get(key)
        if isinstance(value, str) and value.lstrip().startswith("{"):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                continue

    payload: dict[str, Any] = {}
    event = fields.get("Event") or fields.get("event")
    if event:
        payload["Event"] = event
    item_raw = fields.get("Item") or fields.get("item")
    if isinstance(item_raw, str) and item_raw.strip():
        try:
            item = json.loads(item_raw)
        except ValueError:
            item = {"Name": item_raw, "Id": fields.get("Id"), "Type": fields.get("Type")}
        if isinstance(item, dict):
            payload["Item"] = item
    if not payload and fields:
        sample = {key: value[:160] for key, value in fields.items()}
        LOG.warning("webhook body not understood; content_type=%s fields=%s", content_type, sample)
    return payload


def handle_emby_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    """Queue at most one render for a newly added Movie/Series/Season."""
    event = str(payload.get("Event") or payload.get("event") or "").lower()
    if "add" not in event and "new" not in event:
        return {"ok": True, "ignored": "event", "event": event}
    item = payload.get("Item") or payload.get("item") or {}
    if not isinstance(item, dict):
        return {"ok": True, "ignored": "item"}
    item_id = str(item.get("Id") or item.get("id") or "").strip()
    item_type = str(item.get("Type") or item.get("type") or "").strip().lower()
    if not item_id or not re.fullmatch(r"[A-Za-z0-9_-]+", item_id):
        return {"ok": True, "ignored": "id"}
    if item_type and item_type not in WEBHOOK_TYPES:
        return {"ok": True, "ignored": "type", "type": item_type}

    queue = _work_queue()
    snapshot = queue.snapshot()
    if snapshot["counts"].get("queued", 0) >= WEBHOOK_MAX_QUEUE:
        LOG.warning("webhook rejected %s: queue full (%s)", item_id, snapshot["counts"])
        return {"ok": False, "reason": "queue_full", "queued": snapshot["counts"].get("queued", 0)}

    previous = queue.latest_for(item_id) or {}
    if previous.get("status") in {"queued", "running"}:
        return {"ok": True, "deduped": True, "item_id": item_id}
    finished = float(previous.get("finished_at") or 0)
    if finished and (time.time() - finished) < WEBHOOK_MIN_INTERVAL:
        return {"ok": True, "deduped": True, "reason": "cooldown", "item_id": item_id}

    job = queue.enqueue(item_id, str(item.get("Name") or ""), action="render", refresh=False,
                        context={"source": "emby-webhook", "type": item_type})
    LOG.info("webhook queued %s (%s) job=%s", item.get("Name"), item_id, job.get("id"))
    return {"ok": True, "queued": True, "item_id": item_id, "job": job.get("id")}


def notify_scan_failure(report: dict[str, Any] | None) -> None:
    """Push scan failures to an optional webhook; never raise into the scan."""
    alert_url = str(_maint("alert_webhook_url", ALERT_WEBHOOK_URL) or "").strip()
    if not alert_url:
        return
    if not report:
        message = "海报扫描失败：未生成报告（查看容器日志）"
        failed = 0
    else:
        results = report.get("results") or {}
        failed = int(results.get("failed") or 0)
        if failed < max(1, int(_maint("alert_min_failed", ALERT_MIN_FAILED))):
            return
        message = (f"海报扫描有 {failed} 项失败"
                   f"（共 {report.get('total')} 项，用时 {round(float(report.get('duration_seconds') or 0))}s）")

    def send() -> None:
        try:
            requests.post(alert_url, json={"text": message, "status": (report or {}).get("status"),
                                                   "failed": failed,
                                                   "finished_at": (report or {}).get("finished_at")},
                          timeout=8)
            LOG.info("scan alert sent: %s", message)
        except Exception:
            LOG.warning("scan alert delivery failed", exc_info=True)

    threading.Thread(target=send, name="scan-alert", daemon=True).start()


class PosterHandler(SimpleHTTPRequestHandler):
    server_version = "EmbyPosterWorker/2.0"
    # HTTP/1.1 keeps connections alive so a page load does not pay a new TCP
    # handshake for every asset. Every response below sets Content-Length.
    protocol_version = "HTTP/1.1"

    def _serve_path(self, path: Path, content_type: str | None = None) -> None:
        try:
            stat = path.stat()
            data = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        content_type = content_type or "application/octet-stream"
        etag = f'"{int(stat.st_mtime)}-{stat.st_size:x}"'
        # Assets revalidate cheaply (304); posters are versioned by ?v=, but a
        # rating refresh can reuse the same version, so keep their TTL short.
        if "image/" in content_type:
            cache_control = "public, max-age=300" if "v=" in self.path else "public, max-age=60"
        else:
            cache_control = "no-cache"
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache_control)
            self.end_headers()
            return
        body, compressed = _maybe_gzip(self, data, content_type)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        self.send_header("ETag", etag)
        self.send_header("Vary", "Accept-Encoding")
        if compressed:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # Widths the grid/list views are allowed to request. Snapping keeps the
    # thumbnail cache bounded instead of honouring arbitrary pixel widths.
    THUMB_WIDTHS = (240, 320, 400, 480, 600, 800)

    def _thumb_for(self, item_id: str, width: int) -> Path | None:
        """Return a cached thumbnail, generating it from the render on demand."""
        rendered = CACHE_DIR / "rendered" / f"{item_id}.jpg"
        try:
            rendered_mtime = rendered.stat().st_mtime
        except OSError:
            return None
        target = CACHE_DIR / "thumbnails" / str(width) / f"{item_id}.jpg"

        def fresh() -> bool:
            try:
                return target.is_file() and target.stat().st_mtime >= rendered_mtime
            except OSError:
                return False

        if fresh():
            return target
        with THUMB_LOCK:
            if fresh():
                return target
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with Image.open(rendered) as img:
                    height = max(1, round(img.height * width / img.width))
                    resized = img.convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
                temporary = target.with_name(f"{item_id}.{os.getpid()}.{threading.get_ident()}.tmp")
                resized.save(temporary, "JPEG", quality=84, optimize=True, progressive=True)
                os.replace(temporary, target)
            except (OSError, ValueError):
                return None
        return target

    def _serve_poster(self, item_id: str, query: dict[str, list[str]] | None = None) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", item_id):
            self.send_error(404)
            return
        width = 0
        if query:
            try:
                width = int((query.get("w") or ["0"])[0])
            except (TypeError, ValueError):
                width = 0
        if width > 0:
            width = min(self.THUMB_WIDTHS, key=lambda option: abs(option - width))
            thumb = self._thumb_for(item_id, width)
            if thumb is None:
                self.send_error(404)
                return
            self._serve_path(thumb, "image/jpeg")
            return
        target = CACHE_DIR / "rendered" / f"{item_id}.jpg"
        self._serve_path(target, "image/jpeg")

    def _serve_original_poster(self, item_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", item_id):
            self.send_error(404)
            return
        target = CACHE_DIR / "original" / f"{item_id}.jpg"
        self._serve_path(target, "image/jpeg")

    def _serve_frontend(self) -> None:
        index = WEB_DIR / "index.html"
        if not index.exists():
            self.send_error(503, "frontend files are missing")
            return
        self._serve_path(index, "text/html; charset=utf-8")

    def _api_path(self) -> tuple[str, list[str], dict[str, list[str]]]:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        parts = [unquote(part) for part in path.split("/") if part]
        return path, parts, parse_qs(parsed.query)

    def do_GET(self) -> None:
        path, parts, query = self._api_path()
        try:
            if path == "/health":
                _json_response(self, scan_status_snapshot())
                return
            if path == "/api/session":
                auth = _ensure_admin_auth()
                _json_response(self, {"authenticated": bool(_session_username(self)), "username": _session_username(self), "force_change": bool(auth.get("force_change")), "app_version": APP_VERSION})
                return
            if path == "/api/posters" or (len(parts) == 2 and parts[0] == "api" and parts[1] == "posters"):
                self.send_error(404)
                return
            if len(parts) >= 3 and parts[0] == "api" and parts[1] == "posters":
                self._serve_poster(parts[2])
                return
            if len(parts) == 2 and parts[0] == "original" and parts[1].endswith(".jpg"):
                self._serve_original_poster(Path(parts[1]).stem)
                return
            if path.startswith("/api/"):
                if not _require_admin(self):
                    return
                if path == "/api/key-status":
                    current_status = _api_key_status_payload()
                    needs_check = any(
                        row.get("status") == "unchecked"
                        for provider in current_status.get("providers", {}).values()
                        for row in provider.get("keys", [])
                    )
                    _json_response(self, _check_api_keys() if needs_check else current_status)
                    return
                if path == "/api/config":
                    auth = _ensure_admin_auth()
                    _json_response(self, {
                        "settings": ADMIN_SETTINGS,
                        "rating_policy": {"douban_verified_only": DOUBAN_REQUIRE_VERIFIED_CACHE, "community_fallback_enabled": not DOUBAN_REQUIRE_VERIFIED_CACHE and COMMUNITY_RATING_IS_DOUBAN},
                        "maintenance": {
                            "backup": {
                                "enabled": bool(_maint("backup_enabled", BACKUP_ENABLED)),
                                "keep": int(_maint("backup_keep", BACKUP_KEEP)),
                                "hour": int(_maint("backup_hour", BACKUP_HOUR)),
                                "interval_days": int(_maint("backup_interval_days", BACKUP_INTERVAL_DAYS)),
                                "last": _read_report(BACKUP_STATUS_FILE, {}),
                            },
                            "webhook": {
                                "enabled": bool(WEBHOOK_TOKEN),
                                "url": f"{PUBLIC_BASE_URL.rstrip('/')}/emby/webhook?token={WEBHOOK_TOKEN}" if WEBHOOK_TOKEN else "",
                                "max_queue": WEBHOOK_MAX_QUEUE,
                                "cooldown_seconds": int(WEBHOOK_MIN_INTERVAL),
                            },
                            "alert": {
                                "enabled": bool(str(_maint("alert_webhook_url", ALERT_WEBHOOK_URL) or "").strip()),
                                "url": str(_maint("alert_webhook_url", ALERT_WEBHOOK_URL) or ""),
                                "min_failed": int(_maint("alert_min_failed", ALERT_MIN_FAILED)),
                            },
                            "cache_gc": {
                                "enabled": bool(_maint("cache_gc_enabled", CACHE_GC_ENABLED)),
                                "days": int(_maint("cache_gc_days", CACHE_GC_DAYS)),
                            },
                            "rating_refresh_days": {
                                source: max(1, round(seconds / 86400))
                                for source, seconds in rating_refresh_seconds().items()
                            },
                        },
                        "connections": {
                            "emby_url": EMBY_URL,
                            "has_emby_api_key": bool(EMBY_API_KEY),
                            "has_omdb_api_key": bool(OMDB_API_KEYS),
                            "omdb_api_key_count": len(OMDB_API_KEYS),
                            "has_mdblist_api_key": bool(MDBLIST_API_KEYS),
                            "mdblist_api_key_count": len(MDBLIST_API_KEYS),
                            "public_base_url": PUBLIC_BASE_URL,
                        },
                        "auth": {"username": auth.get("username", "admin"), "force_change": bool(auth.get("force_change"))},
                    })
                    return
                if path == "/api/matching-queue":
                    _json_response(self, _admin_view_payload("queue", force=(query.get("refresh") or [""])[0] == "1"))
                    return
                if path == "/api/rating-accuracy":
                    _json_response(self, _read_report(RATING_ACCURACY_FILE, {"counts": {}, "findings": []}))
                    return
                if path == "/api/rating-audit":
                    _json_response(self, _read_report(DOUBAN_AUDIT_FILE, {"status": "pending", "counts": {}, "findings": []}))
                    return
                if path == "/api/overview":
                    try:
                        _json_response(self, _admin_view_payload("overview"))
                    except requests.RequestException as exc:
                        _error_response(self, str(exc), 502)
                    return
                if path == "/api/libraries":
                    libraries = _library_definitions()
                    allowed = _enabled_library_ids()
                    for library in libraries:
                        library["enabled"] = True if allowed is None else str(library.get("id")) in allowed
                    _json_response(self, {"items": libraries, "restricted": allowed is not None})
                    return
                if path == "/api/items":
                    search = str((query.get("q") or [""])[0]).strip()
                    issue_only = str((query.get("issues") or [""])[0]).lower() in {"1", "true", "yes"}
                    missing_rating = str((query.get("missing_rating") or [""])[0]).lower() in {"1", "true", "yes"}
                    rating_mismatch = str((query.get("rating_mismatch") or [""])[0]).lower() in {"1", "true", "yes"}
                    library_id = str((query.get("library_id") or [""])[0]).strip()
                    triage_filter=str((query.get("triage") or [""])[0])
                    if triage_filter not in {"","action","waiting","info","ignored"}:raise ValueError("处理分类无效")
                    match_filter = str((query.get("match_filter") or [""])[0])
                    if match_filter not in {"", "matching", "missing_both_ids", "missing_douban_id", "missing_imdb_id", "identity_conflict", "ids_without_score"}:
                        raise ValueError("匹配筛选无效")
                    try:
                        page = max(1, int((query.get("page") or ["1"])[0]))
                        page_size = min(100, max(1, int((query.get("page_size") or ["24"])[0])))
                    except ValueError:
                        page, page_size = 1, 24
                    if not issue_only and not missing_rating and not rating_mismatch and not match_filter and not triage_filter and EMBY_URL and EMBY_API_KEY:
                        try:
                            sort_key = str((query.get("sort") or ["name"])[0]).lower()
                            if sort_key not in ITEM_SORT_OPTIONS:
                                sort_key = "name"
                            type_filter = str((query.get("type") or [""])[0])
                            if type_filter not in ITEM_TYPE_FILTERS:
                                type_filter = ""
                            page_items, total = get_item_page((page - 1) * page_size, page_size, library_id, search,
                                                              ADMIN_API_TIMEOUT, sort=sort_key, item_type=type_filter)
                            _json_response(self, {"items": [_library_card(item) if (query.get("brief") or [""])[0]=="1" else _item_summary(item) for item in page_items], "total": total, "page": page, "page_size": page_size})
                        except requests.RequestException as exc:
                            _error_response(self, str(exc), 502)
                        return
                    try:
                        items = get_items_cached(timeout=ADMIN_API_TIMEOUT) if EMBY_URL and EMBY_API_KEY else []
                    except requests.RequestException as exc:
                        _error_response(self, str(exc), 502)
                        return
                    libraries = _library_definitions()
                    candidates = []
                    for item in items:
                        if triage_filter and _item_triage(item)["status"]!=triage_filter:continue
                        if match_filter:
                            flags = _match_queue_flags(item)
                            if not flags or match_filter != "matching" and match_filter not in flags:
                                continue
                        library = _library_for_item(item, libraries)
                        if library_id and str(library.get("id") if library else "") != library_id:
                            continue
                        if search and search not in str(item.get("Name") or "").casefold() and search not in str(item.get("OriginalTitle") or "").casefold():
                            continue
                        candidates.append(item)
                    if issue_only or missing_rating or rating_mismatch:
                        filtered = []
                        for item in candidates:
                            summary = _item_summary(item)
                            if issue_only and summary["triage"]["status"]!="action":
                                continue
                            if missing_rating and len(summary.get("ratings") or {}) >= 3:
                                continue
                            if rating_mismatch and not set(summary.get("diagnostic_codes") or {}).intersection({"douban_stale", "douban_identity", "omdb_api_invalid", "omdb_unavailable"}):
                                continue
                            filtered.append((item, summary))
                        filtered.sort(key=lambda entry: (str(entry[1].get("name") or "").casefold(), str(entry[1].get("year") or "")))
                        total = len(filtered)
                        summaries = [summary for _, summary in filtered[(page - 1) * page_size:page * page_size]]
                    else:
                        candidates.sort(key=lambda item: (str(item.get("Name") or "").casefold(), str(item.get("ProductionYear") or "")))
                        total = len(candidates)
                        summaries = [_item_summary(item) for item in candidates[(page - 1) * page_size:page * page_size]]
                    _json_response(self, {"items": summaries, "total": total, "page": page, "page_size": page_size})
                    return
                if len(parts) >= 3 and parts[1] == "items":
                    item_id = parts[2]
                    item = _get_item_by_id(item_id)
                    if item is None:
                        _error_response(self, "资源不存在或 Emby 不可用", 404)
                        return
                    summary = _item_summary(item)
                    summary["overview"] = item.get("Overview")
                    summary["path"] = item.get("Path")
                    summary["media_sources"] = item.get("MediaSources") or []
                    summary["media_streams"] = item.get("MediaStreams") or []
                    _json_response(self, summary)
                    return
                if path == "/api/reports/health":
                    _json_response(self, _read_report(MEDIA_HEALTH_REPORT_FILE, {"summary": {}, "items": []}))
                    return
                if path == "/api/reports/review":
                    _json_response(self, _read_report(REVIEW_REPORT_FILE, {"generated_at": None, "items": []}))
                    return
                if path == "/api/tasks":
                    _json_response(self, _work_queue().snapshot())
                    return
                if path == "/api/scan/status":
                    _json_response(self, scan_status_snapshot())
                    return
                if path == "/api/style-preview/image":
                    preview = CACHE_DIR / "previews" / "style-preview.jpg"
                    self._serve_path(preview, "image/jpeg")
                    return
                _error_response(self, "未知接口", 404)
                return
            if path.endswith(".jpg") and path.count("/") == 1:
                self._serve_poster(Path(path).stem, query)
                return
            if path in {"/", "/admin", "/admin/", "/index.html"}:
                self._serve_frontend()
                return
            if path in {"/app.js", "/match.js", "/style-tools.js", "/styles.css"}:
                static_file = WEB_DIR / path.lstrip("/")
                content_type = "application/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8"
                self._serve_path(static_file, content_type)
                return
            super().do_GET()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            LOG.exception("GET request failed: %s", exc)
            if not self.wfile.closed:
                _error_response(self, str(exc), 500)

    def handle_one_request(self) -> None:
        # One handler instance serves every request on a keep-alive connection,
        # so the drain flag must be reset per request.
        self._body_consumed = False
        super().handle_one_request()

    def do_HEAD(self) -> None:
        # Same routing as GET so HEAD responses carry the real headers
        # (Content-Length, ETag, Content-Encoding) without a body.
        self.do_GET()

    def do_POST(self) -> None:
        path, parts, _query = self._api_path()
        try:
            if path == "/emby/webhook":
                if not _webhook_authorised(self):
                    _error_response(self, "webhook disabled or token invalid", 403)
                    return
                result = handle_emby_webhook(_parse_webhook_payload(self))
                _json_response(self, result, 202 if result.get("queued") else 200)
                return
            if path == "/api/auth/login":
                body = _json_body(self)
                auth = _ensure_admin_auth()
                username = str(body.get("username") or "")
                password = str(body.get("password") or "")
                if username != str(auth.get("username")) or not _verify_password(password, auth):
                    _error_response(self, "账号或密码错误", 401)
                    return
                token = _new_session(username)
                headers = {"Set-Cookie": f"pw_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={ADMIN_SESSION_TTL}"}
                _json_response(self, {"ok": True, "force_change": bool(auth.get("force_change"))}, headers=headers)
                return
            if path == "/api/auth/logout":
                _destroy_session(self)
                _json_response(self, {"ok": True}, headers={"Set-Cookie": "pw_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})
                return
            if path == "/api/auth/password":
                if not _require_admin(self):
                    return
                body = _json_body(self)
                current = str(body.get("current_password") or "")
                new_password = str(body.get("new_password") or "")
                auth = _ensure_admin_auth()
                if not _verify_password(current, auth):
                    _error_response(self, "当前密码不正确", 400)
                    return
                if len(new_password) < 8:
                    _error_response(self, "新密码至少需要 8 位", 400)
                    return
                salt, password_hash = _hash_password(new_password)
                auth.update({"salt": salt, "password_hash": password_hash, "force_change": False})
                atomic_write_text(ADMIN_AUTH_FILE, json.dumps(auth, ensure_ascii=False, indent=2))
                _json_response(self, {"ok": True})
                return
            if not _require_admin(self):
                return
            if path == "/api/scan":
                started = _start_scan_thread()
                _json_response(self, {"started": started, "status": scan_status_snapshot()}, 202 if started else 409)
                return
            if path == "/api/key-config":
                body = _json_body(self)
                provider = str(body.get("provider") or "").strip().lower()
                try:
                    index = int(body.get("index") or 0)
                    remove = bool(body.get("remove"))
                    status = _update_provider_key(provider, index, body.get("key"), remove=remove)
                    if not remove:
                        keys = _provider_key_list(provider)
                        key_status = _check_single_api_key(provider, index, keys[index - 1])
                    else:
                        key_status = None
                    _json_response(self, {"ok": True, "status": _api_key_status_payload(), "key_status": key_status})
                except (TypeError, ValueError) as exc:
                    _error_response(self, str(exc), 400)
                return
            if path == "/api/key-status/check":
                body = _json_body(self)
                provider = str(body.get("provider") or "").strip().lower()
                if provider and body.get("index") is not None:
                    try:
                        index = int(body.get("index") or 0)
                        keys = _provider_key_list(provider)
                        if index < 1 or index > len(keys):
                            raise ValueError("API Key 不存在")
                        key_status = _check_single_api_key(provider, index, keys[index - 1])
                        _json_response(self, {"ok": True, "key_status": key_status, "status": _api_key_status_payload()})
                    except (TypeError, ValueError) as exc:
                        _error_response(self, str(exc), 400)
                else:
                    _json_response(self, _check_api_keys())
                return
            if path == "/api/config":
                body = _json_body(self)
                settings = body.get("settings") if isinstance(body.get("settings"), dict) else dict(ADMIN_SETTINGS)
                if body.get("public_base_url"):
                    settings["public_base_url"] = str(body["public_base_url"]).strip().rstrip("/")
                incoming_maintenance = body.get("maintenance")
                if not isinstance(incoming_maintenance, dict):
                    incoming_maintenance = settings.get("maintenance") if isinstance(settings.get("maintenance"), dict) else None
                if isinstance(incoming_maintenance, dict):
                    settings["maintenance"] = _normalise_maintenance(incoming_maintenance, ADMIN_SETTINGS.get("maintenance"))
                _save_admin_settings(settings)
                secret_fields = ("emby_url", "emby_api_key", "omdb_api_keys", "mdblist_api_keys")
                secret_changed = False
                secrets_payload = dict(_ADMIN_SECRETS)
                for field in secret_fields:
                    if field in body and body[field] not in (None, ""):
                        secrets_payload[field] = str(body[field]).strip()
                        secret_changed = True
                # A style/settings-only save must never erase credentials that
                # came from the environment or were already persisted.
                if secret_changed:
                    _save_admin_secrets(secrets_payload)
                SCAN_WAKE_EVENT.set()
                _json_response(self, {"ok": True, "settings": ADMIN_SETTINGS, "connections": {
                    "emby_url": EMBY_URL,
                    "has_emby_api_key": bool(EMBY_API_KEY),
                    "has_omdb_api_key": bool(OMDB_API_KEYS),
                    "omdb_api_key_count": len(OMDB_API_KEYS),
                    "has_mdblist_api_key": bool(MDBLIST_API_KEYS),
                    "mdblist_api_key_count": len(MDBLIST_API_KEYS),
                }})
                return
            if path == "/api/connection/test":
                _json_response(self, {
                    "emby": _connection_status(),
                    "omdb_configured": bool(OMDB_API_KEYS),
                    "mdblist_configured": bool(MDBLIST_API_KEYS),
                })
                return
            if path == "/api/style-preview":
                body = _json_body(self)
                payload = render_style_preview(body.get("settings") if isinstance(body.get("settings"), dict) else DEFAULT_RENDER_SETTINGS, body.get("rating_display_strategy"))
                _json_response(self, {"ok": True, **payload})
                return
            if path == "/api/tasks/clear":
                removed = _work_queue().clear_finished()
                _invalidate_matching_queue()
                _json_response(self, {"ok": True, "removed": removed})
                return
            if len(parts)==4 and parts[0]=='api' and parts[1]=='tasks' and parts[3]=='retry':
                job = _work_queue().retry(parts[2])
                _invalidate_matching_queue()
                _json_response(self, {'ok': True, 'queued': True, 'task': job}, 202)
                return
            if len(parts)==4 and parts[1]=='items' and parts[3]=='issue-action':
                item=_get_item_by_id(parts[2])
                if item is None:raise ValueError('资源不存在或 Emby 暂不可用')
                _json_response(self,_issue_action(item,_json_body(self)),202)
                return
            if len(parts) == 4 and parts[1] == "items" and parts[3] in {"match-search", "match-preview", "match-confirm"}:
                item = _get_item_by_id(parts[2])
                if item is None:
                    _error_response(self,"资源不存在或 Emby 不可用",404)
                    return
                body = _json_body(self)
                action={"match-search":_match_search,"match-preview":_match_preview,"match-confirm":_match_confirm}[parts[3]]
                _json_response(self,action(item,body),202 if parts[3]=="match-confirm" else 200)
                return
            if len(parts) >= 4 and parts[1] == "items" and parts[3] == "override":
                item_id = parts[2]
                item = _get_item_by_id(item_id)
                if item is None:
                    _error_response(self, "资源不存在或 Emby 不可用", 404)
                    return
                body = _json_body(self)
                with MATCH_EDIT_LOCK:
                    override = _update_rating_override(item_id, body)
                _invalidate_matching_queue()
                queued = _start_item_render(_get_item_by_id(item_id) or item) if bool(body.get("render", True)) else False
                _json_response(self, {"ok": True, "queued": queued, "task": _task_state(item_id), "override": override, "item": _item_summary(item)}, 202)
                return
            if path == "/api/items/bulk":
                body = _json_body(self)
                _json_response(self, _bulk_enqueue(body.get("ids"), body.get("action")), 202)
                return
            if len(parts) >= 4 and parts[1] == "items" and parts[3] == "render":
                item_id = parts[2]
                item = _get_item_by_id(item_id)
                if item is None:
                    _error_response(self, "资源不存在或 Emby 不可用", 404)
                    return
                if not _start_item_render(item):
                    _error_response(self, "当前有扫描或渲染任务正在执行，请稍后再试", 409)
                    return
                _json_response(self, {"ok": True, "status": "queued", "task": _task_state(item_id)}, 202)
                return
            if len(parts) >= 4 and parts[1] == "items" and parts[3] == "restore":
                item_id = parts[2]
                original = CACHE_DIR / "original" / f"{item_id}.jpg"
                if not original.is_file():
                    _error_response(self, "没有可恢复的原图缓存", 404)
                    return
                item = _get_item_by_id(item_id)
                if not item:
                    _error_response(self, "资源不存在或 Emby 不可用", 404)
                    return
                job = _enqueue_item(item, action='restore')
                _json_response(self, {"ok": True, "queued": True, "task": job}, 202)
                return
            _error_response(self, "未知接口", 404)
        except ValueError as exc:
            _error_response(self, str(exc), 400)
        except requests.RequestException as exc:
            _error_response(self, str(exc), 502)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            LOG.exception("POST request failed: %s", exc)
            _error_response(self, str(exc), 500)

    def log_message(self, format: str, *args: Any) -> None:
        message = format % args
        if LOG_HTTP_REQUESTS or '" 200 ' not in message and '" 304 ' not in message:
            LOG.info("poster http: %s", message)


def start_http_server() -> None:
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    handler = partial(PosterHandler, directory=str(WEB_DIR))
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    LOG.info("poster HTTP server listening on 0.0.0.0:%s, public base URL %s", HTTP_PORT, PUBLIC_BASE_URL)


def backfill_boxset_covers() -> dict[str, Any]:
    """补齐缺封面的合集。

    Emby 的库扫描只建 BoxSet 条目、不下载封面；由插件写进来的 collection.nfo
    只带 tmdbid，图片要等一次图片刷新才会从 TMDB 拉下来。这里在每次扫描末尾
    做一次兜底，缺图就补，TMDB 确实没有合集海报时复用成员影片的封面。
    """
    if DRY_RUN:
        # 补合集封面会触发 Emby 的图片刷新并可能写入图片，试运行时必须跳过。
        return {"enabled": False, "skipped": "dry_run"}
    if not BOXSET_COVERS_ENABLED or not EMBY_URL or not EMBY_API_KEY:
        return {"enabled": False}
    try:
        result = boxset_covers.backfill(
            emby,
            BOXSET_COVERS_STATE_FILE,
            retry_days=BOXSET_COVERS_RETRY_DAYS,
            log=lambda message: LOG.info(message),
        )
    except Exception as exc:
        LOG.exception("boxset cover backfill failed")
        return {"enabled": True, "error": str(exc)}
    result.pop("items", None)
    return result


def get_items(timeout: float | None = None) -> list[dict[str, Any]]:
    """Read the complete Movie/Series set, not just Emby's first page."""
    fields = "ProviderIds,MediaSources,MediaStreams,Path,OriginalTitle,Genres,CommunityRating,CriticRating,ProductionYear,DateCreated,DateLastMediaAdded,DateModified,ImageTags,ParentId,SeriesId,IndexNumber"
    base_params: dict[str, str] = {"Fields": fields}
    if ITEM_IDS:
        base_params["Ids"] = ",".join(sorted(ITEM_IDS))
        request_kwargs: dict[str, Any] = {"params": base_params}
        if timeout is not None:
            request_kwargs["timeout"] = timeout
        return emby("GET", "/Items", **request_kwargs).json().get("Items", [])

    base_params.update({"Recursive": "true", "IncludeItemTypes": "Movie,Series,Season" if SEASON_POSTERS_ENABLED else "Movie,Series"})
    if LIBRARY_SCOPE_ENABLED and not LIBRARY_IDS:
        LOG.warning("library scope is enabled but no library is selected; nothing will be processed")
        return []
    if LIBRARY_IDS:
        base_params["ParentIds"] = ",".join(sorted(LIBRARY_IDS))
    items: list[dict[str, Any]] = []
    start_index = 0
    seen_page_keys: set[tuple[str, ...]] = set()
    while True:
        params = {**base_params, "StartIndex": str(start_index), "Limit": str(EMBY_PAGE_SIZE)}
        request_kwargs = {"params": params}
        if timeout is not None:
            request_kwargs["timeout"] = timeout
        payload = emby("GET", "/Items", **request_kwargs).json()
        page = payload.get("Items") or []
        if not isinstance(page, list):
            break
        page_key = tuple(str(item.get("Id") or "") for item in page[:5])
        if page_key and page_key in seen_page_keys:
            LOG.warning("Emby pagination repeated page at StartIndex=%s; stopping to avoid an infinite scan", start_index)
            break
        if page_key:
            seen_page_keys.add(page_key)
        items.extend(page)
        total = int(payload.get("TotalRecordCount") or 0)
        if not page or len(page) < EMBY_PAGE_SIZE or (total and len(items) >= total) or len(items) >= 100_000:
            break
        start_index += len(page)
    if LIBRARY_SCOPE_ENABLED:
        # ParentIds is honoured by most Emby builds but ignored for some
        # virtual/mixed libraries, so enforce the allow-list again by path.
        kept = [item for item in items if library_enabled(item)]
        if len(kept) != len(items):
            LOG.info("library scope: %s items outside the enabled libraries were skipped",
                     len(items) - len(kept))
        items = kept
    return items


ITEM_SORT_OPTIONS: dict[str, tuple[str, str]] = {
    "name": ("SortName", "Ascending"),
    "year": ("ProductionYear", "Descending"),
    "rating": ("CommunityRating", "Descending"),
    "added": ("DateCreated", "Descending"),
}
ITEM_TYPE_FILTERS = {"Movie", "Series", "Season"}


def _local_sort_key(sort: str):
    """Ascending key for one sort mode; direction is applied by _sort_items."""
    if sort == "year":
        return lambda item: (int(item.get("ProductionYear") or 0),
                             str(item.get("SortName") or item.get("Name") or "").casefold())
    if sort == "rating":
        return lambda item: (float(item.get("CommunityRating") or 0),
                             str(item.get("SortName") or item.get("Name") or "").casefold())
    if sort == "added":
        return lambda item: (str(item.get("DateCreated") or ""),)
    return lambda item: (str(item.get("SortName") or item.get("Name") or "").casefold(),
                         str(item.get("ProductionYear") or ""))


def _sort_items(items: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    """Newest/highest first for year, rating and added; A→Z for name."""
    return sorted(items, key=_local_sort_key(sort), reverse=sort in {"year", "rating", "added"})


def get_item_page(start_index: int, limit: int, library_id: str = "", search: str = "",
                  timeout: float | None = None, sort: str = "name", item_type: str = "") -> tuple[list[dict[str, Any]], int]:
    """Return a page of items, applying library filtering by resolved path.

    Some Emby versions accept ParentIds but ignore it for mixed/virtual
    libraries. Resolve the library from each item's path as a reliable fallback.
    """
    start_index = max(0, start_index)
    limit = max(1, min(100, limit))
    # When the library scope is enabled, browse must go through the scoped
    # cache: Emby's ParentIds filter is unreliable (it returns items from
    # libraries outside the allow-list), so server-side paging would leak
    # excluded libraries into the grid.
    if library_id or LIBRARY_IDS:
        libraries = _library_definitions()
        items = get_items_cached(timeout=timeout)
        search_term = search.casefold()
        filtered = []
        for item in items:
            if not library_enabled(item):
                continue
            library = _library_for_item(item, libraries)
            if library_id and str((library or {}).get("id") or "") != library_id:
                continue
            if search_term and search_term not in str(item.get("Name") or "").casefold() and search_term not in str(item.get("OriginalTitle") or "").casefold():
                continue
            if item_type and str(item.get("Type") or "") != item_type:
                continue
            filtered.append(item)
        filtered = _sort_items(filtered, sort)
        return filtered[start_index:start_index + limit], len(filtered)

    fields = "ProviderIds,MediaSources,MediaStreams,Path,OriginalTitle,Genres,CommunityRating,CriticRating,ProductionYear,DateCreated,DateLastMediaAdded,DateModified,ImageTags,ParentId,SeriesId,IndexNumber"
    sort_by, sort_order = ITEM_SORT_OPTIONS.get(str(sort or "name"), ITEM_SORT_OPTIONS["name"])
    include_types = item_type if item_type in ITEM_TYPE_FILTERS else ("Movie,Series,Season" if SEASON_POSTERS_ENABLED else "Movie,Series")
    params: dict[str, str] = {
        "Recursive": "true",
        "IncludeItemTypes": include_types,
        "Fields": fields,
        "StartIndex": str(start_index),
        "Limit": str(limit),
        "SortBy": sort_by,
        "SortOrder": sort_order,
    }
    if search:
        params["SearchTerm"] = search
    elif LIBRARY_IDS:
        params["ParentIds"] = ",".join(sorted(LIBRARY_IDS))
    request_kwargs: dict[str, Any] = {"params": params}
    if timeout is not None:
        request_kwargs["timeout"] = timeout
    payload = emby("GET", "/Items", **request_kwargs).json()
    return payload.get("Items") or [], int(payload.get("TotalRecordCount") or 0)

def get_items_cached(timeout: float | None = None, force: bool = False) -> list[dict[str, Any]]:
    global ITEM_CACHE, ITEM_CACHE_AT
    with ITEM_CACHE_LOCK:
        if not force and ITEM_CACHE and time.time() - ITEM_CACHE_AT < 30:
            return list(ITEM_CACHE)
    items = get_items(timeout=timeout)
    with ITEM_CACHE_LOCK:
        ITEM_CACHE = list(items)
        ITEM_CACHE_AT = time.time()
    return list(items)


def process(item: dict[str, Any], rating_stats: Counter[str] | None = None, review_items: list[dict[str, Any]] | None = None, health_items: list[dict[str, Any]] | None = None) -> str:
    item = _effective_item_type(item)
    item_id = item.get("Id")
    if not item_id or item.get("Type") not in ({"Movie", "Series", "Season"} if SEASON_POSTERS_ENABLED else {"Movie", "Series"}):
        return "ignored"
    if not library_enabled(item):
        return "ignored"
    if item.get("Type") in {"Series", "Season"}:
        item = enrich_series(item)
    health = media_health(item)
    if health_items is not None and health.get("issues"):
        with REVIEW_ITEMS_LOCK:
            health_items.append(health)
    state_file = CACHE_DIR / "state" / f"{item_id}.json"
    state: dict[str, Any] = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}

    now = time.time()
    current_item_image_tag = str((item.get("ImageTags") or {}).get("Primary", ""))
    current_media_signature = media_signature(item)
    current_override_signature = rating_override_signature(item)
    cached_ratings = state.get("ratings") if isinstance(state.get("ratings"), dict) else None
    cached_rating_details = state.get("rating_details") if isinstance(state.get("rating_details"), dict) else {}
    ratings_checked_at = float(state.get("ratings_checked_at", 0) or 0)
    checked_by_source = state.get("rating_checked_at") if isinstance(state.get("rating_checked_at"), dict) else {}
    if not checked_by_source:
        checked_by_source = {source: ratings_checked_at for source in rating_refresh_seconds()}
    due_sources = {
        source
        for source, refresh_seconds in rating_refresh_seconds().items()
        if now - float(checked_by_source.get(source, 0) or 0) >= refresh_seconds
    }
    current_rating_identity = rating_identity(item)
    if not rating_identity_matches(state.get("rating_identity"), current_rating_identity):
        # Older state has no identity binding. Re-resolve every source before
        # any previous score can be drawn on a new or corrected media match.
        due_sources.update(rating_refresh_seconds())
        cached_ratings = None
        cached_rating_details = {}
    if state.get("rating_provider_signature") == rating_provider_signature(4):
        # A code-only normalization migration checks local Douban immediately;
        # external sources keep their existing TTL to avoid a full-library burst.
        due_sources.add("豆瓣")
    elif state.get("rating_provider_signature") != rating_provider_signature():
        # Actual policy/credential changes affect every source.
        due_sources.update({"豆瓣", "IMDb", "RT"})
    if community_rating_changed(item, cached_ratings, cached_rating_details):
        due_sources.add("豆瓣")
    if (_douban_cache_result(item).get("status") in {"identity_mismatch", "media_type_mismatch"}
            and not _override_value(_rating_override(item), "imdb_id", "imdbid")):
        # A known wrong-season/remake binding is not safe for other sources
        # either. Clear automatic values, while explicit manual scores survive.
        if any((cached_ratings or {}).get(source) and
               (cached_rating_details.get(source) or {}).get("source") != "rating_overrides"
               for source in ("IMDb", "RT")):
            due_sources.update({"IMDb", "RT"})
    if (
        cached_ratings is None
        or set(cached_rating_details) != set(rating_refresh_seconds())
        or state.get("rating_overrides_signature", "") != current_override_signature
    ):
        # Migrate pre-V1.5 state files that have values but no provenance.
        due_sources = set(rating_refresh_seconds())
    # An empty rating result is still a checked result. Respect per-source
    # timestamps instead of retrying every hourly media scan and hammering OMDb.
    ratings_due = bool(due_sources)
    if due_sources:
        rating_result = rating_details(item, due_sources, cached_ratings, cached_rating_details)
        score_data = {str(key): str(value) for key, value in rating_result["values"].items()}
        rating_detail_data = rating_result["details"]
        refreshed_at = {**checked_by_source, **{source: now for source in due_sources}}
    else:
        # Nothing is due, but Douban is a local file lookup: re-validate it so a
        # score whose identity vanished upstream never stays on the poster.
        rating_result = rating_details(item, set(), cached_ratings, cached_rating_details)
        score_data = {str(key): str(value) for key, value in rating_result["values"].items()}
        rating_detail_data = rating_result["details"]
        refreshed_at = checked_by_source
        if score_data != {str(k): str(v) for k, v in cached_ratings.items()}:
            ratings_due = True
    # Migrate legacy success-without-score statuses even on scans that are not due.
    for source, info in rating_detail_data.items():
        if info.get("status") == "ok" and not score_data.get(source):
            rating_detail_data[source] = {**info, "status": "no_rating"}
            ratings_due = True
    update_rating_stats(rating_stats, rating_detail_data, score_data)
    if review_items is not None and any(
        isinstance(info, dict) and info.get("status") not in {"ok", "override"}
        for info in rating_detail_data.values()
    ):
        with REVIEW_ITEMS_LOCK:
            review_items.append({"id": item_id, "name": item.get("Name"), "year": item.get("ProductionYear"), "ratings": score_data, "rating_details": rating_detail_data})
    if state.get("rating_provider_signature") != rating_provider_signature():
        ratings_due = True
    current_signature = signature(item, score_data)
    # 试运行必须真的渲染一遍才有意义，所以 DRY_RUN 时不走 unchanged 短路
    # （进入本分支即意味着 DRY_RUN 为假，无需在内部再判断）。
    if not FORCE_RENDER and not DRY_RUN and state.get("signature") == current_signature and state.get("image_tag") == current_item_image_tag:
        if ratings_due:
            atomic_write_text(state_file, json.dumps({
                **state,
                "ratings": score_data,
                "rating_details": rating_detail_data,
                "ratings_checked_at": now,
                "rating_checked_at": refreshed_at,
                "rating_overrides_signature": current_override_signature,
                "rating_provider_signature": rating_provider_signature(),
                "rating_identity": current_rating_identity,
                "media_signature": current_media_signature,
            }, ensure_ascii=False))
        return "unchanged"
    refresh_original = bool(
        state.get("render_version") == RENDER_VERSION
        and (
            (state.get("image_tag") and current_item_image_tag and state.get("image_tag") != current_item_image_tag)
            or (state.get("media_signature") and state.get("media_signature") != current_media_signature)
        )
    )
    season_parent_id = str(item.get("SeriesId")) if item.get("Type") == "Season" and item.get("SeriesId") else None
    try:
        original = get_original(item_id, refresh=refresh_original, fallback_item_id=season_parent_id)
    except FileNotFoundError as exc:
        # Some Emby items legitimately have no poster. Skip them without
        # turning the whole scan into a noisy exception loop.
        LOG.warning("skipping %s (%s): %s", item.get("Name"), item_id, exc)
        return "skipped"
    rendered = CACHE_DIR / "rendered" / f"{item_id}.jpg"
    with RENDER_LOCK:
        render(item, original, rendered, score_data)
    if REQUIRE_ALL_RATINGS and not all(score_data.get(name) for name in ("豆瓣", "IMDb", "RT")):
        LOG.info("preview only for %s (%s): missing one or more ratings", item.get("Name"), item_id)
        return "preview"
    if DRY_RUN:
        # 渲染文件已经落在 cache/rendered/ 里，可直接打开核对；不写 state、不动 Emby。
        LOG.info("dry-run: rendered %s (%s) -> %s; not published to Emby",
                 item.get("Name"), item_id, rendered)
        return "dry-run"
    deliver_image(item_id, rendered)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(state_file, json.dumps({
        "app_version": APP_VERSION,
        "render_version": RENDER_VERSION,
        "signature": current_signature,
        "image_tag": current_image_tag(item_id),
        "ratings": score_data,
        "rating_details": rating_detail_data,
        "ratings_checked_at": now,
        "rating_checked_at": refreshed_at,
        "rating_overrides_signature": current_override_signature,
        "rating_provider_signature": rating_provider_signature(),
        "rating_identity": current_rating_identity,
        "media_signature": current_media_signature,
        "rendered_at": current_time_iso(),
    }, ensure_ascii=False))
    if LOG_ITEM_UPDATES or any(info.get("status") not in {"ok", "override"} for info in rating_detail_data.values() if isinstance(info, dict)):
        LOG.info("updated %s (%s): quality=%s ratings=%s", item.get("Name"), item_id, quality_labels(item), score_data)
        LOG.debug("rating details %s (%s): %s", item.get("Name"), item_id, rating_detail_data)
    return "updated"


def _library_fingerprint() -> str | None:
    """Cheap probe: did anything in the Emby library change at all?"""
    try:
        counts = emby("GET", "/Items/Counts", timeout=ADMIN_API_TIMEOUT).json()
        latest = emby("GET", "/Items", params={
            "Recursive": "true", "IncludeItemTypes": "Movie,Series,Season",
            "SortBy": "DateModified", "SortOrder": "Descending", "Limit": 1,
            "Fields": "DateModified,DateLastMediaAdded",
        }, timeout=ADMIN_API_TIMEOUT).json()
        items = latest.get("Items") or []
        head = items[0] if items else {}
        return json.dumps({
            "movies": counts.get("MovieCount"), "series": counts.get("SeriesCount"),
            "episodes": counts.get("EpisodeCount"), "seasons": counts.get("SeasonCount"),
            "head_id": head.get("Id"), "modified": head.get("DateModified"),
            "added": head.get("DateLastMediaAdded"),
        }, sort_keys=True)
    except (requests.RequestException, ValueError, TypeError):
        return None


def _next_rating_due() -> float:
    """Earliest moment any item's rating refresh becomes due again."""
    now = time.time()
    horizon = now + max(rating_refresh_seconds().values() or [86400], default=86400)
    earliest = horizon
    for path in (CACHE_DIR / "state").glob("*.json"):
        state = _load_json_file(path, {})
        if not state.get("id"):
            continue  # auxiliary files (boxset-covers.json) share this directory
        checked = state.get("rating_checked_at") if isinstance(state.get("rating_checked_at"), dict) else {}
        if not checked:
            return now  # an item without rating history is always due
        for source, checked_at in checked.items():
            refresh = rating_refresh_seconds().get(str(source))
            if refresh:
                earliest = min(earliest, float(checked_at or 0) + refresh)
    return earliest


def run_scan_once(force: bool = False) -> dict[str, Any] | None:
    global ITEM_CACHE, ITEM_CACHE_AT
    if not EMBY_URL or not EMBY_API_KEY:
        update_scan_status(status="needs_configuration", last_error="EMBY_URL and EMBY_API_KEY are required", finished_at=current_time_iso())
        return None
    if not SCAN_RUN_LOCK.acquire(blocking=False):
        LOG.info("scan skipped because another scan is already running")
        return None
    scan_started = time.monotonic()
    scan_started_at = current_time_iso()
    try:
        if not force:
            # Scheduled runs probe first: an unchanged library with no rating
            # refresh due costs two cheap Emby calls instead of a full scan.
            fingerprint = _library_fingerprint()
            previous = _read_report(SCAN_REPORT_FILE, {})
            if (fingerprint and previous.get("library_fingerprint") == fingerprint
                    and time.time() < float(previous.get("ratings_due_at") or 0)):
                report = {
                    "status": "ok", "started_at": scan_started_at, "finished_at": current_time_iso(),
                    "duration_seconds": round(time.monotonic() - scan_started, 1),
                    "total": int(previous.get("total") or 0),
                    "results": {"skipped_unchanged": 1},
                    "skipped_reason": "library_unchanged",
                    "library_fingerprint": fingerprint,
                    "ratings_due_at": previous.get("ratings_due_at"),
                    "rating_results": {}, "failed_items": [], "review_items": [], "health_items": [],
                    "cache": {},
                }
                write_scan_report(report)
                update_scan_status(status="ok", finished_at=current_time_iso(), processed=0, total=0,
                                   current_item=None, last_error=None,
                                   elapsed_seconds=round(time.monotonic() - scan_started, 1))
                due_text = datetime.fromtimestamp(float(previous["ratings_due_at"]), _local_zone()).isoformat(timespec="seconds")
                LOG.info("scan skipped: library unchanged; next rating refresh due %s", due_text)
                return report
        update_scan_status(status="loading_library", started_at=scan_started_at, finished_at=None, current_item="正在读取 Emby 影视库…", last_progress_at=current_time_iso(), elapsed_seconds=0, last_error=None)
        items = get_items(timeout=EMBY_REQUEST_TIMEOUT)
        with ITEM_CACHE_LOCK:
            ITEM_CACHE = list(items)
            ITEM_CACHE_AT = time.time()
        item_ids = {str(item.get("Id")) for item in items if item.get("Id")}
        douban_audit = audit_douban_ratings(items)
        mdblist_prefetch = prefetch_mdblist_ratings(items)
        rating_stats: Counter[str] = Counter()
        result_counts: Counter[str] = Counter()
        failed_items: list[dict[str, Any]] = []
        review_items: list[dict[str, Any]] = []
        health_items: list[dict[str, Any]] = []
        update_scan_status(
            status="scanning",
            started_at=scan_started_at,
            finished_at=None,
            total=len(items),
            processed=0,
            updated=0,
            failed=0,
            skipped=0,
            last_error=None,
        )
        LOG.info("scan started: %d items; rating inputs=%s", len(items), rating_input_summary(items))
        # 放在处理条目前面：合集封面是独立的一小步，缺图时先补上，不拖到最后。
        boxset_covers_result = backfill_boxset_covers()
        for index, item in enumerate(items, 1):
            try:
                update_scan_status(current_item=item.get("Name") or item.get("Id"), last_progress_at=current_time_iso(), elapsed_seconds=round(time.monotonic() - scan_started, 1))
                result = process(item, rating_stats, review_items, health_items)
                result_counts[result] += 1
            except Exception as exc:
                result_counts["failed"] += 1
                failed_items.append({"id": item.get("Id"), "name": item.get("Name"), "error": str(exc)})
                LOG.exception("failed item %s", item.get("Id"))
            update_scan_status(
                processed=index,
                updated=result_counts["updated"],
                failed=result_counts["failed"],
                skipped=result_counts["skipped"],
                current_item=item.get("Name") or item.get("Id"),
                last_progress_at=current_time_iso(),
                elapsed_seconds=round(time.monotonic() - scan_started, 1),
            )
            if index % SCAN_PROGRESS_EVERY == 0 or index == len(items):
                LOG.info("scan progress: %d/%d items (%.1fs)", index, len(items), time.monotonic() - scan_started)
        cache_result = cache_gc(item_ids, expected_total=len(items))
        finished_at = current_time_iso()
        health_summary = write_media_health_report(health_items, finished_at)
        report = {
            "status": "ok" if not failed_items else "completed_with_errors",
            "dry_run": DRY_RUN,
            "started_at": scan_started_at,
            "finished_at": finished_at,
            "duration_seconds": round(time.monotonic() - scan_started, 1),
            "total": len(items),
            "results": dict(sorted(result_counts.items())),
            "failed_items": failed_items,
            "review_items": len(review_items),
            "rating_results": dict(sorted(rating_stats.items())),
            "rating_prefetch": {"mdblist": mdblist_prefetch},
            "douban_audit_counts": douban_audit["counts"],
            "cache": cache_result,
            "boxset_covers": boxset_covers_result,
            "media_health": health_summary,
            "library_fingerprint": _library_fingerprint(),
            "ratings_due_at": _next_rating_due(),
        }
        accuracy = rating_review.build_report(items, _safe_item_state, _douban_cache_result,
                                              DOUBAN_CACHE_DIR, finished_at, DOUBAN_CACHE_STALE_DAYS, overrides=_rating_override)
        atomic_write_text(RATING_ACCURACY_FILE, json.dumps(accuracy, ensure_ascii=False, indent=2))
        report["actual_rating_coverage"] = accuracy["source_coverage"]
        report["accuracy_counts"] = accuracy["counts"]
        _invalidate_matching_queue()
        write_scan_report(report)
        notify_scan_failure(report)
        try:
            atomic_write_text(REVIEW_REPORT_FILE, json.dumps({"generated_at": finished_at, "items": review_items}, ensure_ascii=False, indent=2))
        except OSError as exc:
            LOG.warning("review report write failed for %s: %s", REVIEW_REPORT_FILE, exc)
        update_scan_status(
            status=report["status"],
            finished_at=finished_at,
            updated=result_counts["updated"],
            failed=result_counts["failed"],
            skipped=result_counts["skipped"],
            last_error=failed_items[-1]["error"] if failed_items else None,
            current_item=None,
            last_progress_at=finished_at,
            elapsed_seconds=report["duration_seconds"],
        )
        LOG.info("scan finished: %d items in %.1fs; results=%s; rating results=%s; next scan in %ss", len(items), report["duration_seconds"], report["results"], report["rating_results"], POLL_SECONDS)
        return report
    except Exception as exc:
        update_scan_status(status="failed", finished_at=current_time_iso(), last_error=str(exc), current_item=None, last_progress_at=current_time_iso(), elapsed_seconds=round(time.monotonic() - scan_started, 1))
        LOG.exception("scan failed after %.1fs", time.monotonic() - scan_started)
        notify_scan_failure(None)
        return None
    finally:
        SCAN_RUN_LOCK.release()


def _scheduled_scan_loop() -> None:
    while True:
        run_scan_once()
        SCAN_WAKE_EVENT.wait(POLL_SECONDS)
        SCAN_WAKE_EVENT.clear()


# ---------------------------------------------------------------- backups

# The clean originals are the only data that cannot be rebuilt (Emby stores
# renders), so they get a scheduled archive on a separate volume.
BACKUP_ENABLED = str(env("BACKUP_ENABLED", "false")).lower() in {"1", "true", "yes", "on"}
BACKUP_DIR = Path(env("BACKUP_DIR", "")) if env("BACKUP_DIR", "").strip() else None
BACKUP_KEEP = max(1, int(env("BACKUP_KEEP", "2")))
BACKUP_HOUR = min(23, max(0, int(env("BACKUP_HOUR", "3"))))
BACKUP_INTERVAL_DAYS = max(1, int(env("BACKUP_INTERVAL_DAYS", "7")))
BACKUP_STATUS_FILE = CACHE_DIR / "reports" / "backup.json"
# Emby webhook: render a single new item immediately. Deliberately conservative
# so a burst of library events can never hammer Emby (which once froze it).
WEBHOOK_TOKEN = env("WEBHOOK_TOKEN", "").strip()
WEBHOOK_MAX_QUEUE = max(1, int(env("WEBHOOK_MAX_QUEUE", "10")))
WEBHOOK_MIN_INTERVAL = max(0.0, float(env("WEBHOOK_MIN_INTERVAL_SECONDS", "30")))
WEBHOOK_TYPES = {"movie", "series", "season"}
ALERT_WEBHOOK_URL = env("ALERT_WEBHOOK_URL", "").strip()
ALERT_MIN_FAILED = max(1, int(env("ALERT_MIN_FAILED", "1")))
BACKUP_LOCK = threading.Lock()


def _local_zone():
    """Configured zone with the same fixed-offset fallback used for logging."""
    name = os.environ.get("TZ") or LOG_TIMEZONE
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=8), name="UTC+08:00") if name in {"Asia/Shanghai", "Asia/Singapore", "Etc/GMT-8"} else timezone.utc


def _backup_archives() -> list[Path]:
    if BACKUP_DIR is None or not BACKUP_DIR.is_dir():
        return []
    try:
        return sorted(BACKUP_DIR.glob("*.tar.gz"),
                      key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        return []


def _backup_targets() -> list[tuple[Path, str]]:
    """Everything needed to rebuild the library look after a disaster.

    The clean originals cannot be regenerated (Emby only stores renders), and
    the manual rating corrections / admin settings are hours of human work, so
    both travel in the same archive.
    """
    targets: list[tuple[Path, str]] = []
    for name, arcname in (("original", "original"), ("state", "state")):
        path = CACHE_DIR / name
        if path.is_dir():
            targets.append((path, arcname))
    for name in ("rating-overrides.json", "issue-decisions.json"):
        path = CACHE_DIR / name
        if path.is_file():
            targets.append((path, name))
    for name in ("settings.json", "auth.json", "secrets.json"):
        path = ADMIN_DATA_DIR / name
        if path.is_file():
            targets.append((path, f"config/{name}"))
    return targets


def run_originals_backup() -> dict[str, Any]:
    """Archive originals + state + config, keeping the newest BACKUP_KEEP copies."""
    if BACKUP_DIR is None:
        return {"status": "disabled"}
    if not BACKUP_LOCK.acquire(blocking=False):
        return {"status": "busy"}
    started = time.time()
    try:
        targets = _backup_targets()
        if not targets:
            raise FileNotFoundError(str(CACHE_DIR / "original"))
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(_local_zone()).strftime("%Y%m%d")
        target = BACKUP_DIR / f"worker-backup-{stamp}.tar.gz"
        partial = target.with_name(target.name + ".part")
        files = 0
        with tarfile.open(partial, "w:gz", compresslevel=6) as archive:
            for path, arcname in targets:
                if path.is_file():
                    archive.add(path, arcname=arcname)
                    files += 1
                    continue
                for child in sorted(path.iterdir()):
                    if child.is_file():
                        archive.add(child, arcname=f"{arcname}/{child.name}")
                        files += 1
        os.replace(partial, target)
        removed: list[str] = []
        for old in _backup_archives()[max(1, int(_maint("backup_keep", BACKUP_KEEP))):]:
            try:
                old.unlink()
                removed.append(old.name)
            except OSError:
                continue
        result: dict[str, Any] = {
            "status": "ok", "file": target.name, "files": files,
            "bytes": target.stat().st_size,
            "seconds": round(time.time() - started, 1),
            "pruned": removed,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        }
    except Exception as exc:  # noqa: BLE001 - surfaced through status + log
        result = {"status": "failed", "error": str(exc),
                  "seconds": round(time.time() - started, 1),
                  "finished_at": datetime.now().isoformat(timespec="seconds")}
        LOG.exception("originals backup failed")
    finally:
        BACKUP_LOCK.release()
    try:
        atomic_write_text(BACKUP_STATUS_FILE, json.dumps(result, ensure_ascii=False, indent=2))
    except OSError:
        pass
    LOG.info("originals backup: %s", result)
    return result


def _backup_loop() -> None:
    while True:
        try:
            if BACKUP_ENABLED and BACKUP_DIR is not None:
                archives = _backup_archives()
                newest = archives[0].stat().st_mtime if archives else 0
                due = (time.time() - newest) >= BACKUP_INTERVAL_DAYS * 86400
                hour = datetime.now(_local_zone()).hour
                if due and hour == BACKUP_HOUR:
                    run_originals_backup()
        except Exception:
            LOG.exception("backup scheduler error")
        time.sleep(1800)


def main() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / "rendered").mkdir(parents=True, exist_ok=True)
    ADMIN_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_admin_auth()
    if DRY_RUN:
        LOG.warning(
            "DRY_RUN is enabled: posters are rendered into %s but never written to Emby; "
            "no item state is saved. Unset DRY_RUN to publish for real.",
            CACHE_DIR / "rendered",
        )
    _work_queue().start()
    threading.Thread(target=_admin_warm_loop, name="admin-view-warmer", daemon=True).start()
    start_http_server()
    threading.Thread(target=_cache_followup_loop, name='poster-cache-followup', daemon=True).start()
    threading.Thread(target=_scheduled_scan_loop, name="poster-scan-scheduler", daemon=True).start()
    threading.Thread(target=_backup_loop, name="originals-backup", daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()

