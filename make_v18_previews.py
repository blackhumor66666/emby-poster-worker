from __future__ import annotations

import json
import os
from pathlib import Path

from PIL import Image, ImageDraw

import app

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
ORIGINAL = CACHE / "original"
OUT = CACHE / "previews" / "v18-layouts"
SHOW_ANNOTATIONS = os.environ.get("V18_PREVIEW_ANNOTATIONS", "false").strip().lower() in {"1", "true", "yes"}

# These are real posters already cached from the connected Emby library.  The
# explicit scores mirror the current library values used in the V1.8 review
# sheet, so the preview isolates layout rather than depending on a live lookup.
SAMPLES = [
    {"id": "64855", "name": "骗骗喜欢你", "kind": "single", "scores": {"豆瓣": "6.4"}, "quality": ["4K"]},
    {"id": "114521", "name": "10间敢死队", "kind": "single", "scores": {"豆瓣": "6.6"}, "quality": ["4K"]},
    {"id": "121722", "name": "007：无暇赴死", "kind": "double", "scores": {"豆瓣": "6.6", "IMDb": "7.3"}, "quality": ["4K", "HDR10"]},
    {"id": "133313", "name": "8英里", "kind": "double", "scores": {"豆瓣": "8.3", "RT": "76%"}, "quality": ["4K", "HDR10"]},
    {"id": "121722", "name": "007：无暇赴死", "kind": "triple", "scores": {"豆瓣": "6.6", "IMDb": "7.3", "RT": "83%"}, "quality": ["4K", "HDR10"]},
    {"id": "133313", "name": "8英里", "kind": "triple", "scores": {"豆瓣": "8.3", "IMDb": "7.6", "RT": "76%"}, "quality": ["4K", "HDR10"]},
]


def sample_item(sample: dict[str, object]) -> dict[str, object]:
    labels = list(sample["quality"])
    streams = [{"Type": "Video", "Width": 3840, "Height": 2160, "DisplayTitle": "4K HDR10"}]
    return {
        "Id": sample["id"],
        "Name": sample["name"],
        "MediaStreams": streams,
        "MediaSources": [{"Name": "sample-4K-HDR10.mkv", "Path": "sample-4K-HDR10.mkv"}],
        "_preview_quality": labels,
    }


def contact_sheet(kind: str, entries: list[dict[str, object]]) -> Path:
    cell_w, cell_h = 360, 540
    # The title/id/rating text is review-sheet metadata only. Keep it hidden by
    # default so the sheet mirrors the actual JPEG uploaded to Emby.
    label_h = 74 if SHOW_ANNOTATIONS else 0
    cols = 2
    rows = (len(entries) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell_w, rows * (cell_h + label_h)), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    font = app.load_font(24, bold=True)
    subfont = app.load_font(18)
    for index, entry in enumerate(entries):
        image = Image.open(entry["file"]).convert("RGB")
        image.thumbnail((cell_w - 20, cell_h - 20), Image.Resampling.LANCZOS)
        left = (index % cols) * cell_w + (cell_w - image.width) // 2
        top = (index // cols) * (cell_h + label_h) + (cell_h - image.height) // 2
        sheet.paste(image, (left, top))
        if SHOW_ANNOTATIONS:
            x = (index % cols) * cell_w + 10
            y = (index // cols) * (cell_h + label_h) + cell_h + 8
            draw.text((x, y), str(entry["name"]), fill="white", font=font)
            draw.text((x, y + 30), f"{entry['id']}  {entry['ratings']}", fill=(190, 190, 190), font=subfont)
    path = OUT / f"contact-sheet-{kind}.jpg"
    sheet.save(path, quality=94)
    return path


def main() -> None:
    entries_by_kind: dict[str, list[dict[str, object]]] = {"single": [], "double": [], "triple": []}
    for sample in SAMPLES:
        source = ORIGINAL / f"{sample['id']}.jpg"
        if not source.exists():
            raise SystemExit(f"missing cached poster: {source}")
        item = sample_item(sample)
        kind = str(sample["kind"])
        output_dir = OUT / kind
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f"{sample['id']}-{sample['name']}.jpg"
        app.render(item, source, output, dict(sample["scores"]))
        entries_by_kind[kind].append({
            "id": sample["id"],
            "name": sample["name"],
            "ratings": sample["scores"],
            "file": str(output),
        })
    manifest = {"version": app.APP_VERSION, "entries": entries_by_kind}
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    for kind, entries in entries_by_kind.items():
        print(f"{kind}: {contact_sheet(kind, entries)}")


if __name__ == "__main__":
    main()
