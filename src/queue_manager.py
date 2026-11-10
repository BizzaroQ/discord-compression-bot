from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable, Awaitable


class CompressionQueueManager:
    """Manages concurrency limits for heavy (video/GIF) and light (image) compression jobs."""

    def __init__(self, max_video_jobs: int = 2, max_image_jobs: int = 5) -> None:
        self._video_sem = asyncio.Semaphore(max_video_jobs)
        self._image_sem = asyncio.Semaphore(max_image_jobs)
        self._waiting_video = 0
        self._waiting_image = 0

    @property
    def waiting_video_jobs(self) -> int:
        return self._waiting_video

    @property
    def waiting_image_jobs(self) -> int:
        return self._waiting_image

    @asynccontextmanager
    async def acquire_video_slot(
        self,
        on_queued: Callable[[int], Awaitable[None]] | None = None,
    ) -> AsyncIterator[None]:
        if self._video_sem.locked():
            self._waiting_video += 1
            position = self._waiting_video
            if on_queued is not None:
                try:
                    await on_queued(position)
                except Exception:
                    pass
            try:
                await self._video_sem.acquire()
            finally:
                self._waiting_video -= 1
        else:
            await self._video_sem.acquire()

        try:
            yield
        finally:
            self._video_sem.release()

    @asynccontextmanager
    async def acquire_image_slot(
        self,
        on_queued: Callable[[int], Awaitable[None]] | None = None,
    ) -> AsyncIterator[None]:
        if self._image_sem.locked():
            self._waiting_image += 1
            position = self._waiting_image
            if on_queued is not None:
                try:
                    await on_queued(position)
                except Exception:
                    pass
            try:
                await self._image_sem.acquire()
            finally:
                self._waiting_image -= 1
        else:
            await self._image_sem.acquire()

        try:
            yield
        finally:
            self._image_sem.release()
