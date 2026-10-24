"""Create local-only poster previews without changing Emby images."""

import json
import os
import sys
from pathlib import Path

import app


def main() -> None:
    preview_root = Path(os.environ.get("PREVIEW_DIR", str(app.CACHE_DIR / "previews")))
    movie_dir = preview_root / "movies"
    series_dir = preview_root / "series"
    movie_dir.mkdir(parents=True, exist_ok=True)
    series_dir.mkdir(parents=True, exist_ok=True)

    fields = "ProviderIds,MediaSources,MediaStreams,Path,OriginalTitle,Genres,CommunityRating,CriticRating,DateLastMediaAdded"
    items = app.emby("GET", "/Items", params={
        "Recursive": "true",
        "IncludeItemTypes": "Movie,Series",
        "Fields": fields,
        "SortBy": "SortName",
        "SortOrder": "Ascending",
        "Limit": 300,
    }).json().get("Items", [])
    movies = [item for item in items if item.get("Type") == "Movie"][:5]
    series = [item for item in items if item.get("Type") == "Series"][:3]
    manifest = []

    for category, selected, output_dir in (("movie", movies, movie_dir), ("series", series, series_dir)):
        for index, item in enumerate(selected, 1):
            if category == "series":
                item = app.enrich_series(item)
            source = app.get_original(item["Id"])
            output = output_dir / f"{index:02d}-{item['Id']}.jpg"
            score_data = app.ratings(item)
            app.render(item, source, output, score_data)
            manifest.append({
                "category": category,
                "index": index,
                "id": item["Id"],
                "name": item.get("Name"),
                "quality": app.quality_labels(item),
                "ratings": score_data,
                "file": str(output),
            })

    (preview_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    for entry in manifest:
        print(f"{entry['category']} {entry['index']}: {entry['name']} -> {entry['file']} | {entry['quality']} | {entry['ratings']}")


if __name__ == "__main__":
    main()
