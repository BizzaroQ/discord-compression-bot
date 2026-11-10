from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from src.compressors.probe import probe_media
from src.compressors.router import classify_media, route_and_compress
from src.compressors.video import check_nvenc_available
from src.queue_manager import CompressionQueueManager


def _create_sample_image(path: Path, size: tuple[int, int] = (1600, 1200), rgba: bool = False) -> None:
    mode = "RGBA" if rgba else "RGB"
    img = Image.new(mode, size)
    draw = ImageDraw.Draw(img)
    for y in range(0, size[1], 10):
        for x in range(0, size[0], 10):
            color = ((x * 7) % 255, (y * 13) % 255, ((x + y) * 3) % 255)
            if rgba:
                color = (*color, 200)
            draw.rectangle([x, y, x + 9, y + 9], fill=color)
    img.save(path)


def _create_sample_video(path: Path, duration: float = 2.0) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=duration={duration}:size=1280x720:rate=30",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={duration}",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "12",
        "-c:a",
        "aac",
        str(path),
    ]
    subprocess.run(cmd, check=True)


def _create_sample_gif(path: Path, duration: float = 1.5) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=duration={duration}:size=640x480:rate=20",
        str(path),
    ]
    subprocess.run(cmd, check=True)


@pytest.mark.asyncio
async def test_image_compression_presets_and_target_size(tmp_path: Path) -> None:
    in_png = tmp_path / "sample.png"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _create_sample_image(in_png, size=(1600, 1200), rgba=True)

    assert classify_media(in_png) == "image"

    # 1. Balanced preset (auto -> webp for RGBA PNG)
    res_webp = await route_and_compress(
        input_path=in_png,
        output_dir=out_dir,
        media_kind="image",
        quality_preset="balanced",
        requested_format="auto",
    )
    assert res_webp.output_path.exists()
    assert res_webp.output_format == "webp"
    assert res_webp.compressed_bytes < res_webp.original_bytes

    # 2. Target size mode with JPG conversion & 720p cap
    res_jpg = await route_and_compress(
        input_path=in_png,
        output_dir=out_dir,
        media_kind="image",
        quality_preset="balanced",
        target_size_mb=0.08,  # 80 KB cap
        requested_format="jpg",
        resolution="720p",
    )
    assert res_jpg.output_path.exists()
    assert res_jpg.output_format == "jpg"
    assert res_jpg.compressed_bytes <= int(0.08 * 1024 * 1024)
    assert res_jpg.output_resolution[1] <= 720


@pytest.mark.asyncio
async def test_video_probe_and_compression(tmp_path: Path) -> None:
    in_mp4 = tmp_path / "sample.mp4"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _create_sample_video(in_mp4, duration=2.0)

    assert classify_media(in_mp4) == "video"
    info = await probe_media(in_mp4)
    assert 1.8 <= info.duration <= 2.3
    assert info.width == 1280
    assert info.height == 720
    assert info.has_audio is True

    progress_events: list[int] = []

    async def on_progress(pct: int, _stage: str) -> None:
        progress_events.append(pct)

    res = await route_and_compress(
        input_path=in_mp4,
        output_dir=out_dir,
        media_kind="video",
        quality_preset="heavy",
        target_size_mb=0.25,
        requested_format="mp4",
        resolution="auto",
        prefer_nvenc=await check_nvenc_available(),
        progress_cb=on_progress,
    )
    assert res.output_path.exists()
    assert res.output_format == "mp4"
    assert res.compressed_bytes <= int(0.25 * 1024 * 1024)


@pytest.mark.asyncio
async def test_gif_compression(tmp_path: Path) -> None:
    in_gif = tmp_path / "sample.gif"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _create_sample_gif(in_gif, duration=1.5)

    assert classify_media(in_gif) == "gif"
    res = await route_and_compress(
        input_path=in_gif,
        output_dir=out_dir,
        media_kind="gif",
        quality_preset="balanced",
        requested_format="auto",
    )
    assert res.output_path.exists()
    assert res.output_format == "gif"
    assert res.compressed_bytes < res.original_bytes


@pytest.mark.asyncio
async def test_queue_manager() -> None:
    q = CompressionQueueManager(max_video_jobs=1, max_image_jobs=2)
    queued_positions: list[int] = []

    async def on_queued(pos: int) -> None:
        queued_positions.append(pos)

    async with q.acquire_video_slot():
        # Start a second job that must wait
        async def second_worker() -> None:
            async with q.acquire_video_slot(on_queued=on_queued):
                await asyncio.sleep(0.02)

        task = asyncio.create_task(second_worker())
        await asyncio.sleep(0.02)
        assert q.waiting_video_jobs == 1
    await task
    assert queued_positions == [1]
    assert q.waiting_video_jobs == 0
