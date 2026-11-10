from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path
from typing import Awaitable, Callable, Literal

from src.compressors.probe import CompressionResult, MediaInfo, probe_media

QualityPreset = Literal["light", "balanced", "heavy"]
ResolutionOption = Literal["auto", "original", "1080p", "720p", "480p", "360p"]
ProgressCallback = Callable[[int, str], Awaitable[None]]

_NVENC_CHECKED: bool = False
_NVENC_AVAILABLE: bool = False

RESOLUTION_STEPS: list[int] = [1080, 720, 480, 360, 240]

PRESET_CRF_MAP: dict[QualityPreset, tuple[int, int]] = {
    # (libx264 CRF, h264_nvenc CQ)
    "light": (23, 25),
    "balanced": (28, 30),
    "heavy": (33, 35),
}


async def check_nvenc_available() -> bool:
    """Test whether NVIDIA h264_nvenc hardware encoding is operational on this system."""
    global _NVENC_CHECKED, _NVENC_AVAILABLE
    if _NVENC_CHECKED:
        return _NVENC_AVAILABLE

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=256x256:d=0.1",
        "-c:v",
        "h264_nvenc",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=8.0)
        _NVENC_AVAILABLE = proc.returncode == 0
    except Exception:
        _NVENC_AVAILABLE = False

    _NVENC_CHECKED = True
    return _NVENC_AVAILABLE


def _make_even(val: int) -> int:
    return max(2, val - (val % 2))


def choose_video_dimensions_and_fps(
    orig_w: int,
    orig_h: int,
    orig_fps: float,
    resolution: ResolutionOption,
    quality_preset: QualityPreset,
    target_video_kbps: int | None,
) -> tuple[int, int, float]:
    """Calculate optimal output (width, height, fps) to avoid blocky low-bitrate artifacts."""
    if orig_w <= 0 or orig_h <= 0:
        return (1280, 720, 30.0)

    aspect = orig_w / orig_h
    out_w, out_h = orig_w, orig_h
    out_fps = min(orig_fps, 60.0)

    def scale_to_height(target_h: int) -> tuple[int, int]:
        if orig_h <= target_h and orig_w <= int(target_h * 16 / 9):
            return (_make_even(orig_w), _make_even(orig_h))
        if orig_w >= orig_h:
            h = min(orig_h, target_h)
            w = int(round(h * aspect))
        else:
            # Vertical video (e.g. 9:16 phone clip): cap short edge at target_h
            w = min(orig_w, target_h)
            h = int(round(w / aspect))
        return (_make_even(w), _make_even(h))

    if resolution in {"1080p", "720p", "480p", "360p"}:
        target_h = int(resolution.rstrip("p"))
        out_w, out_h = scale_to_height(target_h)
    elif resolution == "original":
        out_w, out_h = _make_even(orig_w), _make_even(orig_h)
    else:
        # "auto" resolution scaling
        if quality_preset == "heavy":
            out_w, out_h = scale_to_height(720)
            out_fps = min(out_fps, 30.0)
        elif quality_preset == "balanced" and max(orig_w, orig_h) > 1920:
            out_w, out_h = scale_to_height(1080)

        # If we have a strict bitrate budget, check Bits-Per-Pixel (BPP)
        if target_video_kbps is not None:
            for step_h in RESOLUTION_STEPS:
                cand_w, cand_h = scale_to_height(step_h)
                cand_fps = min(out_fps, 30.0 if step_h <= 720 else out_fps)
                bpp = (target_video_kbps * 1000.0) / max(1.0, cand_w * cand_h * cand_fps)
                out_w, out_h, out_fps = cand_w, cand_h, cand_fps
                if bpp >= 0.048:
                    break

    return (out_w, out_h, round(out_fps, 2))


