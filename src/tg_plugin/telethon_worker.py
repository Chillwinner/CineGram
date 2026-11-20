from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from telethon import TelegramClient, events, utils
from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.types import DocumentAttributeVideo, MessageEmpty

from tg_plugin.cinevec_client import CineVecAPIError, CineVecClient
from tg_plugin.config import PluginSettings
from tg_plugin.ingest_pipeline import (
    IngestPipeline,
    build_ref_id,
    build_telegram_link,
    matches_filter_rules,
    propagate_media_group_captions,
)
from tg_plugin.media_grouper import MediaGroupBuffer
from tg_plugin.models import (
    ChatProbeResult,
    MessageProbeResult,
    TelegramMediaEnvelope,
    format_search_hit_caption,
    is_face_search_caption,
    is_photo_search_caption,
    parse_face_search_index,
    parse_text_search_query,
)
from tg_plugin.state_store import StateStore

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


def _infer_media_meta(msg: Any) -> Optional[Dict[str, Any]]:
    if msg is None or getattr(msg, "media", None) is None:
        return None
    if getattr(msg, "photo", None) is not None:
        photo = msg.photo
        return {
            "kind": "image",
            "filename": f"photo_{msg.id}.jpg",
            "mime_type": "image/jpeg",
            "file_size": 0,
            "file_unique_id": f"tg_photo_{photo.id}",
            "file_id": "",
            "duration": None,
            "has_thumb": False,
        }
    doc = getattr(msg, "document", None)
    if doc is None:
        return None
    mime = (getattr(doc, "mime_type", None) or "").lower()
    fname = ""
    duration: Optional[int] = None
    for attr in getattr(doc, "attributes", None) or []:
        if hasattr(attr, "file_name") and getattr(attr, "file_name", None):
            fname = str(attr.file_name)
        if isinstance(attr, DocumentAttributeVideo):
            duration = int(getattr(attr, "duration", 0) or 0) or None
    ext = Path(fname).suffix.lower() if fname else ""
    if mime.startswith("image/") or ext in _IMAGE_EXTS:
        kind = "image"
        default_name = f"image_{msg.id}{ext or '.jpg'}"
    elif mime.startswith("video/") or ext in _VIDEO_EXTS:
        kind = "video"
        default_name = f"video_{msg.id}{ext or '.mp4'}"
    else:
        return None
    return {
        "kind": kind,
        "filename": fname or default_name,
        "mime_type": mime or ("image/jpeg" if kind == "image" else "video/mp4"),
        "file_size": int(getattr(doc, "size", 0) or 0),
        "file_unique_id": f"tg_doc_{doc.id}",
        "file_id": "",
        "duration": duration,
        "has_thumb": bool(getattr(doc, "thumbs", None)),
    }


