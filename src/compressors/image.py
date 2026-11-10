from __future__ import annotations

import asyncio
import io
import time
from pathlib import Path
from typing import Literal

from PIL import Image, ImageOps

from src.compressors.probe import CompressionResult

QualityPreset = Literal["light", "balanced", "heavy"]
ResolutionOption = Literal["auto", "original", "1080p", "720p", "480p", "360p"]

RESOLUTION_HEIGHT_MAP: dict[str, int] = {
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
}

QUALITY_MAP: dict[QualityPreset, int] = {
    "light": 85,
    "balanced": 74,
    "heavy": 52,
}


def _resize_to_max_height(img: Image.Image, max_height: int) -> Image.Image:
    w, h = img.size
    if h <= max_height and w <= int(max_height * (16 / 9)):
        return img
    scale = min(max_height / h, (max_height * 16 / 9) / w)
    if scale >= 1.0:
        return img
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    return img.resize((new_w, new_h), Image.Resampling.LANCZOS)


def _prepare_for_format(img: Image.Image, fmt: str) -> Image.Image:
    """Ensure image color mode is compatible with the target output format."""
    if fmt in {"jpg", "jpeg"}:
        if img.mode in {"RGBA", "LA"} or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (30, 31, 34))  # Discord dark theme background
            bg.paste(rgba, mask=rgba.split()[3])
            return bg
        return img.convert("RGB")
    if fmt == "webp":
        if img.mode not in {"RGB", "RGBA"}:
            has_alpha = img.mode in {"LA", "RGBA"} or (
                img.mode == "P" and "transparency" in img.info
            )
            return img.convert("RGBA" if has_alpha else "RGB")
        return img
    if fmt == "png":
        return img
    return img.convert("RGB")


def _encode_to_bytes(img: Image.Image, fmt: str, quality: int) -> bytes:
    buf = io.BytesIO()
    if fmt in {"jpg", "jpeg"}:
        img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
    elif fmt == "webp":
        img.save(buf, format="WEBP", quality=quality, method=6)
    elif fmt == "png":
        # For PNG, quantize colors when quality < 85 to achieve real file size savings
        if quality < 85:
            colors = 256 if quality >= 70 else (128 if quality >= 55 else 64)
            quantized = img.quantize(colors=colors, method=Image.Quantize.FASTOCTREE)
            quantized.save(buf, format="PNG", optimize=True, compress_level=9)
        else:
            img.save(buf, format="PNG", optimize=True, compress_level=9)
    else:
        img.save(buf, format="WEBP", quality=quality, method=6)
    return buf.getvalue()


def _compress_image_sync(
    input_path: Path,
    output_dir: Path,
    quality_preset: QualityPreset,
    target_size_mb: float | None,
    requested_format: str,
    resolution: ResolutionOption,
) -> CompressionResult:
    start_time = time.perf_counter()
    original_bytes = input_path.stat().st_size

    with Image.open(input_path) as raw_img:
        img = ImageOps.exif_transpose(raw_img)
        orig_w, orig_h = img.size
        has_alpha = img.mode in {"RGBA", "LA"} or (
            img.mode == "P" and "transparency" in img.info
        )

        # Determine output format
        in_ext = input_path.suffix.lower().lstrip(".")
        if requested_format == "auto":
            if has_alpha:
                out_fmt = "webp"
            elif in_ext in {"jpg", "jpeg"}:
                out_fmt = "jpg"
            else:
                out_fmt = "webp"
        elif requested_format in {"jpg", "jpeg"}:
            out_fmt = "jpg"
        elif requested_format in {"webp", "png"}:
            out_fmt = requested_format
        else:
            out_fmt = "webp"

        # Apply explicit or preset resolution scaling
        working_img = img.copy()
        if resolution in RESOLUTION_HEIGHT_MAP:
            working_img = _resize_to_max_height(working_img, RESOLUTION_HEIGHT_MAP[resolution])
        elif resolution == "auto" and quality_preset == "heavy":
            working_img = _resize_to_max_height(working_img, 1080)

        working_img = _prepare_for_format(working_img, out_fmt)

        target_bytes = int(target_size_mb * 1024 * 1024 * 0.98) if target_size_mb else None

        if target_bytes is not None:
            # Binary search quality and scale if needed to fit under target_bytes
            best_bytes: bytes | None = None
            for _scale_step in range(6):
                # Check if lowest reasonable quality fits
                min_q = 12
                max_q = 95
                low_candidate = _encode_to_bytes(working_img, out_fmt, min_q)
                if len(low_candidate) <= target_bytes:
                    best_bytes = low_candidate
                    lo, hi = min_q, max_q
                    while lo <= hi:
                        mid = (lo + hi) // 2
                        cand = _encode_to_bytes(working_img, out_fmt, mid)
                        if len(cand) <= target_bytes:
                            best_bytes = cand
                            lo = mid + 1
                        else:
                            hi = mid - 1
                    break

                # Even min_q exceeded target_bytes -> downscale dimensions by 75%
                best_bytes = low_candidate
                cur_w, cur_h = working_img.size
                if cur_w <= 160 or cur_h <= 160:
                    break
                working_img = working_img.resize(
                    (max(1, int(cur_w * 0.75)), max(1, int(cur_h * 0.75))),
                    Image.Resampling.LANCZOS,
                )
            encoded_data = best_bytes or _encode_to_bytes(working_img, out_fmt, 50)
        else:
            base_q = QUALITY_MAP.get(quality_preset, 74)
            encoded_data = _encode_to_bytes(working_img, out_fmt, base_q)

            # If preset compression accidentally produced a larger file (e.g. already tiny JPEG),
            # step quality down or fall back to WebP so we still save space
            if len(encoded_data) >= original_bytes and base_q > 35:
                for q_try in (base_q - 15, base_q - 30, 35):
                    if q_try < 20:
                        break
                    smaller = _encode_to_bytes(working_img, out_fmt, q_try)
                    if len(smaller) < len(encoded_data):
                        encoded_data = smaller
                    if len(encoded_data) < original_bytes:
                        break

        out_w, out_h = working_img.size

    output_path = output_dir / f"{input_path.stem}_compressed.{out_fmt}"
    output_path.write_bytes(encoded_data)
    elapsed = time.perf_counter() - start_time

    return CompressionResult(
        output_path=output_path,
        original_bytes=original_bytes,
        compressed_bytes=len(encoded_data),
        original_resolution=(orig_w, orig_h),
        output_resolution=(out_w, out_h),
        encoder_used=f"Pillow ({out_fmt.upper()})",
        output_format=out_fmt,
        elapsed_seconds=elapsed,
    )


async def compress_image(
    input_path: Path,
    output_dir: Path,
    quality_preset: QualityPreset = "balanced",
    target_size_mb: float | None = None,
    requested_format: str = "auto",
    resolution: ResolutionOption = "auto",
) -> CompressionResult:
    """Compress a static image asynchronously in a worker thread."""
    return await asyncio.to_thread(
        _compress_image_sync,
        input_path,
        output_dir,
        quality_preset,
        target_size_mb,
        requested_format,
        resolution,
    )
