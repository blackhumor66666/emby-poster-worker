"""Quality badge rules for one Emby item.

Pure functions over Emby metadata: no HTTP, no settings globals. The caller
passes the current render settings so badge output always matches the admin UI.
"""
import re
from typing import Any


def _badge_path_text(item: dict[str, Any]) -> str:
    """Collect the fields that carry release/source tokens.

    MediaStreams are authoritative for resolution/HDR/audio. The path is only
    used as a fallback (ISO/STRM often expose no decoded streams) and for the
    release source tag, which is not part of Emby's media streams.
    """
    parts: list[str] = []
    for key in ("Name", "FileName", "Path"):
        value = item.get(key)
        if value:
            parts.append(str(value))
    for source in item.get("MediaSources") or []:
        for key in ("Name", "Path", "Container"):
            value = source.get(key)
            if value:
                parts.append(str(value))
    return " ".join(parts)


def _badge_streams(item: dict[str, Any]) -> list[dict[str, Any]]:
    streams: list[dict[str, Any]] = []
    for source in item.get("MediaSources") or []:
        streams.extend(source.get("MediaStreams") or [])
    if not streams:
        streams = item.get("MediaStreams") or []
    return streams


def _badge_video(item: dict[str, Any]) -> dict[str, Any]:
    videos = [stream for stream in _badge_streams(item) if str(stream.get("Type") or "").lower() == "video"]
    return max(videos, key=lambda stream: int(stream.get("Width") or 0) * int(stream.get("Height") or 0), default={})


def _badge_audio(item: dict[str, Any]) -> dict[str, Any]:
    audios = [stream for stream in _badge_streams(item) if str(stream.get("Type") or "").lower() == "audio"]
    for stream in audios:
        text = " ".join(str(stream.get(key) or "") for key in ("Codec", "Profile", "DisplayTitle", "Title", "ChannelLayout")).lower()
        if "atmos" in text or "dts:x" in text or "dts-x" in text:
            return stream
    return audios[0] if audios else {}


def _resolution_badge(video: dict[str, Any], path_text: str) -> str | None:
    width = int(video.get("Width") or 0)
    height = int(video.get("Height") or 0)
    if width or height:
        if width >= 3000 or height >= 2000:
            return "4K"
        if width >= 1800 or height >= 1000:
            return "1080P"
        if width >= 1200 or height >= 700:
            return "720P"
        if width >= 700 or height >= 400:
            return "576P" if height >= 560 else "480P"
        return "SD"
    search_text = " ".join(
        str(video.get(key) or "") for key in ("DisplayTitle", "VideoRange", "VideoRangeType", "ColorTransfer")
    ) + " " + path_text
    match = re.search(r"\b(2160p|1080p|720p|576p|480p|4k)\b", search_text, re.I)
    if match:
        return {"2160p": "4K", "1080p": "1080P", "720p": "720P", "576p": "576P", "480p": "480P", "4k": "4K"}[match.group(1).lower()]
    if re.search(r"(?:uhd|ultra[ ._-]?hd)", search_text, re.I):
        return "4K"
    if re.search(r"\bfhd\b", search_text, re.I):
        return "1080P"
    return None


def _source_badge(path_text: str) -> str | None:
    text = path_text.casefold()
    patterns = (
        ("REMUX", r"\bremux\b"),
        ("BLURAY", r"\b(?:uhd[ ._-]?)?blu[ ._-]?ray\b|\bbdmv\b"),
        ("BDRIP", r"\bbdrip\b|\bbrrip\b"),
        ("WEB-DL", r"\bweb[ ._-]?dl\b"),
        ("WEBRIP", r"\bweb[ ._-]?rip\b"),
        ("HDTV", r"\bhdtv\b"),
        ("DVDRIP", r"\bdvd[ ._-]?rip\b"),
    )
    for label, pattern in patterns:
        if re.search(pattern, text):
            return label
    if re.search(r"\.iso(?:$|[ ._-])", text):
        return "ISO"
    return None


def _hdr_sub_label(text: str, color_transfer: str = "", video_range: str = "") -> str:
    """Return the HDR base layer shown under a Dolby Vision logo."""
    if "hdr10+" in text or "hdr10plus" in text:
        return "HDR10+"
    if "hdr10" in text or color_transfer == "smpte2084" or video_range == "hdr":
        return "HDR10"
    if "hlg" in text or color_transfer == "arib-std-b67":
        return "HLG"
    if re.search(r"\bhdr\b", text):
        return "HDR10"
    return ""


