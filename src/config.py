from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _parse_bool(val: str | None, default: bool = True) -> bool:
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(val: str | None, default: int) -> int:
    if val is None or val.strip() == "":
        return default
    try:
        return int(val.strip())
    except ValueError:
        return default


def _parse_optional_int(val: str | None) -> int | None:
    if val is None or val.strip() == "":
        return None
    try:
        return int(val.strip())
    except ValueError:
        return None


@dataclass(frozen=True)
class BotConfig:
    discord_token: str
    guild_id: int | None
    prefer_nvenc: bool
    max_concurrent_video_jobs: int
    max_concurrent_image_jobs: int
    max_input_file_mb: int

    @classmethod
    def from_env(cls) -> BotConfig:
        return cls(
            discord_token=os.getenv("DISCORD_TOKEN", "").strip(),
            guild_id=_parse_optional_int(os.getenv("DISCORD_GUILD_ID")),
            prefer_nvenc=_parse_bool(os.getenv("PREFER_NVENC"), default=True),
            max_concurrent_video_jobs=max(
                1, _parse_int(os.getenv("MAX_CONCURRENT_VIDEO_JOBS"), default=2)
            ),
            max_concurrent_image_jobs=max(
                1, _parse_int(os.getenv("MAX_CONCURRENT_IMAGE_JOBS"), default=5)
            ),
            max_input_file_mb=max(
                10, _parse_int(os.getenv("MAX_INPUT_FILE_MB"), default=500)
            ),
        )


CONFIG = BotConfig.from_env()
