from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from tg_plugin.cinevec_client import CineVecAPIError, CineVecClient


class BackpressureController:
    """Pauses Telegram media downloads when local free disk is low or CineVec preprocess queue is saturated."""

    def __init__(
        self,
        tmp_dir: Path,
        cinevec: CineVecClient,
        *,
        disk_min_free_gb: float = 10.0,
        max_preprocess_inflight: int = 2,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self._tmp_dir = Path(tmp_dir)
        self._cinevec = cinevec
        self._disk_min_free_gb = float(disk_min_free_gb)
        self._max_preprocess_inflight = max(1, int(max_preprocess_inflight))
        self._poll_interval = max(0.2, float(poll_interval_seconds))
        self._active_batches: Set[str] = set()

    def track_batch(self, batch_id: Optional[str]) -> None:
        if batch_id:
            self._active_batches.add(batch_id)

    def update_limits(
        self,
        *,
        disk_min_free_gb: Optional[float] = None,
        max_preprocess_inflight: Optional[int] = None,
    ) -> None:
        if disk_min_free_gb is not None:
            self._disk_min_free_gb = float(disk_min_free_gb)
        if max_preprocess_inflight is not None:
            self._max_preprocess_inflight = max(1, int(max_preprocess_inflight))

    def seed_batches(self, batch_ids: Iterable[str]) -> None:
        for bid in batch_ids:
            if bid:
                self._active_batches.add(bid)

    def free_disk_gb(self) -> float:
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(self._tmp_dir)
        return round(usage.free / (1024**3), 2)

    async def inflight_preprocess_count(self) -> int:
        total = 0
        finished: List[str] = []
        for batch_id in list(self._active_batches):
            try:
                batch = await self._cinevec.get_batch(batch_id)
            except CineVecAPIError as exc:
                if exc.status_code == 404:
                    finished.append(batch_id)
                continue
            except Exception:
                continue
            counts = batch.get("counts") or {}
            in_prep = int(counts.get("queued", 0)) + int(counts.get("preprocess", 0))
            total += in_prep
            if in_prep == 0:
                finished.append(batch_id)
        for bid in finished:
            self._active_batches.discard(bid)
        return total

    async def snapshot(self) -> Dict[str, Any]:
        free_gb = self.free_disk_gb()
        inflight = await self.inflight_preprocess_count()
        disk_ok = free_gb >= self._disk_min_free_gb
        queue_ok = inflight < self._max_preprocess_inflight
        reason = None
        if not disk_ok:
            reason = f"low_disk ({free_gb} GB < {self._disk_min_free_gb} GB)"
        elif not queue_ok:
            reason = f"preprocess_saturated ({inflight} >= {self._max_preprocess_inflight})"
        return {
            "ready": disk_ok and queue_ok,
            "disk_free_gb": free_gb,
            "disk_min_free_gb": self._disk_min_free_gb,
            "inflight_preprocess": inflight,
            "max_preprocess_inflight": self._max_preprocess_inflight,
            "tracked_batches": len(self._active_batches),
            "blocked_reason": reason,
        }

    async def wait_until_ready(self, stop_event: Optional[asyncio.Event] = None) -> None:
        while True:
            snap = await self.snapshot()
            if snap["ready"]:
                return
            if stop_event is not None and stop_event.is_set():
                return
            await asyncio.sleep(self._poll_interval)
