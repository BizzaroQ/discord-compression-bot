> [!WARNING]
> # ⚠️ DISCLAIMER: THIS PROJECT IS UNTESTED AND VIBE CODED ⚠️
> **This entire codebase is UNTESTED and VIBE CODED.**
> Use at your own risk. Nothing in this repository and I won't for a while. Expect bugs, edge cases, and really... anythings on the table. Stay safe.

---

# Discord Media Compression Bot

A self-hosted Discord bot for Linux that compresses **Videos**, **Images**, and **Animated GIFs** using `discord.py`, `Pillow`, and `FFmpeg` (with automatic **NVIDIA NVENC GPU acceleration** and `libx264` CPU fallback).

## Features

- **`/compress` Slash Command**:
  - `attachment`: Any video (`MP4`, `MOV`, `MKV`, `WebM`, `AVI`), image (`PNG`, `JPG`, `WebP`, `BMP`, `TIFF`), or `GIF`.
  - `target_size_mb`: Target a specific file size ceiling in MB (e.g., `8`, `10`, `25`), or leave empty to automatically fit within the Discord server's upload limit.
  - `quality`: Choose between `Light (High Quality)`, `Balanced (Recommended)`, or `Heavy (Smallest Size)`.
  - `format`: Keep `Auto`, or convert between `MP4 (H.264)`, `WebM (VP9)`, `GIF`, `WebP`, `JPG`, and `PNG`.
  - `resolution`: `Auto` (smart Bits-Per-Pixel scaling to prevent blocky low-bitrate video), `Original`, `1080p`, `720p`, `480p`, or `360p`.
- **Right-Click Context Menu (`Apps -> Compress Media`)**:
  - Right-click any existing message with an attachment in Discord and compress it in one click.
- **Live Progress Bar**:
  - Parses `ffmpeg -progress` in real time to display a live progress bar while encoding.
- **Concurrency Queue**:
  - Protects your host's GPU/CPU with bounded worker semaphores and notifies waiting users of their queue position.

---

## Linux Prerequisites

Ensure `ffmpeg` (which includes `ffprobe`) and [`uv`](https://docs.astral.sh/uv/) are installed on your Linux system:

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y ffmpeg

# Fedora / RHEL
sudo dnf install -y ffmpeg

# Arch Linux
sudo pacman -S ffmpeg

# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

*(Optional)* For hardware-accelerated `h264_nvenc` video encoding on Linux, ensure the proprietary NVIDIA drivers and `nvidia-utils` / CUDA runtime libraries are installed so `ffmpeg -encoders | grep nvenc` is available. Otherwise, the bot automatically falls back to CPU (`libx264`).

---

## Setup & Running (Linux)

### 1. Create Your Discord Bot Token
1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**.
2. Open the **Bot** tab on the left, click **Reset Token**, and copy your bot token.
3. Open the **OAuth2 -> URL Generator** tab:
   - Under **Scopes**, check `bot` and `applications.commands`.
   - Under **Bot Permissions**, check `Send Messages`, `Attach Files`, `Embed Links`, and `Use Slash Commands`.
   - Open the generated URL in your browser to invite the bot to your server.

### 2. Configure `.env`
Copy `.env.example` to `.env` and add your token:
```bash
cp .env.example .env
nano .env
```
Set your `DISCORD_TOKEN`:
```env
DISCORD_TOKEN=your_actual_bot_token_here
# Optional: Add your server ID for instant slash command updates during development
DISCORD_GUILD_ID=
```

### 3. Install Dependencies & Run (via `uv`)
```bash
# Create virtual environment and install dependencies into .venv
uv sync

# Start the bot
uv run python -m src.bot
```

### 4. Run Tests (Optional)
```bash
uv run --extra dev pytest
```