def calculate_bitrates(
    duration: float,
    target_size_mb: float,
    has_audio: bool,
    safety_factor: float = 0.92,
) -> tuple[int, int]:
    """Return (video_kbps, audio_kbps) to fit within target_size_mb."""
    effective_dur = max(0.5, duration)
    total_kbps = int((target_size_mb * 8192.0 * safety_factor) / effective_dur)

    if not has_audio:
        return (max(35, total_kbps), 0)

    if total_kbps >= 700:
        audio_kbps = 128
    elif total_kbps >= 320:
        audio_kbps = 96
    elif total_kbps >= 150:
        audio_kbps = 64
    else:
        audio_kbps = 48

    video_kbps = max(30, total_kbps - audio_kbps)
    return (video_kbps, audio_kbps)


async def _run_ffmpeg_with_progress(
    cmd: list[str],
    duration: float,
    stage_label: str,
    progress_cb: ProgressCallback | None = None,
) -> tuple[int, str]:
    """Execute FFmpeg reading -progress pipe:1 and firing throttled async progress updates."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    last_update = 0.0
    last_pct = -1

    assert proc.stdout is not None
    while True:
        line_bytes = await proc.stdout.readline()
        if not line_bytes:
            break
        line = line_bytes.decode("utf-8", errors="replace").strip()
        if line.startswith("out_time_us=") and duration > 0 and progress_cb is not None:
            val_str = line.split("=", 1)[1]
            try:
                out_sec = int(val_str) / 1_000_000.0
                pct = min(99, max(0, int((out_sec / duration) * 100)))
                now = time.monotonic()
                if pct != last_pct and (now - last_update >= 2.5 or pct >= 99):
                    last_pct = pct
                    last_update = now
                    try:
                        await progress_cb(pct, stage_label)
                    except Exception:
                        pass
            except ValueError:
                pass

    _, stderr_bytes = await proc.communicate()
    stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
    return (proc.returncode or 0, stderr_text)


def _build_ffmpeg_command(
    input_path: Path,
    output_path: Path,
    out_w: int,
    out_h: int,
    out_fps: float,
    orig_fps: float,
    has_audio: bool,
    out_fmt: str,
    use_nvenc: bool,
    quality_preset: QualityPreset,
    video_kbps: int | None,
    maxrate_kbps: int,
    audio_kbps: int,
) -> list[str]:
    vf_parts = [f"scale={out_w}:{out_h}:flags=lanczos"]
    if out_fps < orig_fps - 0.5:
        vf_parts.append(f"fps={out_fps}")
    vf_filter = ",".join(vf_parts)

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
        "-vf",
        vf_filter,
    ]

    bufsize_kbps = max(100, int(maxrate_kbps * 1.8))
    crf, cq = PRESET_CRF_MAP.get(quality_preset, (28, 30))

    if out_fmt == "webm":
        target_v = video_kbps if video_kbps is not None else maxrate_kbps
        cmd.extend(
            [
                "-c:v",
                "libvpx-vp9",
                "-b:v",
                f"{target_v}k",
                "-maxrate",
                f"{maxrate_kbps}k",
                "-bufsize",
                f"{bufsize_kbps}k",
                "-crf",
                str(crf),
                "-deadline",
                "good",
                "-cpu-used",
                "3",
                "-row-mt",
                "1",
                "-pix_fmt",
                "yuv420p",
            ]
        )
        if has_audio and audio_kbps > 0:
            cmd.extend(["-c:a", "libopus", "-b:a", f"{audio_kbps}k", "-ac", "2"])
        else:
            cmd.append("-an")
    else:
        # MP4 (H.264)
        if use_nvenc and out_w >= 146 and out_h >= 146 and out_w <= 4096 and out_h <= 4096:
            cmd.extend(
                [
                    "-c:v",
                    "h264_nvenc",
                    "-preset",
                    "p5",
                    "-tune",
                    "hq",
                    "-rc",
                    "vbr",
                    "-multipass",
                    "fullres",
                ]
            )
            if video_kbps is not None:
                cmd.extend(
                    [
                        "-b:v",
                        f"{video_kbps}k",
                        "-maxrate",
                        f"{maxrate_kbps}k",
                        "-bufsize",
                        f"{bufsize_kbps}k",
                    ]
                )
            else:
                cmd.extend(
                    [
                        "-cq",
                        str(cq),
                        "-b:v",
                        "0",
                        "-maxrate",
                        f"{maxrate_kbps}k",
                        "-bufsize",
                        f"{bufsize_kbps}k",
                    ]
                )
            cmd.extend(["-pix_fmt", "yuv420p"])
        else:
            cmd.extend(["-c:v", "libx264", "-preset", "medium"])
            if video_kbps is not None:
                cmd.extend(
                    [
                        "-b:v",
                        f"{video_kbps}k",
                        "-maxrate",
                        f"{maxrate_kbps}k",
                        "-bufsize",
                        f"{bufsize_kbps}k",
                    ]
                )
            else:
                cmd.extend(
                    [
                        "-crf",
                        str(crf),
                        "-maxrate",
                        f"{maxrate_kbps}k",
                        "-bufsize",
                        f"{bufsize_kbps}k",
                    ]
                )
            cmd.extend(["-pix_fmt", "yuv420p"])

        if has_audio and audio_kbps > 0:
            cmd.extend(["-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ac", "2"])
        else:
            cmd.append("-an")

        cmd.extend(["-movflags", "+faststart"])

    cmd.append(str(output_path))
    return cmd


async def compress_video(
    input_path: Path,
    output_dir: Path,
    quality_preset: QualityPreset = "balanced",
    target_size_mb: float | None = None,
    max_upload_mb: float = 9.5,
    requested_format: str = "auto",
    resolution: ResolutionOption = "auto",
    prefer_nvenc: bool = True,
    progress_cb: ProgressCallback | None = None,
    media_info: MediaInfo | None = None,
) -> CompressionResult:
    """Compress a video file using NVENC hardware acceleration or libx264/VP9 fallback."""
    start_time = time.perf_counter()
    info = media_info or await probe_media(input_path)
    duration = max(0.5, info.duration)

    out_fmt = "webm" if requested_format == "webm" else "mp4"
    output_path = output_dir / f"{input_path.stem}_compressed.{out_fmt}"

    # Determine effective size ceiling:
    # 1. If user explicitly set target_size_mb, cap at min(target_size_mb, max_upload_mb)
    # 2. If user did not set target_size_mb, still ensure output fits under max_upload_mb
    #    and is smaller than the original input file.
    orig_mb = info.size_bytes / (1024 * 1024)
    effective_target_mb: float | None = None
    if target_size_mb is not None:
        effective_target_mb = min(target_size_mb, max_upload_mb)
    elif orig_mb > max_upload_mb:
        effective_target_mb = max_upload_mb

    # Calculate maxrate ceiling so even CRF/CQ encodes never exceed max_upload_mb or original size
    ceiling_mb = effective_target_mb if effective_target_mb is not None else min(
        max_upload_mb, orig_mb * 0.85
    )
    cap_video_kbps, audio_kbps = calculate_bitrates(
        duration=duration,
        target_size_mb=ceiling_mb,
        has_audio=info.has_audio,
        safety_factor=0.91,
    )

    explicit_video_kbps = cap_video_kbps if effective_target_mb is not None else None
    maxrate_kbps = (
        int(explicit_video_kbps * 1.12)
        if explicit_video_kbps is not None
        else cap_video_kbps
    )

    out_w, out_h, out_fps = choose_video_dimensions_and_fps(
        orig_w=info.width or 1280,
        orig_h=info.height or 720,
        orig_fps=info.fps,
        resolution=resolution,
        quality_preset=quality_preset,
        target_video_kbps=explicit_video_kbps or cap_video_kbps,
    )

    nvenc_ok = (await check_nvenc_available()) if (prefer_nvenc and out_fmt == "mp4") else False
    encoder_label = (
        "VP9 (libvpx-vp9)"
        if out_fmt == "webm"
        else ("H.264 (NVENC GPU)" if nvenc_ok else "H.264 (libx264 CPU)")
    )

    cmd = _build_ffmpeg_command(
        input_path=input_path,
        output_path=output_path,
        out_w=out_w,
        out_h=out_h,
        out_fps=out_fps,
        orig_fps=info.fps,
        has_audio=info.has_audio,
        out_fmt=out_fmt,
        use_nvenc=nvenc_ok,
        quality_preset=quality_preset,
        video_kbps=explicit_video_kbps,
        maxrate_kbps=maxrate_kbps,
        audio_kbps=audio_kbps,
    )

    rc, stderr_text = await _run_ffmpeg_with_progress(
        cmd=cmd,
        duration=duration,
        stage_label=f"Encoding video ({encoder_label})",
        progress_cb=progress_cb,
    )

    # If NVENC failed for any reason (e.g., busy GPU sessions or driver hiccup), fall back to libx264
    if rc != 0 and nvenc_ok:
        nvenc_ok = False
        encoder_label = "H.264 (libx264 CPU)"
        cmd = _build_ffmpeg_command(
            input_path=input_path,
            output_path=output_path,
            out_w=out_w,
            out_h=out_h,
            out_fps=out_fps,
            orig_fps=info.fps,
            has_audio=info.has_audio,
            out_fmt=out_fmt,
            use_nvenc=False,
            quality_preset=quality_preset,
            video_kbps=explicit_video_kbps,
            maxrate_kbps=maxrate_kbps,
            audio_kbps=audio_kbps,
        )
        rc, stderr_text = await _run_ffmpeg_with_progress(
            cmd=cmd,
            duration=duration,
            stage_label=f"Encoding video ({encoder_label})",
            progress_cb=progress_cb,
        )

    if rc != 0 or not output_path.exists():
        raise RuntimeError(f"FFmpeg video encoding failed: {stderr_text[-400:]}")

    # Verify strict size cap if target_size_mb or max_upload_mb was exceeded
    strict_limit_mb = effective_target_mb or max_upload_mb
    strict_limit_bytes = int(strict_limit_mb * 1024 * 1024)
    actual_bytes = output_path.stat().st_size

    if actual_bytes > strict_limit_bytes:
        # Tightened retry pass using libx264 with scaled-down bitrate
        ratio = strict_limit_bytes / max(1, actual_bytes)
        base_kbps = explicit_video_kbps or cap_video_kbps
        retry_video_kbps = max(25, int(base_kbps * ratio * 0.88))
        retry_audio_kbps = min(audio_kbps, 64) if info.has_audio else 0

        out_w, out_h, out_fps = choose_video_dimensions_and_fps(
            orig_w=out_w,
            orig_h=out_h,
            orig_fps=out_fps,
            resolution="auto" if resolution == "auto" else resolution,
            quality_preset="heavy",
            target_video_kbps=retry_video_kbps,
        )
        encoder_label = "H.264 (libx264 Tight Pass)" if out_fmt == "mp4" else encoder_label
        retry_cmd = _build_ffmpeg_command(
            input_path=input_path,
            output_path=output_path,
            out_w=out_w,
            out_h=out_h,
            out_fps=out_fps,
            orig_fps=info.fps,
            has_audio=info.has_audio,
            out_fmt=out_fmt,
            use_nvenc=False,
            quality_preset="heavy",
            video_kbps=retry_video_kbps,
            maxrate_kbps=retry_video_kbps,
            audio_kbps=retry_audio_kbps,
        )
        rc, stderr_text = await _run_ffmpeg_with_progress(
            cmd=retry_cmd,
            duration=duration,
            stage_label="Refining file size to fit target",
            progress_cb=progress_cb,
        )
        if rc != 0 or not output_path.exists():
            raise RuntimeError(f"FFmpeg retry pass failed: {stderr_text[-400:]}")
        actual_bytes = output_path.stat().st_size

    elapsed = time.perf_counter() - start_time
    return CompressionResult(
        output_path=output_path,
        original_bytes=info.size_bytes,
        compressed_bytes=actual_bytes,
        original_resolution=(info.width or out_w, info.height or out_h),
        output_resolution=(out_w, out_h),
        encoder_used=encoder_label,
        output_format=out_fmt,
        elapsed_seconds=elapsed,
    )
