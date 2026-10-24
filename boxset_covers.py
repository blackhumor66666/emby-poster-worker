"""合集（BoxSet）封面补齐。

Emby 在库扫描阶段不会给 BoxSet 下载封面：由插件/自动合集写进
`config/metadata/collections/*/collection.nfo` 的条目只带 tmdbid，
图片要等一次元数据/图片刷新才会去 TMDB 拉取。结果就是合集库里
一部分合集有封面、一部分一直是空白。

本模块负责：
1. 列出全库 BoxSet；
2. 找出没有 Primary 图片的条目；
3. 对它们触发一次图片刷新（ImageRefreshMode=FullRefresh，不动标题等元数据）；
4. 记录尝试时间，避免对 TMDB 确实没有封面的合集每小时重试。

既可以由 app.py 的扫描流程调用，也可以单独跑：
    EMBY_URL=... EMBY_API_KEY=... python boxset_covers.py --apply
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

BOXSET_FIELDS = "ProviderIds,ImageTags,SortName,Overview,ParentId"
DEFAULT_RETRY_DAYS = 7
# 刷新是异步的，等 Emby 把图下完再复查。
SETTLE_SECONDS = 10
SETTLE_POLL_SECONDS = 3
SETTLE_MAX_SECONDS = 120


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"items": {}}
    if not isinstance(data, dict):
        return {"items": {}}
    if not isinstance(data.get("items"), dict):
        data["items"] = {}
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def list_boxsets(emby: Callable[..., Any], timeout: float | None = None) -> list[dict[str, Any]]:
    """读取全库 BoxSet（带 ImageTags，用来判断有没有封面）。"""
    params: dict[str, str] = {
        "Recursive": "true",
        "IncludeItemTypes": "BoxSet",
        "Fields": BOXSET_FIELDS,
        "Limit": "1000",
    }
    kwargs: dict[str, Any] = {"params": params}
    if timeout is not None:
        kwargs["timeout"] = timeout
    payload = emby("GET", "/Items", **kwargs).json()
    return payload.get("Items") or []


def has_primary(item: dict[str, Any]) -> bool:
    tags = item.get("ImageTags") or {}
    return bool(tags.get("Primary")) if isinstance(tags, dict) else False


def refresh_images(emby: Callable[..., Any], item_id: str) -> None:
    """只刷图片，不动标题/简介等用户可能改过的元数据。"""
    emby(
        "POST",
        f"/Items/{item_id}/Refresh",
        params={
            "MetadataRefreshMode": "Default",
            "ImageRefreshMode": "FullRefresh",
            "ReplaceAllImages": "false",
            "ReplaceAllMetadata": "false",
        },
    )


def first_child_with_image(emby: Callable[..., Any], item_id: str) -> dict[str, Any] | None:
    """找合集里第一个自带封面的成员，作为 TMDB 没有合集图时的兜底。"""
    payload = emby(
        "GET",
        "/Items",
        params={"ParentId": item_id, "Fields": "ImageTags,ProductionYear", "Limit": "50"},
    ).json()
    for child in payload.get("Items") or []:
        if has_primary(child):
            return child
    return None


def apply_member_poster(emby: Callable[..., Any], item_id: str) -> bool:
    """TMDB 合集本身没有海报时，把成员影片的封面复用为合集封面。

    Emby 的图片上传接口要求 base64 文本（直接发二进制会返回 204 但不生效）。
    """
    child = first_child_with_image(emby, item_id)
    if not child:
        return False
    data = emby("GET", f"/Items/{child['Id']}/Images/Primary", params={"quality": "95"}).content
    if not data:
        return False
    emby(
        "POST",
        f"/Items/{item_id}/Images/Primary",
        params={"index": "0"},
        data=base64.b64encode(data),
        headers={"Content-Type": "text/plain"},
    )
    return True


def _settle(emby: Callable[..., Any], wanted: int, log: Callable[[str], None] | None) -> list[dict[str, Any]]:
    """等 Emby 把图片写完；返回最后一次读取的合集列表。"""
    deadline = time.monotonic() + SETTLE_MAX_SECONDS
    time.sleep(SETTLE_SECONDS)
    items = list_boxsets(emby)
    while time.monotonic() < deadline:
        missing = sum(1 for item in items if not has_primary(item))
        if wanted <= 0 or missing <= 0:
            break
        previous = missing
        time.sleep(SETTLE_POLL_SECONDS)
        items = list_boxsets(emby)
        if sum(1 for item in items if not has_primary(item)) >= previous:
            # 不再变好就提前结束，别白等。
            break
    if log:
        log(f"boxset covers: settle done, {sum(1 for i in items if not has_primary(i))} still missing")
    return items


def backfill(
    emby: Callable[..., Any],
    state_path: Path,
    retry_days: int = DEFAULT_RETRY_DAYS,
    force: bool = False,
    dry_run: bool = False,
    member_fallback: bool = True,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """补齐缺封面的合集。返回可直接写进扫描报告的摘要。"""
    items = list_boxsets(emby)
    state = load_state(state_path)
    records: dict[str, Any] = state["items"]
    now = time.time()
    retry_seconds = max(1, retry_days) * 86400

    missing = [item for item in items if not has_primary(item)]
    targets: list[dict[str, Any]] = []
    skipped_recent = 0
    for item in missing:
        item_id = str(item.get("Id") or "")
        if not item_id:
            continue
        record = records.get(item_id) or {}
        last_attempt = float(record.get("last_attempt_ts") or 0)
        if not force and record.get("result") == "missing" and now - last_attempt < retry_seconds:
            skipped_recent += 1
            continue
        targets.append(item)

    summary: dict[str, Any] = {
        "total": len(items),
        "missing_before": len(missing),
        "attempted": 0,
        "fixed": 0,
        "fallback": 0,
        "still_missing": 0,
        "skipped_recent": skipped_recent,
        "dry_run": dry_run,
        "items": [],
    }
    if log:
        log(f"boxset covers: {len(items)} boxsets, {len(missing)} without cover, {len(targets)} to refresh")

    refreshed_ids: set[str] = set()
    for item in targets:
        item_id = str(item.get("Id") or "")
        name = item.get("Name") or item_id
        if dry_run:
            summary["items"].append({"id": item_id, "name": name, "status": "would_refresh"})
            continue
        try:
            refresh_images(emby, item_id)
            summary["attempted"] += 1
            refreshed_ids.add(item_id)
            records[item_id] = {
                "name": name,
                "last_attempt_ts": now,
                "last_attempt": _now_iso(),
                "result": "pending",
            }
            if log:
                log(f"boxset covers: refresh requested for {item_id} {name}")
        except Exception as exc:  # 单条失败不影响整批
            records[item_id] = {
                "name": name,
                "last_attempt_ts": now,
                "last_attempt": _now_iso(),
                "result": "error",
                "error": str(exc),
            }
            summary["items"].append({"id": item_id, "name": name, "status": "error", "error": str(exc)})
            if log:
                log(f"boxset covers: refresh failed for {item_id} {name}: {exc}")
        time.sleep(0.4)

    if dry_run or not summary["attempted"]:
        if not dry_run:
            save_state(state_path, state)
        summary["still_missing"] = len(missing)
        return summary

    after = _settle(emby, len(targets), log)
    by_id = {str(item.get("Id")): item for item in after}
    for item in targets:
        item_id = str(item.get("Id") or "")
        if item_id not in refreshed_ids:
            continue
        name = item.get("Name") or item_id
        current = by_id.get(item_id) or {}
        fixed = has_primary(current)
        status = "fixed" if fixed else "missing"
        if not fixed and member_fallback:
            # TMDB 合集本身没有海报（国产网络电影很常见），退一步用成员影片的封面。
            try:
                if apply_member_poster(emby, item_id):
                    status = "fallback"
                    summary["fallback"] += 1
                    if log:
                        log(f"boxset covers: {item_id} {name} has no TMDB poster, reused a member poster")
            except Exception as exc:
                if log:
                    log(f"boxset covers: member poster fallback failed for {item_id} {name}: {exc}")
        if status == "fixed":
            summary["fixed"] += 1
        elif status == "missing":
            summary["still_missing"] += 1
        records[item_id] = {
            "name": name,
            "last_attempt_ts": now,
            "last_attempt": _now_iso(),
            "result": status,
        }
        summary["items"].append({"id": item_id, "name": name, "status": status})

    save_state(state_path, state)
    summary["missing_after"] = max(0, len(missing) - summary["fixed"] - summary["fallback"])
    if log:
        log(
            "boxset covers: %d fixed, %d reused a member poster, %d still missing (%d boxsets total)"
            % (summary["fixed"], summary["fallback"], summary["missing_after"], len(after))
        )
    return summary


def main(argv: list[str]) -> int:
    import requests

    base = (os.environ.get("EMBY_URL") or "").rstrip("/")
    key = os.environ.get("EMBY_API_KEY") or ""
    if not base or not key:
        print("EMBY_URL / EMBY_API_KEY required", file=sys.stderr)
        return 2
    session = requests.Session()
    session.headers.update({"X-Emby-Token": key})

    def emby(method: str, path: str, **kwargs: Any) -> Any:
        timeout = kwargs.pop("timeout", 60)
        response = session.request(method, f"{base}{path}", timeout=timeout, **kwargs)
        response.raise_for_status()
        return response

    state_path = Path(os.environ.get("BOXSET_STATE_FILE") or Path(__file__).parent / "boxset-covers-state.json")
    result = backfill(
        emby,
        state_path,
        retry_days=int(os.environ.get("BOXSET_RETRY_DAYS", str(DEFAULT_RETRY_DAYS))),
        force="--force" in argv,
        dry_run="--apply" not in argv,
        member_fallback="--no-fallback" not in argv,
        log=lambda message: print(message, flush=True),
    )
    print(json.dumps({k: v for k, v in result.items() if k != "items"}, ensure_ascii=False))
    for entry in result["items"]:
        print(f"  {entry['status']:14s} {entry['id']} {entry['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
