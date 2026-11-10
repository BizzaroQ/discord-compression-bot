from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Literal

from src.compressors.probe import CompressionResult, MediaInfo, probe_media
from src.compressors.video import ProgressCallback, _run_ffmpeg_with_progress

QualityPreset = Literal["light", "balanced", "heavy"]
ResolutionOption = Literal["auto", "original", "1080p", "720p", "480p", "360p"]

# (max_width, max_fps, max_colors, bayer_scale)
GIF_TIERS: list[tuple[int, int, int, int]] = [
    (600, 18, 256, 2),  # Tier 0: Light
    (480, 15, 192, 3),  # Tier 1: Balanced
    (360, 12, 128, 4),  # Tier 2: Heavy
    (280, 10, 64, 5),   # Tier 3: Ultra-compact fallback
    (220, 8, 48, 5),    # Tier 4: Extreme target-size fallback
]


async def compress_gif(
    input_path: Path,
    output_dir: Path,
    quality_preset: QualityPreset = "balanced",
    target_size_mb: float | None = None,
    max_upload_mb: float = 9.5,
    resolution: ResolutionOption = "auto",
    progress_cb: ProgressCallback | None = None,
    media_info: MediaInfo | None = None,
) -> CompressionResult:
    """Compress an animated GIF or convert a short video to an optimized palette GIF."""
    start_time = time.perf_counter()
    info = media_info or await probe_media(input_path)
    duration = max(0.5, info.duration)

    output_path = output_dir / f"{input_path.stem}_compressed.gif"

    start_tier = {"light": 0, "balanced": 1, "heavy": 2}.get(quality_preset, 1)

    explicit_max_h: int | None = None
    if resolution in {"1080p", "720p", "480p", "360p"}:
        explicit_max_h = int(resolution.rstrip("p"))

    strict_limit_mb = min(target_size_mb, max_upload_mb) if target_size_mb else max_upload_mb
    strict_limit_bytes = int(strict_limit_mb * 1024 * 1024)

    orig_w = info.width or 480
    orig_h = info.height or 480
    out_w, out_h = orig_w, orig_h
    used_colors = 192

    for tier_idx in range(start_tier, len(GIF_TIERS)):
        tier_w, tier_fps, max_colors, bayer_scale = GIF_TIERS[tier_idx]
        used_colors = max_colors

        if resolution == "original" and tier_idx == start_tier:
            target_w = orig_w
        elif explicit_max_h is not None and tier_idx == start_tier:
            scale = min(1.0, explicit_max_h / max(1, orig_h))
            target_w = max(64, int(round(orig_w * scale)))
        else:
            target_w = min(orig_w, tier_w)

        scale_ratio = target_w / max(1, orig_w)
        out_w = max(32, int(round(orig_w * scale_ratio)))
        out_h = max(32, int(round(orig_h * scale_ratio)))
        eff_fps = min(info.fps or 15.0, float(tier_fps))

        filter_complex = (
            f"fps={eff_fps:.1f},scale={out_w}:{out_h}:flags=lanczos,split[s0][s1];"
            f"[s0]palettegen=max_colors={max_colors}:stats_mode=diff[p];"
            f"[s1][p]paletteuse=dither=bayer:bayer_scale={bayer_scale}:diff_mode=rectangle"
        )

        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-nostats",
            "-i",
            str(input_path),
            "-lavfi",
            filter_complex,
            "-loop",
            "0",
            str(output_path),
        ]

        rc, stderr_text = await _run_ffmpeg_with_progress(
            cmd=cmd,
            duration=duration,
            stage_label=f"Optimizing GIF ({out_w}x{out_h}, {max_colors} colors)",
            progress_cb=progress_cb,
        )

        if rc != 0 or not output_path.exists():
            raise RuntimeError(f"FFmpeg GIF optimization failed: {stderr_text[-400:]}")

        actual_bytes = output_path.stat().st_size
        # Stop if it fits under strict_limit_bytes AND (either user set target_size_mb or it shrunk vs original)
        if actual_bytes <= strict_limit_bytes:
            if target_size_mb is not None or actual_bytes < info.size_bytes:
                break

    elapsed = time.perf_counter() - start_time
    return CompressionResult(
        output_path=output_path,
        original_bytes=info.size_bytes,
        compressed_bytes=output_path.stat().st_size,
        original_resolution=(orig_w, orig_h),
        output_resolution=(out_w, out_h),
        encoder_used=f"FFmpeg Palette ({used_colors} colors)",
        output_format="gif",
        elapsed_seconds=elapsed,
    )
