from __future__ import annotations

from pathlib import Path
from typing import Literal

from PIL import Image

from src.compressors.gif import compress_gif
from src.compressors.image import compress_image
from src.compressors.probe import CompressionResult, probe_media
from src.compressors.video import ProgressCallback, compress_video

MediaKind = Literal["image", "gif", "video"]
QualityPreset = Literal["light", "balanced", "heavy"]
ResolutionOption = Literal["auto", "original", "1080p", "720p", "480p", "360p"]

IMAGE_EXTENSIONS: set[str] = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}
VIDEO_EXTENSIONS: set[str] = {
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
    ".avi",
    ".flv",
    ".wmv",
    ".m4v",
    ".mpeg",
    ".mpg",
    ".3gp",
}


def classify_media(path: Path, content_type: str | None = None) -> MediaKind:
    """Determine whether the input file is a static image, animated GIF, or video."""
    ext = path.suffix.lower()
    ctype = (content_type or "").lower()

    if ext == ".gif" or "image/gif" in ctype:
        return "gif"

    if ext in VIDEO_EXTENSIONS or ctype.startswith("video/"):
        return "video"

    if ext in IMAGE_EXTENSIONS or ctype.startswith("image/"):
        # Check if it's an animated WebP/APNG
        try:
            with Image.open(path) as img:
                if getattr(img, "is_animated", False) and getattr(img, "n_frames", 1) > 1:
                    return "gif"
        except Exception:
            pass
        return "image"

    raise ValueError(
        f"Unsupported file type (`{path.suffix or content_type or 'unknown'}`). "
        "Please provide an image (PNG, JPG, WebP, BMP), GIF, or video (MP4, MOV, WebM, MKV)."
    )


async def route_and_compress(
    input_path: Path,
    output_dir: Path,
    media_kind: MediaKind,
    quality_preset: QualityPreset = "balanced",
    target_size_mb: float | None = None,
    max_upload_mb: float = 9.5,
    requested_format: str = "auto",
    resolution: ResolutionOption = "auto",
    prefer_nvenc: bool = True,
    progress_cb: ProgressCallback | None = None,
) -> CompressionResult:
    """Dispatch compression to the right engine based on input media kind and target format."""
    # Case 1: Static Image
    if media_kind == "image":
        if requested_format in {"mp4", "webm", "gif"}:
            raise ValueError(
                f"Cannot convert a static image to `{requested_format.upper()}`. "
                "Choose `Auto`, `WebP`, `JPG`, or `PNG` for images."
            )
        effective_target = (
            min(target_size_mb, max_upload_mb) if target_size_mb is not None else None
        )
        return await compress_image(
            input_path=input_path,
            output_dir=output_dir,
            quality_preset=quality_preset,
            target_size_mb=effective_target,
            requested_format=requested_format,
            resolution=resolution,
        )

    # Probe video/GIF metadata once
    info = await probe_media(input_path)

    # Case 2: User wants GIF output (either GIF->GIF or Video->GIF)
    if requested_format == "gif" or (media_kind == "gif" and requested_format == "auto"):
        return await compress_gif(
            input_path=input_path,
            output_dir=output_dir,
            quality_preset=quality_preset,
            target_size_mb=target_size_mb,
            max_upload_mb=max_upload_mb,
            resolution=resolution,
            progress_cb=progress_cb,
            media_info=info,
        )

    # Case 3: Video compression (or GIF -> MP4/WebM conversion)
    if requested_format in {"webp", "jpg", "png"}:
        raise ValueError(
            f"Cannot convert a video/animation to static `{requested_format.upper()}`. "
            "Choose `Auto`, `MP4`, `WebM`, or `GIF`."
        )

    return await compress_video(
        input_path=input_path,
        output_dir=output_dir,
        quality_preset=quality_preset,
        target_size_mb=target_size_mb,
        max_upload_mb=max_upload_mb,
        requested_format=requested_format,
        resolution=resolution,
        prefer_nvenc=prefer_nvenc,
        progress_cb=progress_cb,
        media_info=info,
    )
