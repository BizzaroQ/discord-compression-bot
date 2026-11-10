from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

import discord
from discord import app_commands

from src.compressors.probe import CompressionResult
from src.compressors.router import (
    QualityPreset,
    ResolutionOption,
    classify_media,
    route_and_compress,
)
from src.compressors.video import check_nvenc_available
from src.config import CONFIG
from src.queue_manager import CompressionQueueManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("compress-bot")


def format_size(num_bytes: int) -> str:
    mb = num_bytes / (1024 * 1024)
    if mb >= 1.0:
        return f"{mb:.2f} MB"
    kb = num_bytes / 1024
    return f"{kb:.1f} KB"


def render_progress_bar(percent: int, width: int = 12) -> str:
    clamped = max(0, min(100, percent))
    filled = int(round((clamped / 100.0) * width))
    return "█" * filled + "░" * (width - filled)


def get_max_upload_mb(interaction: discord.Interaction) -> float:
    """Determine the maximum file size the bot can upload in this interaction context."""
    limit_bytes: int | None = None
    if interaction.guild is not None:
        limit_bytes = interaction.guild.filesize_limit
    if not limit_bytes:
        limit_bytes = getattr(interaction, "attachment_size_limit", None)
    if not limit_bytes:
        limit_bytes = 10 * 1024 * 1024  # 10 MB standard Discord default

    # Leave a 5% buffer for Discord multipart payload overhead
    return round((limit_bytes / (1024 * 1024)) * 0.95, 2)


def build_result_embed(
    filename: str,
    result: CompressionResult,
) -> discord.Embed:
    saved_pct = result.saved_ratio
    if saved_pct >= 0:
        delta_str = f"-{saved_pct:.1f}%"
        color = discord.Color.from_rgb(46, 204, 113)
    else:
        delta_str = f"+{abs(saved_pct):.1f}%"
        color = discord.Color.from_rgb(241, 196, 15)

    orig_w, orig_h = result.original_resolution
    out_w, out_h = result.output_resolution
    res_str = (
        f"{orig_w}×{orig_h} → {out_w}×{out_h}"
        if (orig_w, orig_h) != (out_w, out_h)
        else f"{out_w}×{out_h}"
    )

    embed = discord.Embed(
        title="✅ Media Compressed",
        color=color,
    )
    embed.add_field(
        name="Size",
        value=f"**{format_size(result.original_bytes)}** → **{format_size(result.compressed_bytes)}** (`{delta_str}`)",
        inline=False,
    )
    embed.add_field(name="Resolution", value=f"`{res_str}`", inline=True)
    embed.add_field(name="Encoder", value=f"`{result.encoder_used}`", inline=True)
    embed.add_field(name="Time", value=f"`{result.elapsed_seconds:.2f}s`", inline=True)
    embed.set_footer(text=f"Source: {filename}")
    return embed


class CompressionBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.queue = CompressionQueueManager(
            max_video_jobs=CONFIG.max_concurrent_video_jobs,
            max_image_jobs=CONFIG.max_concurrent_image_jobs,
        )

    async def setup_hook(self) -> None:
        nvenc = await check_nvenc_available() if CONFIG.prefer_nvenc else False
        logger.info(
            "Hardware NVENC available: %s (prefer_nvenc=%s)",
            nvenc,
            CONFIG.prefer_nvenc,
        )
        if CONFIG.guild_id:
            guild_obj = discord.Object(id=CONFIG.guild_id)
            self.tree.copy_global_to(guild=guild_obj)
            synced = await self.tree.sync(guild=guild_obj)
            logger.info("Synced %d command(s) to guild ID %d", len(synced), CONFIG.guild_id)
        else:
            synced = await self.tree.sync()
            logger.info("Synced %d global command(s)", len(synced))

    async def on_ready(self) -> None:
        assert self.user is not None
        logger.info("Logged in as %s (ID: %d)", self.user, self.user.id)


client = CompressionBot()


