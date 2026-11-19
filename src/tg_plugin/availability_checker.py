from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from tg_plugin.cinevec_client import CineVecClient
from tg_plugin.config import PluginSettings
from tg_plugin.ingest_pipeline import build_telegram_link
from tg_plugin.models import TelegramInspector
from tg_plugin.state_store import StateStore, utc_iso


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class AvailabilityChecker:
    """Periodically verifies that monitored Telegram chats and messages remain accessible."""

    def __init__(
        self,
        settings: PluginSettings,
        store: StateStore,
        cinevec: CineVecClient,
        inspector: TelegramInspector,
    ) -> None:
        self._settings = settings
        self._store = store
        self._cinevec = cinevec
        self._inspector = inspector

    async def run_once(self, source_id: Optional[int] = None) -> Dict[str, Any]:
        started_at = utc_iso()
        if source_id is not None:
            src = await self._store.get_source(source_id)
            sources = [src] if src is not None else []
        else:
            sources = await self._store.list_sources(enabled_only=True)

        sources_checked = 0
        messages_checked = 0
        urls_updated = 0
        marked_invalid = 0
        restored_active = 0
        auto_excluded = 0
        alerts: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []

        for source in sources:
            sources_checked += 1
            sid = int(source["id"])
            try:
                probe = await self._inspector.probe_chat(source)
            except Exception as exc:
                errors.append({"source_id": sid, "stage": "probe_chat", "error": str(exc)})
                continue

            now_iso = utc_iso()
            items = await self._store.list_checkable_items_for_source(sid)

            if not probe.accessible:
                alerts.append(
                    {
                        "type": "chat_inaccessible",
                        "source_id": sid,
                        "chat_ref": source.get("chat_ref"),
                        "reason": probe.reason or "inaccessible",
                    }
                )
                for item in items:
                    if item["lifecycle"] != "source_invalid":
                        await self._cinevec.patch_asset(item["asset_id"], lifecycle="source_invalid")
                        await self._store.update_synced_item(
                            item["id"],
                            lifecycle="source_invalid",
                            unavailable_since=item.get("unavailable_since") or now_iso,
                            error_message=probe.reason or "chat_inaccessible",
                        )
                        marked_invalid += 1
                if self._settings.auto_exclude_after_days > 0:
                    auto_excluded += await self._apply_auto_exclude(sid)
                await self._store.update_source(sid, last_checked_at=now_iso)
                continue

            resolved_chat_id = probe.chat_id or source.get("chat_id")
            old_username = (source.get("chat_username") or "").strip() or None
            new_username = (probe.username or "").strip() or None
            source_updates: Dict[str, Any] = {"last_checked_at": now_iso}
            if resolved_chat_id and resolved_chat_id != source.get("chat_id"):
                source_updates["chat_id"] = resolved_chat_id
            if probe.title and probe.title != source.get("chat_title"):
                source_updates["chat_title"] = probe.title
            if probe.chat_type and probe.chat_type != source.get("chat_type"):
                source_updates["chat_type"] = probe.chat_type
            if new_username != old_username:
                source_updates["chat_username"] = new_username
                if old_username and not new_username:
                    alerts.append(
                        {
                            "type": "public_channel_became_private",
                            "source_id": sid,
                            "chat_id": resolved_chat_id,
                            "old_username": old_username,
                        }
                    )
                elif new_username and old_username != new_username:
                    alerts.append(
                        {
                            "type": "chat_username_changed",
                            "source_id": sid,
                            "chat_id": resolved_chat_id,
                            "old_username": old_username,
                            "new_username": new_username,
                        }
                    )
            await self._store.update_source(sid, **source_updates)

            if new_username != old_username and resolved_chat_id is not None:
                for item in items:
                    new_url = build_telegram_link(int(resolved_chat_id), int(item["message_id"]), new_username)
                    if new_url != item["source_url"]:
                        try:
                            await self._cinevec.patch_asset(item["asset_id"], source_url=new_url)
                            await self._store.update_synced_item(item["id"], source_url=new_url)
                            urls_updated += 1
                        except Exception as exc:
                            errors.append({"item_id": item["id"], "stage": "patch_url", "error": str(exc)})

            if resolved_chat_id is not None and items:
                for idx in range(0, len(items), 100):
                    chunk = items[idx : idx + 100]
                    msg_ids = [int(it["message_id"]) for it in chunk]
                    try:
                        msg_probes = await self._inspector.probe_messages(int(resolved_chat_id), msg_ids)
                    except Exception as exc:
                        errors.append({"source_id": sid, "stage": "probe_messages", "error": str(exc)})
                        break
                    messages_checked += len(chunk)
                    for item in chunk:
                        mprobe = msg_probes.get(int(item["message_id"]))
                        if mprobe is None:
                            continue
                        if not mprobe.exists:
                            if item["lifecycle"] != "source_invalid":
                                try:
                                    await self._cinevec.patch_asset(item["asset_id"], lifecycle="source_invalid")
                                    await self._store.update_synced_item(
                                        item["id"],
                                        lifecycle="source_invalid",
                                        unavailable_since=item.get("unavailable_since") or now_iso,
                                        error_message=mprobe.reason or "message_deleted",
                                    )
                                    marked_invalid += 1
                                except Exception as exc:
                                    errors.append({"item_id": item["id"], "stage": "mark_invalid", "error": str(exc)})
                        else:
                            if item["lifecycle"] != "active":
                                try:
                                    await self._cinevec.patch_asset(item["asset_id"], lifecycle="active")
                                    await self._store.update_synced_item(
                                        item["id"],
                                        lifecycle="active",
                                        unavailable_since=None,
                                        error_message=None,
                                    )
                                    restored_active += 1
                                except Exception as exc:
                                    errors.append({"item_id": item["id"], "stage": "restore_active", "error": str(exc)})

            if self._settings.auto_exclude_after_days > 0:
                auto_excluded += await self._apply_auto_exclude(sid)

        finished_at = utc_iso()
        report = {
            "started_at": started_at,
            "finished_at": finished_at,
            "sources_checked": sources_checked,
            "messages_checked": messages_checked,
            "urls_updated": urls_updated,
            "marked_invalid": marked_invalid,
            "restored_active": restored_active,
            "auto_excluded": auto_excluded,
            "details": {"alerts": alerts, "errors": errors},
        }
        return await self._store.record_check_log(report)

    async def _apply_auto_exclude(self, source_id: int) -> int:
        days = int(self._settings.auto_exclude_after_days)
        if days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        items = await self._store.list_checkable_items_for_source(source_id)
        excluded = 0
        for item in items:
            if item["lifecycle"] != "source_invalid":
                continue
            since = _parse_iso(item.get("unavailable_since"))
            if since is None or since > cutoff:
                continue
            try:
                await self._cinevec.exclude_asset(item["asset_id"])
                await self._store.update_synced_item(
                    item["id"],
                    status="excluded",
                    lifecycle="excluded",
                )
                excluded += 1
            except Exception:
                continue
        return excluded