class TelethonWorker:
    def __init__(
        self,
        settings: PluginSettings,
        store: StateStore,
        pipeline: IngestPipeline,
        cinevec: Optional[CineVecClient] = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._pipeline = pipeline
        self._cinevec = cinevec
        self._client: Optional[TelegramClient] = None
        self._stop = asyncio.Event()
        self._connected = False
        self._authorized = False
        self._backfill_tasks: Dict[int, asyncio.Task] = {}
        self._grouper = MediaGroupBuffer(
            settings.media_group_debounce_seconds,
            self._handle_grouped_envelopes,
        )

    @property
    def status(self) -> Dict[str, Any]:
        return {
            "configured": self._settings.telethon_configured,
            "connected": self._connected,
            "authorized": self._authorized,
            "active_backfills": [sid for sid, t in self._backfill_tasks.items() if not t.done()],
        }

    async def start(self) -> None:
        self._stop.clear()
        if not self._settings.telethon_configured:
            return
        self._settings.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self._client = TelegramClient(
            str(self._settings.session_path),
            int(self._settings.tg_api_id),
            self._settings.tg_api_hash,
        )
        await self._client.connect()
        self._connected = self._client.is_connected()
        self._authorized = await self._client.is_user_authorized()
        if not self._authorized:
            return
        self._client.add_event_handler(self._on_new_message, events.NewMessage())

    async def stop(self) -> None:
        self._stop.set()
        for task in list(self._backfill_tasks.values()):
            task.cancel()
        self._backfill_tasks.clear()
        await self._grouper.flush_all()
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
        self._connected = False
        self._authorized = False

    def update_settings(self, settings: PluginSettings) -> None:
        self._settings = settings

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def _handle_grouped_envelopes(self, envelopes: List[TelegramMediaEnvelope]) -> None:
        propagate_media_group_captions(envelopes)
        if envelopes and envelopes[0].source_id is not None:
            src = await self._store.get_source(int(envelopes[0].source_id))
            rules = (src or {}).get("filter_rules") if src else None
            if rules:
                envelopes = [e for e in envelopes if matches_filter_rules(e, rules)[0]]
        if envelopes:
            await self._pipeline.ingest_batch(envelopes, stop_event=self._stop)

    async def _apply_group_captions(self, entity: Any, envs: List[TelegramMediaEnvelope]) -> None:
        propagate_media_group_captions(envs)
        missing = [e for e in envs if e.media_group_id and not (e.caption or "").strip()]
        if not missing or self._client is None:
            return
        probe_ids: List[int] = []
        seen_ids = set()
        for e in missing:
            for mid in range(max(1, e.message_id - 9), e.message_id + 10):
                if mid not in seen_ids:
                    seen_ids.add(mid)
                    probe_ids.append(mid)
        if not probe_ids:
            return
        try:
            nearby = await self._client.get_messages(entity, ids=probe_ids)
        except Exception:
            return
        if not isinstance(nearby, list):
            nearby = [nearby]
        cap_by_group: Dict[str, str] = {}
        for m in nearby:
            if m is None or isinstance(m, MessageEmpty):
                continue
            gid = getattr(m, "grouped_id", None)
            txt = (getattr(m, "message", None) or "").strip()
            if gid and txt:
                gkey = str(gid)
                if gkey not in cap_by_group:
                    cap_by_group[gkey] = txt
        for e in missing:
            gkey = str(e.media_group_id)
            if gkey in cap_by_group:
                e.caption = cap_by_group[gkey]

    def _to_envelope(self, msg: Any, source: Dict[str, Any], chat: Any) -> Optional[TelegramMediaEnvelope]:
        meta = _infer_media_meta(msg)
        if meta is None or self._client is None:
            return None
        client = self._client
        chat_id = int(source.get("chat_id") or utils.get_peer_id(chat))
        chat_username = getattr(chat, "username", None) or source.get("chat_username")
        chat_title = getattr(chat, "title", None) or source.get("chat_title")

        async def _download(dest: Path) -> Path:
            out = await client.download_media(msg, file=str(dest))
            if out is None:
                raise RuntimeError("Telethon download_media returned None")
            return Path(out)

        async def _cover() -> Optional[bytes]:
            if not meta["has_thumb"]:
                return None
            buf = BytesIO()
            res = await client.download_media(msg, thumb=-1, file=buf)
            if res is None:
                return None
            val = buf.getvalue()
            return val or None

        async def _backup(target_chat_id: int) -> Optional[str]:
            sent = await client.send_message(target_chat_id, message=msg.message or "", file=msg.media)
            if sent is None:
                return None
            return build_telegram_link(target_chat_id, int(sent.id), None)

        return TelegramMediaEnvelope(
            chat_id=chat_id,
            message_id=int(msg.id),
            kind=meta["kind"],
            filename=meta["filename"],
            mime_type=meta["mime_type"],
            file_size=meta["file_size"],
            file_unique_id=meta["file_unique_id"],
            file_id=meta["file_id"],
            media_group_id=str(msg.grouped_id) if getattr(msg, "grouped_id", None) else None,
            chat_username=chat_username,
            chat_title=chat_title,
            caption=getattr(msg, "message", None) or "",
            duration=meta["duration"],
            source_id=int(source["id"]),
            store_original=bool(source.get("store_original")),
            backup_chat_id=source.get("backup_chat_id"),
            default_tags=list(source.get("default_tags") or []),
            download_fn=_download,
            cover_download_fn=_cover,
            backup_copy_fn=_backup,
        )

    def _is_search_allowed(self, msg: Any) -> bool:
        if getattr(msg, "out", False):
            return True
        allowed = self._settings.tg_allowed_user_ids
        if not allowed:
            return False
        sender_id = getattr(msg, "sender_id", None)
        return sender_id is not None and int(sender_id) in allowed

    async def _reply_search_results(self, event: Any, res: Dict[str, Any]) -> None:
        items = (res.get("items") or [])[:5]
        if not items:
            await event.reply("没有找到匹配的结果。")
            return
        for item in items:
            caption = format_search_hit_caption(item, include_source_url=True)
            previews = item.get("previews") or []
            first_prev = previews[0] if previews else {}
            locator = first_prev.get("locator") or item.get("video_cover_locator")
            if locator and self._cinevec is not None:
                try:
                    img_bytes = await self._cinevec.fetch_asset_bytes(locator)
                    buf = BytesIO(img_bytes)
                    buf.name = "preview.jpg"
                    await event.reply(caption, file=buf)
                    continue
                except Exception:
                    pass
            await event.reply(caption)

    async def _run_telethon_face_search(self, event: Any, target_msg: Any, raw_text: str) -> None:
        buf = BytesIO()
        await self._client.download_media(target_msg, file=buf)
        data = buf.getvalue()
        if not data:
            return
        face_index = parse_face_search_index(raw_text)
        try:
            res = await self._cinevec.search_face(data, face_index=face_index, top_n=5)
        except CineVecAPIError as exc:
            await event.reply(f"人脸搜索不可用：{exc.message}")
            return
        await self._reply_search_results(event, res)
        detected = res.get("detected_faces") or []
        if len(detected) > 1:
            selected = int(res.get("selected_face_index") or 0) + 1
            await event.reply(
                f"检测到 {len(detected)} 张人脸，当前按第 {selected} 张匹配；回复该图发送 `/f 2` 可切换指定人脸。"
            )

    async def _maybe_handle_search_message(self, event: Any) -> bool:
        if self._cinevec is None or self._client is None:
            return False
        msg = getattr(event, "message", None)
        if msg is None or not self._is_search_allowed(msg):
            return False

        raw_text = getattr(msg, "message", None) or getattr(msg, "raw_text", None) or ""
        is_image_msg = getattr(msg, "photo", None) is not None or (_infer_media_meta(msg) or {}).get("kind") == "image"

        # 0) Photo/image sent with `/f` caption or reply with `/f` -> face search
        if is_image_msg and is_face_search_caption(raw_text):
            await self._run_telethon_face_search(event, msg, raw_text)
            return True

        if getattr(msg, "is_reply", False) and is_face_search_caption(raw_text):
            replied = await event.get_reply_message()
            replied_meta = _infer_media_meta(replied) if replied is not None else None
            if replied is not None and (getattr(replied, "photo", None) is not None or (replied_meta or {}).get("kind") == "image"):
                await self._run_telethon_face_search(event, replied, raw_text)
                return True

        # 1) Photo sent with `/s` caption -> image search
        if getattr(msg, "photo", None) is not None and is_photo_search_caption(raw_text):
            buf = BytesIO()
            await self._client.download_media(msg, file=buf)
            data = buf.getvalue()
            if data:
                res = await self._cinevec.search_image(data, top_n=5)
                await self._reply_search_results(event, res)
            return True

        # 2) Reply to a photo with `/s` -> image search
        if getattr(msg, "is_reply", False) and is_photo_search_caption(raw_text):
            replied = await event.get_reply_message()
            replied_meta = _infer_media_meta(replied) if replied is not None else None
            if replied is not None and (getattr(replied, "photo", None) is not None or (replied_meta or {}).get("kind") == "image"):
                buf = BytesIO()
                await self._client.download_media(replied, file=buf)
                data = buf.getvalue()
                if data:
                    res = await self._cinevec.search_image(data, top_n=5)
                    await self._reply_search_results(event, res)
                return True

        # 3) Text search requires `/s <query>` with whitespace after `/s`
        query = parse_text_search_query(raw_text)
        if query:
            res = await self._cinevec.search_text(query, top_n=5)
            await self._reply_search_results(event, res)
            return True

        return False

    async def _on_new_message(self, event: Any) -> None:
        if not self._authorized or self._client is None:
            return
        if await self._maybe_handle_search_message(event):
            return
        chat_id = int(event.chat_id) if event.chat_id is not None else None
        if chat_id is None:
            return
        source = await self._store.find_source_by_chat(chat_id)
        if source is None or not source.get("enabled"):
            return
        chat = await event.get_chat()
        env = self._to_envelope(event.message, source, chat)
        if env is None:
            return
        # Defer rule filtering to _handle_grouped_envelopes for albums so siblings inherit caption first.
        if not env.media_group_id:
            ok, _ = matches_filter_rules(env, source.get("filter_rules"))
            if not ok:
                return
        await self._grouper.push(env)

    def _resolve_target(self, source: Dict[str, Any]) -> Any:
        if source.get("chat_id") is not None:
            return int(source["chat_id"])
        ref = str(source.get("chat_ref") or "").strip()
        if ref.lstrip("-").isdigit():
            return int(ref)
        return ref

    async def resolve_source(self, source_id: int) -> Optional[Dict[str, Any]]:
        if not self._client or not self._authorized:
            return await self._store.get_source(source_id)
        source = await self._store.get_source(source_id)
        if source is None:
            return None
        probe = await self.probe_chat(source)
        if probe.accessible and probe.chat_id is not None:
            return await self._store.update_source(
                source_id,
                chat_id=probe.chat_id,
                chat_username=probe.username,
                chat_title=probe.title,
                chat_type=probe.chat_type,
            )
        return source

    async def probe_chat(self, source: Dict[str, Any]) -> ChatProbeResult:
        if self._client is None or not self._authorized:
            raise RuntimeError("Telethon client is not authorized")
        target = self._resolve_target(source)
        try:
            entity = await self._client.get_entity(target)
            # Touch history (limit=1) so kicked/private channels fail even when cached locally.
            await self._client.get_messages(entity, limit=1)
        except (ChannelPrivateError, ChatAdminRequiredError, UsernameInvalidError, UsernameNotOccupiedError) as exc:
            return ChatProbeResult(accessible=False, reason=f"{type(exc).__name__}: {exc}")
        except ValueError as exc:
            return ChatProbeResult(accessible=False, reason=str(exc))

        peer_id = int(utils.get_peer_id(entity))
        username = getattr(entity, "username", None)
        title = getattr(entity, "title", None) or getattr(entity, "first_name", None)
        if getattr(entity, "broadcast", False):
            ctype = "channel"
        elif getattr(entity, "megagroup", False):
            ctype = "supergroup"
        else:
            ctype = type(entity).__name__.lower()
        return ChatProbeResult(
            accessible=True,
            chat_id=peer_id,
            username=username,
            title=title,
            chat_type=ctype,
        )

    async def probe_messages(self, chat_id: int, message_ids: List[int]) -> Dict[int, MessageProbeResult]:
        if self._client is None or not self._authorized:
            raise RuntimeError("Telethon client is not authorized")
        if not message_ids:
            return {}
        try:
            msgs = await self._client.get_messages(chat_id, ids=message_ids)
        except ChannelPrivateError as exc:
            return {mid: MessageProbeResult(message_id=mid, exists=False, reason=str(exc)) for mid in message_ids}
        if not isinstance(msgs, list):
            msgs = [msgs]
        out: Dict[int, MessageProbeResult] = {}
        for mid, msg in zip(message_ids, msgs):
            exists = msg is not None and not isinstance(msg, MessageEmpty)
            out[int(mid)] = MessageProbeResult(
                message_id=int(mid),
                exists=exists,
                reason=None if exists else "message_deleted",
            )
        return out

    def start_backfill(
        self,
        source_id: int,
        limit: Optional[int] = None,
        days: Optional[int] = None,
    ) -> bool:
        existing = self._backfill_tasks.get(source_id)
        if existing is not None and not existing.done():
            return False
        self._backfill_tasks[source_id] = asyncio.create_task(
            self.backfill_source(source_id, limit=limit, days=days)
        )
        return True

    async def backfill_source(
        self,
        source_id: int,
        *,
        limit: Optional[int] = None,
        days: Optional[int] = None,
    ) -> Dict[str, Any]:
        if self._client is None or not self._authorized:
            raise RuntimeError("Telethon client is not authorized")
        source = await self.resolve_source(source_id)
        if source is None:
            raise RuntimeError(f"Source {source_id} not found")
        target = self._resolve_target(source)
        entity = await self._client.get_entity(target)
        max_id = int(source["backfill_min_message_id"]) if source.get("backfill_min_message_id") else 0
        rules = source.get("filter_rules") or {}
        effective_days = days if days is not None else rules.get("backfill_days")
        cutoff_dt: Optional[datetime] = None
        if effective_days is not None and int(effective_days) > 0:
            cutoff_dt = datetime.now(timezone.utc) - timedelta(days=int(effective_days))

        processed = 0
        filtered_out = 0
        uploaded_total = 0
        buffer: List[TelegramMediaEnvelope] = []
        min_seen = max_id or None

        iter_kwargs: Dict[str, Any] = {}
        if limit is not None:
            iter_kwargs["limit"] = int(limit)
        if max_id > 0:
            iter_kwargs["max_id"] = max_id

        while not self._stop.is_set():
            try:
                async for msg in self._client.iter_messages(entity, **iter_kwargs):
                    if self._stop.is_set():
                        break
                    if cutoff_dt is not None and getattr(msg, "date", None) is not None:
                        msg_dt = msg.date if msg.date.tzinfo is not None else msg.date.replace(tzinfo=timezone.utc)
                        if msg_dt < cutoff_dt:
                            break
                    mid = int(msg.id)
                    if min_seen is None or mid < min_seen:
                        min_seen = mid
                    env = self._to_envelope(msg, source, entity)
                    if env is not None:
                        buffer.append(env)
                        processed += 1
                    if len(buffer) >= self._settings.upload_batch_size:
                        await self._apply_group_captions(entity, buffer)
                        kept = [e for e in buffer if matches_filter_rules(e, rules)[0]]
                        filtered_out += len(buffer) - len(kept)
                        if kept:
                            res = await self._pipeline.ingest_batch(kept, stop_event=self._stop)
                            uploaded_total += len(res.get("uploaded") or [])
                        buffer.clear()
                        await self._store.update_source(source_id, backfill_min_message_id=min_seen)
                break
            except FloodWaitError as exc:
                await asyncio.sleep(int(exc.seconds) + 1)
                if min_seen:
                    iter_kwargs["max_id"] = min_seen

        if buffer and not self._stop.is_set():
            await self._apply_group_captions(entity, buffer)
            kept = [e for e in buffer if matches_filter_rules(e, rules)[0]]
            filtered_out += len(buffer) - len(kept)
            if kept:
                res = await self._pipeline.ingest_batch(kept, stop_event=self._stop)
                uploaded_total += len(res.get("uploaded") or [])
            buffer.clear()

        if not self._stop.is_set():
            await self._store.update_source(
                source_id,
                backfill_min_message_id=min_seen,
                backfill_completed=(limit is None and cutoff_dt is None),
            )
        return {
            "source_id": source_id,
            "processed": processed,
            "filtered_out": filtered_out,
            "uploaded": uploaded_total,
        }

    async def browse_messages(
        self,
        source_id: int,
        *,
        limit: int = 30,
        offset_id: int = 0,
        apply_rules: bool = True,
    ) -> Dict[str, Any]:
        if self._client is None or not self._authorized:
            raise RuntimeError("Telethon client is not authorized")
        source = await self.resolve_source(source_id)
        if source is None:
            raise RuntimeError(f"Source {source_id} not found")
        target = self._resolve_target(source)
        entity = await self._client.get_entity(target)
        rules = (source.get("filter_rules") or {}) if apply_rules else {}
        chat_id = int(source.get("chat_id") or utils.get_peer_id(entity))
        username = getattr(entity, "username", None) or source.get("chat_username")

        iter_kwargs: Dict[str, Any] = {"limit": max(1, min(100, int(limit) * 3))}
        if offset_id > 0:
            iter_kwargs["offset_id"] = int(offset_id)

        candidates: List[Tuple[Any, TelegramMediaEnvelope]] = []
        last_seen_id: Optional[int] = None
        async for msg in self._client.iter_messages(entity, **iter_kwargs):
            mid = int(msg.id)
            last_seen_id = mid
            env = self._to_envelope(msg, source, entity)
            if env is not None:
                candidates.append((msg, env))
            if len(candidates) >= int(limit) * 2:
                break

        await self._apply_group_captions(entity, [e for _, e in candidates])

        items: List[Dict[str, Any]] = []
        for msg, env in candidates:
            mid = int(msg.id)
            matched, filter_reason = matches_filter_rules(env, rules)
            if apply_rules and not matched:
                continue
            existing = await self._store.find_by_chat_msg(chat_id, mid)
            if existing is None and env.file_unique_id:
                existing = await self._store.find_by_file_unique_id(env.file_unique_id)
            meta = _infer_media_meta(msg) or {}
            has_thumb = bool(meta.get("has_thumb") or getattr(msg, "photo", None) is not None)
            items.append(
                {
                    "message_id": mid,
                    "ref_id": build_ref_id(chat_id, mid),
                    "date": msg.date.isoformat() if getattr(msg, "date", None) else None,
                    "kind": env.kind,
                    "filename": env.filename,
                    "file_size": env.file_size,
                    "duration": env.duration,
                    "caption": env.caption,
                    "source_url": build_telegram_link(chat_id, mid, username),
                    "already_synced": existing is not None and existing.get("status") != "failed",
                    "asset_id": existing.get("asset_id") if existing else None,
                    "matched_rules": matched,
                    "filter_reason": filter_reason,
                    "has_thumb": has_thumb,
                    "thumb_url": f"/sources/{source_id}/messages/{mid}/thumb" if has_thumb else None,
                }
            )
            if len(items) >= int(limit):
                break
        return {
            "source_id": source_id,
            "items": items,
            "next_offset_id": last_seen_id if last_seen_id else 0,
        }

    async def get_message_thumb(self, source_id: int, message_id: int) -> Optional[bytes]:
        if self._client is None or not self._authorized:
            raise RuntimeError("Telethon client is not authorized")
        source = await self._store.get_source(source_id)
        if source is None:
            return None
        target = self._resolve_target(source)
        entity = await self._client.get_entity(target)
        msg = await self._client.get_messages(entity, ids=int(message_id))
        if msg is None or isinstance(msg, MessageEmpty):
            return None
        buf = BytesIO()
        res = await self._client.download_media(msg, thumb=-1, file=buf)
        if res is None:
            return None
        val = buf.getvalue()
        return val or None

    async def import_selected_messages(self, source_id: int, message_ids: List[int]) -> Dict[str, Any]:
        if self._client is None or not self._authorized:
            raise RuntimeError("Telethon client is not authorized")
        source = await self.resolve_source(source_id)
        if source is None:
            raise RuntimeError(f"Source {source_id} not found")
        clean_ids = [int(mid) for mid in dict.fromkeys(message_ids) if int(mid) > 0]
        if not clean_ids:
            return {"accepted": 0, "uploaded": [], "skipped": [], "failed": [], "batch_ids": []}
        target = self._resolve_target(source)
        entity = await self._client.get_entity(target)
        msgs = await self._client.get_messages(entity, ids=clean_ids)
        if not isinstance(msgs, list):
            msgs = [msgs]
        envelopes: List[TelegramMediaEnvelope] = []
        for msg in msgs:
            if msg is None or isinstance(msg, MessageEmpty):
                continue
            env = self._to_envelope(msg, source, entity)
            if env is not None:
                envelopes.append(env)
        await self._apply_group_captions(entity, envelopes)
        return await self._pipeline.ingest_batch(envelopes, stop_event=self._stop)