async def _process_attachment_compression(
    interaction: discord.Interaction,
    attachment: discord.Attachment,
    target_size_mb: float | None = None,
    quality: QualityPreset = "balanced",
    fmt: str = "auto",
    resolution: ResolutionOption = "auto",
) -> None:
    max_input_bytes = CONFIG.max_input_file_mb * 1024 * 1024
    if attachment.size > max_input_bytes:
        await interaction.followup.send(
            f"❌ File `{attachment.filename}` ({format_size(attachment.size)}) exceeds the maximum "
            f"allowed input size of **{CONFIG.max_input_file_mb} MB**.",
            ephemeral=True,
        )
        return

    max_upload_mb = get_max_upload_mb(interaction)
    if target_size_mb is not None and target_size_mb <= 0.05:
        await interaction.followup.send(
            "❌ `target_size_mb` must be at least `0.1` MB.",
            ephemeral=True,
        )
        return

    with tempfile.TemporaryDirectory(prefix="discord_compress_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        safe_name = Path(attachment.filename or "input_media").name
        input_path = tmp_dir / f"in_{safe_name}"
        output_dir = tmp_dir / "out"
        output_dir.mkdir(parents=True, exist_ok=True)

        await interaction.edit_original_response(
            content=f"⬇️ Downloading `{safe_name}` ({format_size(attachment.size)})..."
        )
        await attachment.save(input_path)

        try:
            media_kind = classify_media(input_path, attachment.content_type)
        except ValueError as exc:
            await interaction.edit_original_response(content=f"❌ {exc}")
            return

        async def on_queued(pos: int) -> None:
            await interaction.edit_original_response(
                content=f"⏳ Queued for compression (Position **#{pos}** in line)..."
            )

        async def on_progress(pct: int, stage: str) -> None:
            bar = render_progress_bar(pct)
            await interaction.edit_original_response(
                content=f"⚙️ `{bar}` **{pct}%** — {stage}..."
            )

        slot_ctx = (
            client.queue.acquire_image_slot(on_queued=on_queued)
            if media_kind == "image"
            else client.queue.acquire_video_slot(on_queued=on_queued)
        )

        try:
            async with slot_ctx:
                await interaction.edit_original_response(
                    content=f"⚙️ Compressing `{safe_name}` ({media_kind.upper()}, preset: `{quality}`)..."
                )
                result = await route_and_compress(
                    input_path=input_path,
                    output_dir=output_dir,
                    media_kind=media_kind,
                    quality_preset=quality,
                    target_size_mb=target_size_mb,
                    max_upload_mb=max_upload_mb,
                    requested_format=fmt,
                    resolution=resolution,
                    prefer_nvenc=CONFIG.prefer_nvenc,
                    progress_cb=on_progress,
                )
        except Exception as exc:
            logger.exception("Compression failed for %s", safe_name)
            await interaction.edit_original_response(
                content=f"❌ Compression failed: `{exc}`"
            )
            return

        await interaction.edit_original_response(
            content=f"⬆️ Uploading compressed file ({format_size(result.compressed_bytes)})..."
        )

        embed = build_result_embed(safe_name, result)
        discord_file = discord.File(result.output_path, filename=result.output_path.name)
        await interaction.edit_original_response(
            content=None,
            embed=embed,
            attachments=[discord_file],
        )


@client.tree.command(
    name="compress",
    description="Compress an image, video, or GIF with optional target size, quality preset, and format.",
)
@app_commands.describe(
    attachment="The image, video, or GIF file to compress",
    target_size_mb="Target maximum file size in MB (e.g. 8, 10, 25). Defaults to server upload limit.",
    quality="Compression preset (Light = higher quality, Heavy = smallest file)",
    format="Output file format (defaults to Auto)",
    resolution="Output video/image resolution cap (defaults to Auto smart scaling)",
)
@app_commands.choices(
    quality=[
        app_commands.Choice(name="Light (High Quality)", value="light"),
        app_commands.Choice(name="Balanced (Recommended)", value="balanced"),
        app_commands.Choice(name="Heavy (Smallest Size)", value="heavy"),
    ],
    format=[
        app_commands.Choice(name="Auto (Best for media type)", value="auto"),
        app_commands.Choice(name="MP4 (H.264 Video - Universal)", value="mp4"),
        app_commands.Choice(name="WebM (VP9 Video)", value="webm"),
        app_commands.Choice(name="GIF (Animated Palette GIF)", value="gif"),
        app_commands.Choice(name="WebP (Image)", value="webp"),
        app_commands.Choice(name="JPG (Image)", value="jpg"),
        app_commands.Choice(name="PNG (Image)", value="png"),
    ],
    resolution=[
        app_commands.Choice(name="Auto (Smart Bitrate Scaling)", value="auto"),
        app_commands.Choice(name="Original Resolution", value="original"),
        app_commands.Choice(name="1080p", value="1080p"),
        app_commands.Choice(name="720p", value="720p"),
        app_commands.Choice(name="480p", value="480p"),
        app_commands.Choice(name="360p", value="360p"),
    ],
)
async def compress_slash_command(
    interaction: discord.Interaction,
    attachment: discord.Attachment,
    target_size_mb: app_commands.Range[float, 0.1, 500.0] | None = None,
    quality: app_commands.Choice[str] | None = None,
    format: app_commands.Choice[str] | None = None,
    resolution: app_commands.Choice[str] | None = None,
) -> None:
    await interaction.response.defer(thinking=True)

    quality_val: QualityPreset = (
        quality.value if quality is not None else "balanced"  # type: ignore[assignment]
    )
    fmt_val = format.value if format is not None else "auto"
    res_val: ResolutionOption = (
        resolution.value if resolution is not None else "auto"  # type: ignore[assignment]
    )

    await _process_attachment_compression(
        interaction=interaction,
        attachment=attachment,
        target_size_mb=target_size_mb,
        quality=quality_val,
        fmt=fmt_val,
        resolution=res_val,
    )


@client.tree.context_menu(name="Compress Media")
async def compress_message_context_menu(
    interaction: discord.Interaction,
    message: discord.Message,
) -> None:
    if not message.attachments:
        await interaction.response.send_message(
            "❌ That message doesn't have any file attachments to compress.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)
    await _process_attachment_compression(
        interaction=interaction,
        attachment=message.attachments[0],
        target_size_mb=None,
        quality="balanced",
        fmt="auto",
        resolution="auto",
    )


def main() -> None:
    if not CONFIG.discord_token or CONFIG.discord_token == "your_discord_bot_token_here":
        raise SystemExit(
            "ERROR: DISCORD_TOKEN is not set. Copy .env.example to .env and add your Discord Bot Token."
        )
    client.run(CONFIG.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
