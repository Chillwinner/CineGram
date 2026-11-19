from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List

import pytest

from tg_plugin.backpressure import BackpressureController
from tg_plugin.config import PluginSettings
from tg_plugin.ingest_pipeline import (
    IngestPipeline,
    build_ref_id,
    build_telegram_link,
    extract_hashtags,
)
from tg_plugin.media_grouper import MediaGroupBuffer
from tg_plugin.models import TelegramMediaEnvelope
from tg_plugin.state_store import StateStore


class FakeCineVecForIngest:
    def __init__(self, *, inflight: int = 0) -> None:
        self.imports: List[List[Dict[str, Any]]] = []
        self.inflight = inflight

    async def find_asset_by_ref_id(self, ref_id: str):
        return None

    async def import_media(self, items: List[Dict[str, Any]], *, batch_id=None) -> Dict[str, Any]:
        # Verify open files exist on disk at upload time
        for it in items:
            assert Path(it["file_path"]).is_file()
        self.imports.append(items)
        assets = []
        for idx, it in enumerate(items):
            assets.append(
                {
                    "id": f"asset-{len(self.imports)}-{idx}",
                    "ref": it["meta"]["ref"],
                    "lifecycle": "active",
                }
            )
        return {"batch_id": f"batch-{len(self.imports)}", "assets": assets, "jobs": []}

    async def get_batch(self, batch_id: str) -> Dict[str, Any]:
        return {
            "batch_id": batch_id,
            "items": [],
            "counts": {"queued": self.inflight, "preprocess": 0, "searchable": 0},
        }


def test_telegram_link_ref_id_and_hashtags():
    assert build_telegram_link(-1001234567890, 42, "my_channel") == "https://t.me/my_channel/42"
    assert build_telegram_link(-1001234567890, 42, None) == "https://t.me/c/1234567890/42"
    assert build_telegram_link(98765, 7, None) == "tg://openmessage?chat_id=98765&message_id=7"
    ref_id = build_ref_id(-1001234567890, 42)
    assert "/" not in ref_id
    assert extract_hashtags("Nice #running shot #室外运动 #running") == ["running", "室外运动"]


@pytest.mark.asyncio
async def test_media_group_buffer_debounces_album():
    flushed: List[List[int]] = []

    async def on_flush(items: List[TelegramMediaEnvelope]) -> None:
        flushed.append([x.message_id for x in items])

    buf = MediaGroupBuffer(0.08, on_flush)
    for mid in (10, 11, 12):
        await buf.push(
            TelegramMediaEnvelope(
                chat_id=-1001,
                message_id=mid,
                kind="image",
                filename=f"p{mid}.jpg",
                media_group_id="album-1",
            )
        )
    await asyncio.sleep(0.2)
    assert flushed == [[10, 11, 12]]


@pytest.mark.asyncio
async def test_ingest_dedup_temp_cleanup_and_backpressure(tmp_path: Path):
    settings = PluginSettings(
        plugin_data_dir=tmp_path / "data",
        disk_min_free_gb=0.001,
        max_preprocess_inflight=2,
        upload_batch_size=5,
    )
    store = StateStore(settings.db_path)
    await store.init()
    cinevec = FakeCineVecForIngest(inflight=0)
    bp = BackpressureController(
        settings.tmp_dir,
        cinevec,  # type: ignore[arg-type]
        disk_min_free_gb=settings.disk_min_free_gb,
        max_preprocess_inflight=settings.max_preprocess_inflight,
        poll_interval_seconds=0.05,
    )
    pipeline = IngestPipeline(settings, store, cinevec, bp)  # type: ignore[arg-type]

    async def make_dl(content: bytes):
        async def _dl(dest: Path) -> Path:
            dest.write_bytes(content)
            return dest

        return _dl

    try:
        src = await store.create_source({"chat_ref": "@action_clips", "chat_id": -100555, "default_tags": ["base"]})
        env1 = TelegramMediaEnvelope(
            chat_id=-100555,
            message_id=101,
            kind="video",
            filename="run.mp4",
            mime_type="video/mp4",
            file_size=100,
            file_unique_id="uid-1",
            file_id="fid-1",
            chat_username="action_clips",
            chat_title="Action Clips",
            caption="Sprint #running",
            source_id=src["id"],
            store_original=False,
            default_tags=["base"],
            download_fn=await make_dl(b"video-bytes"),
        )
        # Duplicate file_unique_id in a different message must be skipped
        env_dup = TelegramMediaEnvelope(
            chat_id=-100555,
            message_id=102,
            kind="video",
            filename="run_copy.mp4",
            mime_type="video/mp4",
            file_size=100,
            file_unique_id="uid-1",
            file_id="fid-2",
            chat_username="action_clips",
            source_id=src["id"],
            download_fn=await make_dl(b"video-bytes"),
        )
        r1 = await pipeline.ingest_batch([env1])
        assert len(r1["uploaded"]) == 1
        assert len(cinevec.imports) == 1
        uploaded_meta = cinevec.imports[0][0]["meta"]
        assert uploaded_meta["store_original"] is False
        assert uploaded_meta["source_url"] == "https://t.me/action_clips/101"
        assert set(uploaded_meta["tags"]) == {"base", "running", "tg:Action Clips"}
        # Temp files must be completely cleaned up after ingest
        assert list(settings.tmp_dir.rglob("*")) == []

        r2 = await pipeline.ingest_batch([env_dup])
        assert len(r2["uploaded"]) == 0
        assert r2["skipped"][0]["reason"] == "duplicate_file_unique_id"

        # Backpressure: saturated preprocess blocks until inflight drops below max_preprocess_inflight
        cinevec.inflight = 2
        snap_blocked = await bp.snapshot()
        assert snap_blocked["ready"] is False
        assert "preprocess_saturated" in str(snap_blocked["blocked_reason"])

        cinevec.inflight = 1
        snap_ok = await bp.snapshot()
        assert snap_ok["ready"] is True

        # Low disk check
        bp_low = BackpressureController(settings.tmp_dir, cinevec, disk_min_free_gb=9999999.0)  # type: ignore[arg-type]
        snap_disk = await bp_low.snapshot()
        assert snap_disk["ready"] is False
        assert "low_disk" in str(snap_disk["blocked_reason"])

        # Media group caption & hashtag inheritance across sibling files
        env_a1 = TelegramMediaEnvelope(
            chat_id=-100555,
            message_id=201,
            kind="image",
            filename="album1.jpg",
            file_size=50,
            file_unique_id="uid-a1",
            media_group_id="grp-99",
            caption="海边冲浪集锦 #冲浪 #夏日",
            source_id=src["id"],
            download_fn=await make_dl(b"img-1"),
        )
        env_a2 = TelegramMediaEnvelope(
            chat_id=-100555,
            message_id=202,
            kind="video",
            filename="album2.mp4",
            file_size=80,
            file_unique_id="uid-a2",
            media_group_id="grp-99",
            caption="",
            source_id=src["id"],
            download_fn=await make_dl(b"vid-2"),
        )
        r_grp = await pipeline.ingest_batch([env_a1, env_a2])
        assert len(r_grp["uploaded"]) == 2
        batch_metas = [x["meta"] for x in cinevec.imports[-1]]
        assert batch_metas[0]["description"] == "海边冲浪集锦 #冲浪 #夏日"
        assert batch_metas[1]["description"] == "海边冲浪集锦 #冲浪 #夏日"
        assert {"冲浪", "夏日"}.issubset(set(batch_metas[1]["tags"]))
    finally:
        await store.close()
