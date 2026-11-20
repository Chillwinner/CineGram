from __future__ import annotations

import asyncio
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tg_plugin.backpressure import BackpressureController
from tg_plugin.cinevec_client import CineVecClient
from tg_plugin.config import PluginSettings
from tg_plugin.models import TelegramMediaEnvelope
from tg_plugin.state_store import StateStore

_HASHTAG_RE = re.compile(r"#([\w\u4e00-\u9fff-]+)")


def build_telegram_link(
    chat_id: int,
    message_id: int,
    chat_username: Optional[str] = None,
    chat_type: Optional[str] = None,
) -> str:
    username = (chat_username or "").strip().lstrip("@")
    if username:
        return f"https://t.me/{username}/{message_id}"
    cid_str = str(chat_id)
    if cid_str.startswith("-100") and len(cid_str) > 4:
        return f"https://t.me/c/{cid_str[4:]}/{message_id}"
    if chat_type in ("personal_chat", "saved_messages", "bot_chat") and chat_id > 0:
        return f"tg://openmessage?user_id={chat_id}&message_id={message_id}"
    return f"tg://openmessage?chat_id={chat_id}&message_id={message_id}"


def build_ref_id(chat_id: int, message_id: int) -> str:
    # CineVec plugin_object_key rejects '/', so never include '/' in ref_id.
    return f"tg_{chat_id}_{message_id}"


def extract_hashtags(caption: str) -> List[str]:
    if not caption:
        return []
    seen = set()
    tags: List[str] = []
    for m in _HASHTAG_RE.finditer(caption):
        t = m.group(1).strip()
        if t and t not in seen:
            seen.add(t)
            tags.append(t)
    return tags


def _safe_filename(name: str, kind: str) -> str:
    cleaned = re.sub(r"[\\/\x00]+", "_", (name or "").strip())
    if cleaned:
        return cleaned
    return "media.jpg" if kind == "image" else "media.mp4"


def matches_filter_rules(
    env: TelegramMediaEnvelope,
    rules: Optional[Dict[str, Any]],
) -> Tuple[bool, Optional[str]]:
    if not rules:
        return True, None
    kinds = [str(k).strip().lower() for k in (rules.get("media_kinds") or []) if str(k).strip()]
    if kinds and env.kind.lower() not in kinds:
        return False, f"kind_excluded ({env.kind})"

    min_mb = rules.get("min_file_mb")
    if min_mb is not None and env.file_size > 0 and env.file_size < float(min_mb) * 1024 * 1024:
        return False, f"below_min_file_mb ({min_mb})"

    max_mb = rules.get("max_file_mb")
    if max_mb is not None and env.file_size > 0 and env.file_size > float(max_mb) * 1024 * 1024:
        return False, f"above_max_file_mb ({max_mb})"

    min_dur = rules.get("min_duration_sec")
    if min_dur is not None and env.kind == "video":
        if env.duration is None or int(env.duration) < int(min_dur):
            return False, f"below_min_duration_sec ({min_dur})"

    haystack = f"{env.caption or ''}\n{env.filename or ''}".lower()
    excludes = [str(w).strip().lower() for w in (rules.get("exclude_keywords") or []) if str(w).strip()]
    for kw in excludes:
        if kw in haystack:
            return False, f"matched_exclude_keyword ({kw})"

    includes = [str(w).strip().lower() for w in (rules.get("include_keywords") or []) if str(w).strip()]
    if includes and not any(kw in haystack for kw in includes):
        return False, "missing_include_keyword"

    return True, None


def propagate_media_group_captions(envelopes: List[TelegramMediaEnvelope]) -> None:
    """Propagates non-empty caption text across items sharing the same (chat_id, media_group_id)."""
    grouped: Dict[Tuple[int, str], List[TelegramMediaEnvelope]] = {}
    for env in envelopes:
        if env.media_group_id:
            grouped.setdefault((env.chat_id, str(env.media_group_id)), []).append(env)
    for items in grouped.values():
        texts: List[str] = []
        seen = set()
        for it in items:
            t = (it.caption or "").strip()
            if t and t not in seen:
                seen.add(t)
                texts.append(t)
        if not texts:
            continue
        merged = "\n".join(texts)
        for it in items:
            if not (it.caption or "").strip():
                it.caption = merged


