from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    size_bytes: int
    width: int
    height: int
    fps: float
    video_codec: str | None
    audio_codec: str | None
    has_audio: bool
    nb_frames: int
    format_name: str


@dataclass(frozen=True)
class CompressionResult:
    output_path: Path
    original_bytes: int
    compressed_bytes: int
    original_resolution: tuple[int, int]
    output_resolution: tuple[int, int]
    encoder_used: str
    output_format: str
    elapsed_seconds: float

    @property
    def saved_ratio(self) -> float:
        if self.original_bytes <= 0:
            return 0.0
        return (1.0 - (self.compressed_bytes / self.original_bytes)) * 100.0


def _parse_frame_rate(rate_str: str | None) -> float:
    if not rate_str:
        return 30.0
    try:
        if "/" in rate_str:
            num_s, den_s = rate_str.split("/", 1)
            num, den = float(num_s), float(den_s)
            if den > 0 and num > 0:
                fps = num / den
                return min(240.0, max(1.0, fps))
        val = float(rate_str)
        return min(240.0, max(1.0, val)) if val > 0 else 30.0
    except (ValueError, ZeroDivisionError):
        return 30.0


def _parse_tag_duration(dur_str: str | None) -> float:
    """Parse HH:MM:SS.mmmmmm duration tag often present in MKV/WebM streams."""
    if not dur_str:
        return 0.0
    try:
        parts = dur_str.strip().split(":")
        if len(parts) == 3:
            hours = float(parts[0])
            minutes = float(parts[1])
            seconds = float(parts[2])
            return hours * 3600.0 + minutes * 60.0 + seconds
    except ValueError:
        pass
    return 0.0


async def probe_media(path: Path) -> MediaInfo:
    """Run ffprobe asynchronously and extract media stream and container metadata."""
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err_msg = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffprobe failed on {path.name}: {err_msg or 'unknown error'}")

    data = json.loads(stdout.decode("utf-8", errors="replace"))
    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = 0.0
    if "duration" in fmt:
        try:
            duration = float(fmt["duration"])
        except (ValueError, TypeError):
            duration = 0.0

    if duration <= 0.0 and video_stream:
        if "duration" in video_stream:
            try:
                duration = float(video_stream["duration"])
            except (ValueError, TypeError):
                duration = 0.0
        if duration <= 0.0:
            tags = video_stream.get("tags") or {}
            for key, val in tags.items():
                if key.upper().startswith("DURATION"):
                    duration = _parse_tag_duration(str(val))
                    if duration > 0:
                        break

    size_bytes = path.stat().st_size
    width = int(video_stream.get("width", 0)) if video_stream else 0
    height = int(video_stream.get("height", 0)) if video_stream else 0

    fps = 30.0
    if video_stream:
        fps = _parse_frame_rate(
            video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
        )

    nb_frames = 1
    if video_stream and "nb_frames" in video_stream:
        try:
            nb_frames = int(video_stream["nb_frames"])
        except (ValueError, TypeError):
            nb_frames = 1

    if duration <= 0.0 and nb_frames > 1 and fps > 0:
        duration = nb_frames / fps

    return MediaInfo(
        duration=max(0.0, duration),
        size_bytes=size_bytes,
        width=width,
        height=height,
        fps=fps,
        video_codec=video_stream.get("codec_name") if video_stream else None,
        audio_codec=audio_stream.get("codec_name") if audio_stream else None,
        has_audio=audio_stream is not None,
        nb_frames=nb_frames,
        format_name=str(fmt.get("format_name", "")),
    )
