"""Poster compositing: rounded canvas, badge row and rating strip.

The module owns pixels only. Everything environmental (layout settings,
rating strategy, asset directory, render version) arrives through
RenderContext so rendering stays a pure function of its inputs.
"""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

import badges as badge_rules


def _env_int(name: str, default: str) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


# Client-safe canvas size; 0 disables the resize.
POSTER_OUTPUT_WIDTH = max(0, _env_int("POSTER_OUTPUT_WIDTH", "1000"))
POSTER_OUTPUT_HEIGHT = max(0, _env_int("POSTER_OUTPUT_HEIGHT", "1500"))


@dataclass(frozen=True)
class RenderContext:
    settings: dict[str, Any]
    strategy: str
    assets_dir: Path
    render_version: str
    image_marker: str
    ratings: Callable[[dict[str, Any]], dict[str, str]]


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def normalize_poster_canvas(image: Image.Image) -> Image.Image:
    """Keep posters in the common 2:3 canvas so clients do not crop overlays.

    Some source posters, including 毛骗, are much taller than the 2:3 poster
    slot used by the clients. Padding the short dimension is safer than using
    a crop: it preserves both the top quality chips and the footer ratings.
    """
    width, height = image.size
    target_ratio = 2 / 3
    current_ratio = width / height if height else target_ratio
    if abs(current_ratio - target_ratio) < 0.002:
        if POSTER_OUTPUT_WIDTH > 0 and POSTER_OUTPUT_HEIGHT > 0 and image.size != (POSTER_OUTPUT_WIDTH, POSTER_OUTPUT_HEIGHT):
            return image.resize((POSTER_OUTPUT_WIDTH, POSTER_OUTPUT_HEIGHT), Image.Resampling.LANCZOS)
        return image
    if current_ratio < target_ratio:
        target_width = round(height * target_ratio)
        target_height = height
    else:
        target_width = width
        target_height = round(width / target_ratio)
    canvas = Image.new("RGB", (target_width, target_height), (11, 12, 13))
    canvas.paste(image, ((target_width - width) // 2, (target_height - height) // 2))
    image = canvas
    if POSTER_OUTPUT_WIDTH > 0 and POSTER_OUTPUT_HEIGHT > 0 and image.size != (POSTER_OUTPUT_WIDTH, POSTER_OUTPUT_HEIGHT):
        image = image.resize((POSTER_OUTPUT_WIDTH, POSTER_OUTPUT_HEIGHT), Image.Resampling.LANCZOS)
    return image


def rating_sources(scores: dict[str, str], strategy: str | None = None) -> list[tuple[str, str, str, tuple[int, int, int]]]:
    """Return rating groups while preserving the current cover visual style.

    The display strategy controls whether missing sources are compacted out.
    It does not change the established colored marks, typography or footer
    overlay.  `fixed` is available as a compatibility switch for the exact
    three-slot layout shown in the current Emby library. `quality_only` (or
    `none`) intentionally omits the rating footer while keeping quality
    badges, which is useful when a client already paints its own ratings.
    """
    definitions = (
        ("豆瓣", "豆", (29, 178, 68)),
        ("IMDb", "IMDb", (230, 180, 30)),
        ("RT", "", (230, 54, 45)),
    )
    selected = str(strategy or "compact").lower()
    if selected in {"quality_only", "quality-only", "none", "clean"}:
        return []
    if selected in {"fixed", "compat", "current"}:
        return [
            (source, logo, str(scores.get(source, "--") or "--"), color)
            for source, logo, color in definitions
        ]
    return [
        (source, logo, str(scores[source]), color)
        for source, logo, color in definitions
        if scores.get(source) not in (None, "", "--")
    ]


def draw_badges(image: Image.Image, item: dict[str, Any], scale: float, ctx: "RenderContext") -> None:
    """Frosted white chips for text badges, bare white Dolby wordmarks.

    Resolution/source/HDR/audio badges get the same translucent grey-white
    chip with near-black text. Dolby Vision/Atmos use the official wordmark
    floating directly on the artwork (white on dark posters, near-black on
    bright ones) with a soft drop shadow, matching the reference layout.
    Chips are centred in equal horizontal slots across the width.
    """
    badges = badge_rules.quality_badges(item, ctx.settings)
    if not badges:
        return
    preview_badge_count = ctx.settings.get("preview_badge_count")
    if preview_badge_count is not None:
        badges = badges[: min(6, max(1, int(preview_badge_count)))]
    if not badges:
        return

    count = len(badges)
    layout = (ctx.settings.get("badge_layouts") or {}).get(str(count), {})
    style_scale = max(0.75, min(1.5, float(layout.get("scale", ctx.settings.get("badge_scale", 1.0)) or 1.0)))
    unit = image.width / 1000.0
    width, height = image.size
    offset_x = round(float(layout.get("offset_x", ctx.settings.get("badge_offset_x", 0)) or 0) * unit)
    offset_y = round(float(layout.get("offset_y", ctx.settings.get("badge_offset_y", 20)) or 0) * unit)
    on_top = str(ctx.settings.get("badge_position", "top-center")).startswith("top")

    # Load raw Dolby assets (black on transparent)
    raw_assets: dict[str, Image.Image | None] = {}
    for badge in badges:
        asset_name = badge.get("asset")
        if asset_name and asset_name not in raw_assets:
            asset_path = ctx.assets_dir / asset_name
            raw_assets[asset_name] = Image.open(asset_path).convert("RGBA") if asset_path.exists() else None

    measure_draw = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    s = unit * style_scale
    chip_h = max(14, round(64 * s))
    pad = max(4, round(16 * s))
    gap = max(3, round(8 * s))
    font_size = max(9, round(36 * s))
    radius = max(3, round(14 * s))
    text_color = (15, 15, 15, 255)
    blur_pad = max(3, round(5 * s))  # canvas margin so the soft shadow isn't clipped

    # Layout is fixed up-front so Dolby wordmarks can sample the poster
    # region they will land on and flip to black on bright artwork.
    band_h = chip_h
    center_y = round(offset_y) + band_h // 2 if on_top else height - offset_y - band_h // 2
    slot = width / count
    logo_h = chip_h

    def _region_luminance(cx: float) -> float:
        left = int(max(0, cx - slot / 2))
        right = int(min(width, cx + slot / 2))
        top = int(max(0, center_y - band_h / 2 - 4))
        bottom = int(min(height, center_y + band_h / 2 + 4))
        hist = image.convert("L").crop((left, top, right, bottom)).histogram()
        total = sum(hist)
        if not total:
            return 255.0
        return sum(i * c for i, c in enumerate(hist)) / total

    def _floating_wordmark(asset: Image.Image, color: tuple[int, int, int, int]) -> Image.Image:
        logo_w = max(8, round(asset.width * logo_h / asset.height))
        resized = asset.resize((logo_w, logo_h), Image.Resampling.LANCZOS)
        mark = Image.new("RGBA", resized.size, color)
        mark.putalpha(resized.getchannel("A"))
        mark = mark.filter(ImageFilter.UnsharpMask(radius=1.0, percent=70, threshold=2))
        pad_px = max(4, round(logo_h * 0.22))
        layer = Image.new("RGBA", (logo_w + pad_px * 2, logo_h + pad_px * 2), (0, 0, 0, 0))
        # Shadow contrasts with the mark: dark halo behind the white wordmark,
        # soft white glow behind the near-black one on bright posters.
        glow = (255, 255, 255, 255) if color[0] < 128 else (0, 0, 0, 255)
        shadow = Image.new("RGBA", mark.size, glow)
        shadow.putalpha(mark.getchannel("A").point(lambda a: round(a * 0.6)))
        shadow = shadow.filter(ImageFilter.GaussianBlur(max(1, round(pad_px / 2))))
        layer.alpha_composite(shadow, (pad_px, pad_px + 1))
        layer.alpha_composite(mark, (pad_px, pad_px))
        return layer

    rendered: list[tuple[dict[str, Any], int, int, Image.Image | None]] = []
    for badge in badges:
        label = str(badge.get("label") or "")
        key = str(badge.get("key") or "")
        asset = raw_assets.get(str(badge.get("asset") or "")) if badge.get("asset") else None
        if asset is not None and key in {"DV", "ATMOS"}:
            # Reference style: bare official wordmark floating on the artwork.
            # Prefer white wherever it stays readable; only flip to near-black
            # on genuinely bright artwork (mean luminance > 215), where white
            # would be invisible. A soft contrasting shadow keeps thin strokes
            # readable at grid sizes.
            cx = slot * len(rendered) + slot / 2 + offset_x
            color = (255, 255, 255, 255) if _region_luminance(cx) < 215 else (12, 12, 12, 255)
            visual = _floating_wordmark(asset, color)
            rendered.append((badge, visual.width, visual.height, visual))
        else:
            logo = None
            font = load_font(font_size, bold=True)
            text_w = measure_draw.textbbox((0, 0), label, font=font)[2]
            chip_w = max(round(60 * s), text_w + 2 * pad)
            chip = Image.new("RGBA", (chip_w + 2 * blur_pad, chip_h + 2 * blur_pad), (0, 0, 0, 0))
            # soft blurred drop shadow (replaces the old hard offset block)
            shadow = Image.new("RGBA", chip.size, (0, 0, 0, 0))
            ImageDraw.Draw(shadow).rounded_rectangle(
                (blur_pad, blur_pad + max(1, round(2 * s)),
                 blur_pad + chip_w - 1, blur_pad + chip_h - 1 + max(1, round(2 * s))),
                radius=radius, fill=(0, 0, 0, 105),
            )
            shadow = shadow.filter(ImageFilter.GaussianBlur(blur_pad / 2))
            chip.alpha_composite(shadow)
            # frosted body: dense multi-stop gradient inside a rounded mask
            body = Image.new("RGBA", chip.size, (0, 0, 0, 0))
            bd = ImageDraw.Draw(body)
            for row in range(chip_h):
                t = row / max(1, chip_h - 1)
                a = round(248 - 58 * (t ** 0.85))  # bright top, gentle falloff
                bd.line((blur_pad, blur_pad + row, blur_pad + chip_w, blur_pad + row),
                        fill=(255, 255, 255, a))
            mask = Image.new("L", chip.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (blur_pad, blur_pad, blur_pad + chip_w - 1, blur_pad + chip_h - 1),
                radius=radius, fill=255)
            body.putalpha(mask)
            chip.alpha_composite(body)
            cd = ImageDraw.Draw(chip)
            # hairline border + inner top highlight for a polished edge
            cd.rounded_rectangle(
                (blur_pad, blur_pad, blur_pad + chip_w - 1, blur_pad + chip_h - 1),
                radius=radius, outline=(0, 0, 0, 42), width=1)
            cd.rounded_rectangle(
                (blur_pad + 1, blur_pad + 1, blur_pad + chip_w - 2, blur_pad + chip_h - 2),
                radius=max(1, radius - 1), outline=(255, 255, 255, 90), width=1)
            cd.text(
                (blur_pad + chip_w / 2, blur_pad + chip_h / 2 + max(1, round(1 * s))),
                label, font=font, fill=text_color, anchor="mm")
            rendered.append((badge, chip.width, chip.height, chip))

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    for index, (badge, w, h, visual) in enumerate(rendered):
        cx = slot * index + slot / 2 + offset_x
        cx = int(round(max(w // 2 + 4, min(width - w // 2 - 4, cx))))
        px = cx - w // 2
        py = center_y - h // 2
        if visual is not None:
            overlay.alpha_composite(visual, (px, py))
    image.alpha_composite(overlay)

def draw_scores(image: Image.Image, scores: dict[str, str], scale: float, ctx: "RenderContext") -> None:
    """Draw the rating strip using the persisted admin layout settings."""
    sources = rating_sources(scores, ctx.strategy)
    preview_rating_count = ctx.settings.get("preview_rating_count")
    if preview_rating_count is not None and ctx.strategy not in {"quality_only", "quality-only", "none", "clean"}:
        # Let the editor inspect each 1/2/3-column layout against the same sample.
        sources = rating_sources(scores, "fixed")[:min(3, max(1, int(preview_rating_count)))]
    if not sources:
        return
    rating_count = min(3, max(1, len(sources)))
    rating_layout = (ctx.settings.get("rating_layouts") or {}).get(str(rating_count), {})
    style_scale = max(0.75, min(1.5, float(rating_layout.get("scale", ctx.settings.get("rating_scale", 1.0)) or 1.0)))
    scale *= style_scale
    width, height = image.size
    band_height = round(60 * scale)
    rating_position = str(ctx.settings.get("rating_position", "bottom"))
    rating_offset_x = round(float(rating_layout.get("offset_x", ctx.settings.get("rating_offset_x", 0)) or 0) * scale)
    rating_offset_y = round(float(rating_layout.get("offset_y", ctx.settings.get("rating_offset_y", 0)) or 0) * scale)
    bar_top = rating_offset_y if rating_position == "top" else height - band_height - rating_offset_y
    score_overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(score_overlay).rectangle((0, bar_top, width, height), fill=(18, 22, 25, 224))
    image.alpha_composite(score_overlay)
    draw = ImageDraw.Draw(image)
    score_font = load_font(round(38 * scale), bold=True)
    logo_font = load_font(round(12 * scale), bold=True)
    segment_width = width / len(sources)
    icon_size = round(34 * scale)
    value_gap = round(8 * scale)
    usable_width = width
    for index, (source_name, logo, value, color) in enumerate(sources):
        segment_left = index * (usable_width / len(sources))
        segment_right = (index + 1) * (usable_width / len(sources))
        value_bounds = draw.textbbox((0, 0), value, font=score_font)
        available = segment_right - segment_left - round(16 * scale) - icon_size - value_gap
        value_font = score_font
        if value_bounds[2] - value_bounds[0] > available:
            value_font = load_font(round(31 * scale), bold=True)
            value_bounds = draw.textbbox((0, 0), value, font=value_font)
        group_width = icon_size + value_gap + (value_bounds[2] - value_bounds[0])
        # Default layout: every rating group is centred inside its own
        # equal-width slot, matching the badge row's even distribution.
        group_left = round(segment_left + max(0, (segment_right - segment_left - group_width) // 2))
        group_left += rating_offset_x
        icon_x = group_left
        icon_y = bar_top + (band_height - icon_size) // 2
        if source_name == "RT":
            draw.ellipse((icon_x, icon_y + round(3 * scale), icon_x + icon_size, icon_y + icon_size), fill=color)
            draw.polygon(((icon_x + icon_size // 2, icon_y + round(5 * scale)), (icon_x + round(7 * scale), icon_y), (icon_x + round(11 * scale), icon_y + round(9 * scale))), fill=(37, 143, 75))
        else:
            draw.rounded_rectangle((icon_x, icon_y, icon_x + icon_size, icon_y + icon_size), radius=round(2 * scale), fill=color)
            logo_rendered = False
            if source_name == "豆瓣":
                douban_asset = ctx.assets_dir / "douban.png"
                if douban_asset.exists():
                    douban = Image.open(douban_asset).convert("RGBA").resize((icon_size, icon_size), Image.Resampling.LANCZOS)
                    image.paste(douban, (icon_x, icon_y), douban)
                    logo_rendered = True
                logo_font = load_font(round(16 * scale), bold=True)
            elif source_name == "IMDb":
                logo_font = load_font(round(10 * scale), bold=True)
            if not logo_rendered:
                logo_bounds = draw.textbbox((0, 0), logo, font=logo_font)
                logo_x = icon_x + (icon_size - (logo_bounds[2] - logo_bounds[0])) // 2
                logo_y = icon_y + (icon_size - (logo_bounds[3] - logo_bounds[1])) // 2 - logo_bounds[1]
                draw.text((logo_x, logo_y), logo, font=logo_font, fill="white" if source_name == "豆瓣" else (35, 35, 35))
        value_y = bar_top + (band_height - (value_bounds[3] - value_bounds[1])) // 2 - value_bounds[1]
        value_x = icon_x + icon_size + value_gap
        value_fill = (247, 247, 246) if value != "--" else (144, 144, 144)
        draw.text((value_x + round(2 * scale), value_y + round(2 * scale)), value, font=value_font, fill=(0, 0, 0))
        draw.text((value_x, value_y), value, font=value_font, fill=value_fill)


def render(item: dict[str, Any], source: Path, output: Path, scores: dict[str, str] | None,
           ctx: "RenderContext") -> None:
    """Render a client-safe poster; review labels are never part of this JPEG."""
    image = normalize_poster_canvas(Image.open(source).convert("RGB")).convert("RGBA")
    width, _ = image.size
    scale = width / 400
    rounded = Image.new("RGBA", image.size, (11, 12, 13, 255))
    rounded_mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(rounded_mask).rounded_rectangle((0, 0, width - 1, image.height - 1), radius=round(18 * scale), fill=255)
    rounded.paste(image, mask=rounded_mask)
    image = rounded
    draw_badges(image, item, scale, ctx)
    scores = scores if scores is not None else ctx.ratings(item)
    draw_scores(image, scores, scale, ctx)

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        image.convert("RGB").save(temporary, "JPEG", quality=88, optimize=True, progressive=True, comment=f"{ctx.image_marker}{ctx.render_version}".encode("ascii"))
        os.replace(temporary, output)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