def _hdr_badge(video: dict[str, Any], path_text: str) -> dict[str, Any] | None:
    video_range = str(video.get("VideoRange") or "").casefold()
    range_type = str(video.get("VideoRangeType") or "").casefold()
    color_transfer = str(video.get("ColorTransfer") or "").casefold()
    display = " ".join(
        str(video.get(key) or "") for key in ("DisplayTitle", "VideoRange", "VideoRangeType", "ColorTransfer", "Codec", "Profile")
    ).casefold()
    text = f"{range_type} {video_range} {color_transfer} {display}"

    if "dovi" in range_type or "dolbyvision" in range_type or "dolbyvision" in text or "dolby vision" in text or re.search(r"(?:^|[^a-z])dv(?:[^a-z]|$)", text):
        return {"key": "DV", "label": "DOLBY VISION", "style": "dolby", "asset": "dolby-vision-lockup.png", "sub": _hdr_sub_label(text, color_transfer, video_range)}
    if "hdr10plus" in range_type or "hdr10+" in text or "hdr10 plus" in text:
        return {"key": "HDR10+", "label": "HDR10+", "style": "light"}
    if "hlg" in text or color_transfer == "arib-std-b67":
        return {"key": "HLG", "label": "HLG", "style": "light"}
    if "hdr10" in text or color_transfer == "smpte2084":
        return {"key": "HDR10", "label": "HDR10", "style": "light"}
    if video_range == "hdr":
        return {"key": "HDR", "label": "HDR", "style": "light"}

    # ISO/STRM items often have no decoded streams. Only then use explicit
    # release-name tokens; never guess from an unrelated word in the title.
    if not (video_range or range_type or color_transfer):
        release = path_text.casefold()
        if re.search(r"dolby[ ._-]?vision|dovi|\bdv\b", release):
            return {"key": "DV", "label": "DOLBY VISION", "style": "dolby", "asset": "dolby-vision-lockup.png", "sub": _hdr_sub_label(release)}
        if re.search(r"hdr10[ +._-]?\+|hdr\s*10\s*plus", release):
            return {"key": "HDR10+", "label": "HDR10+", "style": "light"}
        if re.search(r"\bhlg\b", release):
            return {"key": "HLG", "label": "HLG", "style": "light"}
        if re.search(r"\bhdr10\b", release):
            return {"key": "HDR10", "label": "HDR10", "style": "light"}
        if re.search(r"\bhdr\b", release):
            return {"key": "HDR", "label": "HDR", "style": "light"}
    if video_range == "sdr" or (video and (video_range or range_type or color_transfer)):
        return {"key": "SDR", "label": "SDR", "style": "light"}
    return None


def _audio_badge(audio: dict[str, Any], path_text: str) -> dict[str, Any] | None:
    fields = " ".join(str(audio.get(key) or "") for key in ("Codec", "Profile", "DisplayTitle", "Title", "ChannelLayout")).casefold()
    codec = str(audio.get("Codec") or "").casefold()
    release = path_text.casefold()
    # Trust the decoded stream first. When Emby does not expose the immersive
    # flag (common for EAC3/TrueHD Atmos), accept the release name only if the
    # codec can actually carry it; never put Atmos on an AAC track.
    atmos = "atmos" in fields or ("atmos" in release and (not audio or codec in {"eac3", "truehd", "ac3", "dts"}))
    if atmos:
        return {"key": "ATMOS", "label": "ATMOS", "style": "dolby", "asset": "dolby-atmos-lockup.png"}
    dtsx = re.search(r"dts[ .:_-]?x\b", fields) or (
        re.search(r"dts[ .:_-]?x\b", release) and (not audio or codec.startswith("dts"))
    )
    if dtsx:
        return {"key": "DTSX", "label": "DTS:X", "style": "light"}
    if audio:
        codec = str(audio.get("Codec") or "").casefold()
        profile = str(audio.get("Profile") or "").casefold()
        if codec == "dts" and "ma" in profile:
            return {"key": "AUDIO", "label": "DTS-HD", "style": "light"}
        labels = {"aac": "AAC", "ac3": "AC3", "eac3": "EAC3", "dts": "DTS", "truehd": "TRUEHD", "flac": "FLAC", "opus": "OPUS", "mp3": "MP3"}
        if codec in labels:
            return {"key": "AUDIO", "label": labels[codec], "style": "light"}
    return None


def quality_badges(item: dict[str, Any], render_settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return only badges backed by the item's actual media metadata.

    Priority order matches the reference layout: resolution, source, HDR,
    audio. Missing metadata produces no badge instead of a guessed one.
    With ``badge_minimal`` (default) the row keeps only high-signal badges:
    SDR (the default range) and lossy audio codecs are omitted.
    """
    settings = render_settings or {}
    video = _badge_video(item)
    audio = _badge_audio(item)
    path_text = _badge_path_text(item)
    badges: list[dict[str, Any]] = []

    resolution = _resolution_badge(video, path_text)
    if resolution:
        badges.append({"key": "RES", "label": resolution, "style": "light"})
    source = _source_badge(path_text)
    if source:
        badges.append({"key": "SRC", "label": source, "style": "light"})
    hdr = _hdr_badge(video, path_text)
    if hdr:
        badges.append(hdr)
    audio_badge = _audio_badge(audio, path_text)
    if audio_badge:
        badges.append(audio_badge)
    if settings.get("badge_minimal", True):
        badges = [badge for badge in badges if _badge_is_high_value(badge)]
    return badges[:5]


# SDR is the default range and lossy codecs are table stakes, so they read as
# noise on a poster. Premium audio (ATMOS / DTS:X / DTS-HD / TRUEHD) stays.
_LOW_VALUE_AUDIO_LABELS = {"AAC", "AC3", "EAC3", "DTS", "FLAC", "OPUS", "MP3"}


def _badge_is_high_value(badge: dict[str, Any]) -> bool:
    key = str(badge.get("key") or "")
    if key == "SDR":
        return False
    if key == "AUDIO" and str(badge.get("label") or "") in _LOW_VALUE_AUDIO_LABELS:
        return False
    return True