class IngestPipeline:
    def __init__(
        self,
        settings: PluginSettings,
        store: StateStore,
        cinevec: CineVecClient,
        backpressure: BackpressureController,
    ) -> None:
        self._settings = settings
        self._store = store
        self._cinevec = cinevec
        self._backpressure = backpressure

    async def ingest_batch(
        self,
        envelopes: List[TelegramMediaEnvelope],
        *,
        stop_event: Optional[asyncio.Event] = None,
    ) -> Dict[str, Any]:
        propagate_media_group_captions(envelopes)
        accepted: List[TelegramMediaEnvelope] = []
        skipped: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []

        for env in envelopes:
            existing = await self._store.find_by_chat_msg(env.chat_id, env.message_id)
            if existing and existing.get("status") != "failed":
                skipped.append({"chat_id": env.chat_id, "message_id": env.message_id, "reason": "duplicate_msg"})
                continue
            if env.file_unique_id:
                dup = await self._store.find_by_file_unique_id(env.file_unique_id)
                if dup is not None:
                    skipped.append(
                        {
                            "chat_id": env.chat_id,
                            "message_id": env.message_id,
                            "reason": "duplicate_file_unique_id",
                            "existing_asset_id": dup.get("asset_id"),
                        }
                    )
                    continue
            ref_id = build_ref_id(env.chat_id, env.message_id)
            try:
                remote = await self._cinevec.find_asset_by_ref_id(ref_id)
            except Exception:
                remote = None
            if remote is not None:
                source_url = build_telegram_link(env.chat_id, env.message_id, env.chat_username, env.chat_type)
                await self._store.upsert_synced_item(
                    {
                        "source_id": env.source_id,
                        "chat_id": env.chat_id,
                        "message_id": env.message_id,
                        "media_group_id": env.media_group_id,
                        "file_unique_id": env.file_unique_id,
                        "file_id": env.file_id,
                        "ref_id": ref_id,
                        "asset_id": remote.get("id"),
                        "source_url": source_url,
                        "kind": env.kind,
                        "file_size": env.file_size,
                        "status": "uploaded",
                        "lifecycle": remote.get("lifecycle") or "active",
                    }
                )
                skipped.append(
                    {
                        "chat_id": env.chat_id,
                        "message_id": env.message_id,
                        "reason": "already_in_cinevec",
                        "asset_id": remote.get("id"),
                    }
                )
                continue

            max_bytes = self._settings.max_file_size_mb * 1024 * 1024
            if env.file_size and env.file_size > max_bytes:
                skipped.append(
                    {
                        "chat_id": env.chat_id,
                        "message_id": env.message_id,
                        "reason": f"exceeds_max_file_size_mb ({self._settings.max_file_size_mb})",
                    }
                )
                continue
            if env.download_fn is None:
                failed.append({"chat_id": env.chat_id, "message_id": env.message_id, "error": "missing download_fn"})
                continue
            accepted.append(env)

        uploaded_items: List[Dict[str, Any]] = []
        batch_ids: List[str] = []
        chunk_size = self._settings.upload_batch_size
        for start in range(0, len(accepted), chunk_size):
            chunk = accepted[start : start + chunk_size]
            res = await self._process_chunk(chunk, stop_event=stop_event)
            if res.get("batch_id"):
                batch_ids.append(res["batch_id"])
            uploaded_items.extend(res.get("uploaded") or [])
            failed.extend(res.get("failed") or [])

        return {
            "accepted": len(accepted),
            "uploaded": uploaded_items,
            "skipped": skipped,
            "failed": failed,
            "batch_ids": batch_ids,
        }

    async def _process_chunk(
        self,
        chunk: List[TelegramMediaEnvelope],
        *,
        stop_event: Optional[asyncio.Event] = None,
    ) -> Dict[str, Any]:
        await self._backpressure.wait_until_ready(stop_event)
        batch_dir = self._settings.tmp_dir / uuid.uuid4().hex
        batch_dir.mkdir(parents=True, exist_ok=True)
        uploaded: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        try:
            upload_payload: List[Dict[str, Any]] = []
            env_by_ref: Dict[str, TelegramMediaEnvelope] = {}
            url_by_ref: Dict[str, str] = {}

            for idx, env in enumerate(chunk):
                ref_id = build_ref_id(env.chat_id, env.message_id)
                safe_name = _safe_filename(env.filename, env.kind)
                dest = batch_dir / f"{idx}_{ref_id}_{safe_name}"
                try:
                    downloaded = await env.download_fn(dest)
                except Exception as exc:
                    source_url = build_telegram_link(env.chat_id, env.message_id, env.chat_username, env.chat_type)
                    await self._store.upsert_synced_item(
                        {
                            "source_id": env.source_id,
                            "chat_id": env.chat_id,
                            "message_id": env.message_id,
                            "media_group_id": env.media_group_id,
                            "file_unique_id": env.file_unique_id,
                            "file_id": env.file_id,
                            "ref_id": ref_id,
                            "source_url": source_url,
                            "kind": env.kind,
                            "file_size": env.file_size,
                            "status": "failed",
                            "lifecycle": "active",
                            "error_message": f"download_failed: {exc}",
                        }
                    )
                    failed.append({"chat_id": env.chat_id, "message_id": env.message_id, "error": str(exc)})
                    continue

                cover_bytes: Optional[bytes] = None
                if env.cover_download_fn is not None:
                    try:
                        cover_bytes = await env.cover_download_fn()
                    except Exception:
                        cover_bytes = None

                backup_url: Optional[str] = None
                if env.backup_chat_id and env.backup_copy_fn is not None:
                    try:
                        backup_url = await env.backup_copy_fn(env.backup_chat_id)
                    except Exception:
                        backup_url = None

                source_url = build_telegram_link(env.chat_id, env.message_id, env.chat_username, env.chat_type)
                tags = list(dict.fromkeys([*env.default_tags, *extract_hashtags(env.caption)]))
                if env.chat_title and f"tg:{env.chat_title}" not in tags:
                    tags.append(f"tg:{env.chat_title}")

                params: Dict[str, Any] = {
                    "chat_id": env.chat_id,
                    "message_id": env.message_id,
                    "chat_username": env.chat_username,
                    "chat_title": env.chat_title,
                    "file_id": env.file_id,
                    "file_unique_id": env.file_unique_id,
                    "media_group_id": env.media_group_id,
                    "mime_type": env.mime_type,
                    "duration": env.duration,
                }
                if backup_url:
                    params["backup_url"] = backup_url

                meta = {
                    "relative_path": f"{ref_id}_{safe_name}",
                    "display_name": safe_name,
                    "store_original": bool(env.store_original),
                    "vectorize": True,
                    "description": (env.caption or "").strip(),
                    "source_url": source_url,
                    "tags": tags,
                    "ref": {
                        "ref_id": ref_id,
                        "locator": source_url,
                        "params": params,
                    },
                }
                upload_payload.append(
                    {
                        "file_path": downloaded,
                        "filename": safe_name,
                        "mime_type": env.mime_type,
                        "meta": meta,
                        "cover_bytes": cover_bytes,
                    }
                )
                env_by_ref[ref_id] = env
                url_by_ref[ref_id] = source_url

            if not upload_payload:
                return {"batch_id": None, "uploaded": uploaded, "failed": failed}

            try:
                res = await self._cinevec.import_media(upload_payload)
            except Exception as exc:
                for ref_id, env in env_by_ref.items():
                    await self._store.upsert_synced_item(
                        {
                            "source_id": env.source_id,
                            "chat_id": env.chat_id,
                            "message_id": env.message_id,
                            "media_group_id": env.media_group_id,
                            "file_unique_id": env.file_unique_id,
                            "file_id": env.file_id,
                            "ref_id": ref_id,
                            "source_url": url_by_ref[ref_id],
                            "kind": env.kind,
                            "file_size": env.file_size,
                            "status": "failed",
                            "lifecycle": "active",
                            "error_message": f"upload_failed: {exc}",
                        }
                    )
                    failed.append({"chat_id": env.chat_id, "message_id": env.message_id, "error": str(exc)})
                return {"batch_id": None, "uploaded": uploaded, "failed": failed}

            batch_id = res.get("batch_id")
            self._backpressure.track_batch(batch_id)

            assets_by_ref: Dict[str, Dict[str, Any]] = {}
            for asset in res.get("assets") or []:
                ref = asset.get("ref") or {}
                rid = ref.get("ref_id")
                if rid:
                    assets_by_ref[str(rid)] = asset

            for ref_id, env in env_by_ref.items():
                asset = assets_by_ref.get(ref_id) or {}
                saved = await self._store.upsert_synced_item(
                    {
                        "source_id": env.source_id,
                        "chat_id": env.chat_id,
                        "message_id": env.message_id,
                        "media_group_id": env.media_group_id,
                        "file_unique_id": env.file_unique_id,
                        "file_id": env.file_id,
                        "ref_id": ref_id,
                        "asset_id": asset.get("id"),
                        "batch_id": batch_id,
                        "source_url": url_by_ref[ref_id],
                        "kind": env.kind,
                        "file_size": env.file_size,
                        "status": "uploaded",
                        "lifecycle": asset.get("lifecycle") or "active",
                        "error_message": None,
                    }
                )
                uploaded.append(saved)
                if env.source_id is not None:
                    src = await self._store.get_source(env.source_id)
                    if src and env.message_id > int(src.get("last_message_id") or 0):
                        await self._store.update_source(env.source_id, last_message_id=env.message_id)
            return {"batch_id": batch_id, "uploaded": uploaded, "failed": failed}
        finally:
            shutil.rmtree(batch_dir, ignore_errors=True)
