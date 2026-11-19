from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from tg_plugin.availability_checker import AvailabilityChecker
from tg_plugin.config import PluginSettings
from tg_plugin.models import ChatProbeResult, MessageProbeResult
from tg_plugin.state_store import StateStore


class FakeInspector:
    def __init__(self) -> None:
        self.chat_probes: Dict[int, ChatProbeResult] = {}
        self.msg_probes: Dict[int, Dict[int, MessageProbeResult]] = {}

    async def probe_chat(self, source: Dict[str, Any]) -> ChatProbeResult:
        return self.chat_probes[int(source["id"])]

    async def probe_messages(self, chat_id: int, message_ids: List[int]) -> Dict[int, MessageProbeResult]:
        chat_map = self.msg_probes.get(chat_id, {})
        return {
            mid: chat_map.get(mid, MessageProbeResult(message_id=mid, exists=True))
            for mid in message_ids
        }


class FakeCineVecPatcher:
    def __init__(self) -> None:
        self.patches: List[Dict[str, Any]] = []
        self.excluded: List[str] = []

    async def patch_asset(self, asset_id: str, **kwargs: Any) -> Dict[str, Any]:
        self.patches.append({"asset_id": asset_id, **kwargs})
        return {"id": asset_id, **kwargs}

    async def exclude_asset(self, asset_id: str) -> Dict[str, Any]:
        self.excluded.append(asset_id)
        return {"asset": {"id": asset_id, "excluded": True}}


@pytest.mark.asyncio
async def test_availability_checker_all_scenarios(tmp_path: Path):
    settings = PluginSettings(plugin_data_dir=tmp_path / "data", auto_exclude_after_days=7)
    store = StateStore(settings.db_path)
    await store.init()
    cinevec = FakeCineVecPatcher()
    inspector = FakeInspector()
    checker = AvailabilityChecker(settings, store, cinevec, inspector)  # type: ignore[arg-type]

    try:
        # Source 1: public channel that renames, then turns private (still a member), and loses msg 102
        s1 = await store.create_source(
            {"chat_ref": "@old_chan", "chat_id": -100111, "chat_username": "old_chan", "chat_title": "Clips", "enabled": True}
        )
        i1 = await store.upsert_synced_item(
            {
                "source_id": s1["id"],
                "chat_id": -100111,
                "message_id": 101,
                "ref_id": "tg_-100111_101",
                "asset_id": "a-101",
                "source_url": "https://t.me/old_chan/101",
                "kind": "video",
                "status": "uploaded",
                "lifecycle": "active",
            }
        )
        i2 = await store.upsert_synced_item(
            {
                "source_id": s1["id"],
                "chat_id": -100111,
                "message_id": 102,
                "ref_id": "tg_-100111_102",
                "asset_id": "a-102",
                "source_url": "https://t.me/old_chan/102",
                "kind": "image",
                "status": "uploaded",
                "lifecycle": "active",
            }
        )

        # Source 2: channel where bot/account gets kicked (inaccessible)
        s2 = await store.create_source(
            {"chat_ref": "@gone_chan", "chat_id": -100222, "chat_username": "gone_chan", "enabled": True}
        )
        i3 = await store.upsert_synced_item(
            {
                "source_id": s2["id"],
                "chat_id": -100222,
                "message_id": 201,
                "ref_id": "tg_-100222_201",
                "asset_id": "a-201",
                "source_url": "https://t.me/gone_chan/201",
                "kind": "video",
                "status": "uploaded",
                "lifecycle": "active",
            }
        )

        # Run 1: Source 1 renames to new_chan (all msgs exist); Source 2 inaccessible
        inspector.chat_probes[s1["id"]] = ChatProbeResult(
            accessible=True, chat_id=-100111, username="new_chan", title="Clips"
        )
        inspector.chat_probes[s2["id"]] = ChatProbeResult(accessible=False, reason="ChannelPrivateError")

        log1 = await checker.run_once()
        assert log1["urls_updated"] == 2
        assert log1["marked_invalid"] == 1
        assert (await store.get_synced_item(i1["id"]))["source_url"] == "https://t.me/new_chan/101"
        assert (await store.get_synced_item(i3["id"]))["lifecycle"] == "source_invalid"

        # Run 2: Source 1 turns private (username=None) & msg 102 deleted; Source 2 recovered!
        inspector.chat_probes[s1["id"]] = ChatProbeResult(
            accessible=True, chat_id=-100111, username=None, title="Private Clips"
        )
        inspector.msg_probes[-100111] = {102: MessageProbeResult(message_id=102, exists=False, reason="deleted")}
        inspector.chat_probes[s2["id"]] = ChatProbeResult(
            accessible=True, chat_id=-100222, username="gone_chan", title="Gone"
        )

        log2 = await checker.run_once()
        assert log2["urls_updated"] == 2  # switched to t.me/c/111/101 and /102
        assert log2["marked_invalid"] == 1  # i2 deleted
        assert log2["restored_active"] == 1  # i3 restored
        assert any(a["type"] == "public_channel_became_private" for a in log2["details"]["alerts"])
        assert (await store.get_synced_item(i1["id"]))["source_url"] == "https://t.me/c/111/101"
        assert (await store.get_synced_item(i2["id"]))["lifecycle"] == "source_invalid"
        assert (await store.get_synced_item(i3["id"]))["lifecycle"] == "active"

        # Run 3: Simulate i2 unavailable for 10 days -> auto_exclude_after_days=7 triggers exclude_asset
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        await store.update_synced_item(i2["id"], unavailable_since=old_ts)
        log3 = await checker.run_once(source_id=s1["id"])
        assert log3["auto_excluded"] == 1
        assert cinevec.excluded == ["a-102"]
        assert (await store.get_synced_item(i2["id"]))["lifecycle"] == "excluded"
    finally:
        await store.close()
