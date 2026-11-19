from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict, List, Tuple

from tg_plugin.models import TelegramMediaEnvelope


class MediaGroupBuffer:
    """Debounces Telegram media_group_id items so an album lands in one CineVec batch_id."""

    def __init__(
        self,
        debounce_seconds: float,
        on_flush: Callable[[List[TelegramMediaEnvelope]], Awaitable[None]],
    ) -> None:
        self._debounce = max(0.05, float(debounce_seconds))
        self._on_flush = on_flush
        self._groups: Dict[Tuple[int, str], List[TelegramMediaEnvelope]] = {}
        self._timers: Dict[Tuple[int, str], asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def push(self, envelope: TelegramMediaEnvelope) -> None:
        if not envelope.media_group_id:
            await self._on_flush([envelope])
            return
        key = (envelope.chat_id, str(envelope.media_group_id))
        async with self._lock:
            self._groups.setdefault(key, []).append(envelope)
            old_timer = self._timers.pop(key, None)
            if old_timer is not None:
                old_timer.cancel()
            self._timers[key] = asyncio.create_task(self._wait_and_flush(key))

    async def _wait_and_flush(self, key: Tuple[int, str]) -> None:
        try:
            await asyncio.sleep(self._debounce)
            async with self._lock:
                items = self._groups.pop(key, [])
                self._timers.pop(key, None)
            if items:
                await self._on_flush(items)
        except asyncio.CancelledError:
            return

    async def flush_all(self) -> None:
        async with self._lock:
            timers = list(self._timers.values())
            self._timers.clear()
            pending = list(self._groups.values())
            self._groups.clear()
        for t in timers:
            t.cancel()
        for items in pending:
            if items:
                await self._on_flush(items)
