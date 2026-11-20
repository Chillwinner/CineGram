from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from tg_plugin.cinevec_client import CineVecClient
from tg_plugin.config import PluginSettings
from tg_plugin.ingest_pipeline import IngestPipeline
from tg_plugin.media_grouper import MediaGroupBuffer
from tg_plugin.models import (
    ChatProbeResult,
    MessageProbeResult,
    TelegramMediaEnvelope,
    format_search_hit_caption,
    is_photo_search_caption,
    parse_text_search_query,
)
from tg_plugin.state_store import StateStore


class TelegramBotRunner:
    """Optional Telegram Bot providing search, forwarded-media ingest, monitored channel sync, and chat probing."""

    def __init__(
        self,
        settings: PluginSettings,
        store: StateStore,
        cinevec: CineVecClient,
        pipeline: IngestPipeline,
    ) -> None:
        self._settings = settings
        self._store = store
        self._cinevec = cinevec
        self._pipeline = pipeline
        self._app: Optional[Application] = None
        self._running = False
        self._stop = asyncio.Event()
        self._poll_tasks: set = set()
        self._grouper = MediaGroupBuffer(
            settings.media_group_debounce_seconds,
            self._flush_bot_envelopes,
        )
        self._reply_targets: Dict[str, Any] = {}

    @property
    def status(self) -> Dict[str, Any]:
        return {
            "configured": self._settings.bot_configured,
            "running": self._running,
            "whitelist_enabled": bool(self._settings.tg_allowed_user_ids),
            "allowed_users": len(self._settings.tg_allowed_user_ids),
        }

    def update_settings(self, settings: PluginSettings) -> None:
        self._settings = settings

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    def _is_allowed(self, update: Update) -> bool:
        allowed = self._settings.tg_allowed_user_ids
        if not allowed:
            # Advisory default: when no whitelist is set, allow private chat interactions out of the box.
            chat = update.effective_chat
            if chat is not None and getattr(chat, "type", None) != "private":
                return False
            return True
        user = update.effective_user
        if user is None:
            return False
        return int(user.id) in allowed

    async def start(self) -> None:
        if not self._settings.bot_configured:
            return
        self._stop.clear()
        app = ApplicationBuilder().token(self._settings.tg_bot_token).build()
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("help", self._cmd_start))
        app.add_handler(CommandHandler("id", self._cmd_whoami))
        app.add_handler(CommandHandler("whoami", self._cmd_whoami))
        app.add_handler(CommandHandler("s", self._cmd_search_text))
        app.add_handler(CommandHandler("m", self._cmd_search_meta))
        app.add_handler(CallbackQueryHandler(self._on_callback))
        app.add_handler(
            MessageHandler(
                filters.UpdateType.CHANNEL_POST & (filters.PHOTO | filters.VIDEO | filters.Document.ALL),
                self._on_channel_media,
            )
        )
        app.add_handler(
            MessageHandler(
                filters.UpdateType.MESSAGE & (filters.PHOTO | filters.VIDEO | filters.Document.ALL),
                self._on_media,
            )
        )
        await app.initialize()
        await app.start()
        if app.updater is not None:
            await app.updater.start_polling(drop_pending_updates=True)
        self._app = app
        self._running = True

    async def stop(self) -> None:
        self._stop.set()
        for task in list(self._poll_tasks):
            task.cancel()
        self._poll_tasks.clear()
        await self._grouper.flush_all()
        if self._app is not None:
            if self._app.updater is not None and self._app.updater.running:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            self._app = None
        self._running = False

    def _resolve_target(self, source: Dict[str, Any]) -> Any:
        if source.get("chat_id") is not None:
            return int(source["chat_id"])
        ref = str(source.get("chat_ref") or "").strip()
        if ref.lstrip("-").isdigit():
            return int(ref)
        if ref and not ref.startswith("@"):
            return f"@{ref}"
        return ref

    async def probe_chat(self, source: Dict[str, Any]) -> ChatProbeResult:
        if self._app is None or not self._running:
            raise RuntimeError("Telegram Bot is not running")
        target = self._resolve_target(source)
        try:
            chat = await self._app.bot.get_chat(target)
        except TelegramError as exc:
            return ChatProbeResult(accessible=False, reason=str(exc))
        return ChatProbeResult(
            accessible=True,
            chat_id=int(chat.id),
            username=getattr(chat, "username", None),
            title=getattr(chat, "title", None) or getattr(chat, "first_name", None),
            chat_type=str(getattr(chat, "type", "") or "") or None,
        )

    async def probe_messages(self, chat_id: int, message_ids: List[int]) -> Dict[int, MessageProbeResult]:
        # Bot API cannot passively probe arbitrary message IDs without forwarding; skip msg-level deletion marks.
        return {}

    async def resolve_source(self, source_id: int) -> Optional[Dict[str, Any]]:
        if not self._running or self._app is None:
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

    async def _cmd_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update) or update.message is None:
            return
        await update.message.reply_text(
            "CineVec Telegram 插件已就绪：\n"
            "• `/s 关键词`（`/s` 后必须加空格）：语义搜图 / 搜视频帧\n"
            "• 发送带 `/s` 说明的图片（或回复图片发送 `/s`）：以图搜图\n"
            "• `/m 关键词`：按标签/描述/文件名搜索\n"
            "• 直接转发或发送图片/视频：入库到 CineVec 并实时跟踪进度\n"
            "• `/id`：查看当前 user_id 与 chat_id"
        )

    async def _cmd_whoami(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None:
            return
        uid = update.effective_user.id if update.effective_user else None
        cid = update.effective_chat.id if update.effective_chat else None
        await msg.reply_text(f"user_id: {uid}\nchat_id: {cid}")

    async def _cmd_search_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update) or update.message is None:
            return
        if update.message.reply_to_message and update.message.reply_to_message.photo:
            await self._search_by_photo(update, update.message.reply_to_message.photo[-1], ctx)
            return
        raw_text = getattr(update.message, "text", None)
        if raw_text is not None:
            query = parse_text_search_query(raw_text)
        else:
            query = " ".join(ctx.args).strip() or None
        if not query:
            await update.message.reply_text("用法：`/s 跑步 逆光`（`/s` 后必须加空格），或回复一张图片发送 `/s` 以图搜图。")
            return
        await self._reply_search_results(update, await self._cinevec.search_text(query, top_n=5))

    async def _cmd_search_meta(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update) or update.message is None:
            return
        query = " ".join(ctx.args).strip()
        if not query:
            await update.message.reply_text("用法：`/m 标签或描述关键词`")
            return
        res = await self._cinevec.search_metadata(query)
        items = (res.get("items") or [])[:5]
        if not items:
            await update.message.reply_text("没有匹配的元数据记录。")
            return
        for item in items:
            aid = item.get("id")
            name = item.get("display_name") or aid
            url = item.get("source_url") or ""
            desc = (item.get("description") or "")[:120]
            text = f"{name}\n{desc}".strip()
            buttons = self._build_buttons(aid, url, item.get("external_ref") or {})
            cov = item.get("video_cover_locator") or item.get("cover_locator")
            if cov:
                try:
                    img_bytes = await self._cinevec.fetch_asset_bytes(cov)
                    await update.message.reply_photo(photo=img_bytes, caption=text, reply_markup=buttons)
                    continue
                except Exception:
                    pass
            await update.message.reply_text(text, reply_markup=buttons)

    async def _on_text(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        # Plain text messages without `/s ` must not trigger search.
        return

    async def _search_by_photo(self, update: Update, photo_size: Any, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        tg_file = await ctx.bot.get_file(photo_size.file_id)
        data = bytes(await tg_file.download_as_bytearray())
        res = await self._cinevec.search_image(data, top_n=5)
        await self._reply_search_results(update, res)

    async def _reply_search_results(self, update: Update, res: Dict[str, Any]) -> None:
        if update.message is None:
            return
        items = (res.get("items") or [])[:5]
        if not items:
            await update.message.reply_text("没有找到匹配的结果。")
            return
        for item in items:
            aid = item.get("asset_id") or item.get("id")
            previews = item.get("previews") or []
            first_prev = previews[0] if previews else {}
            caption = format_search_hit_caption(item)
            buttons = self._build_buttons(
                aid,
                item.get("source_url") or "",
                item.get("external_ref") or {},
            )
            locator = first_prev.get("locator") or item.get("video_cover_locator")
            if locator:
                try:
                    img_bytes = await self._cinevec.fetch_asset_bytes(locator)
                    await update.message.reply_photo(photo=img_bytes, caption=caption, reply_markup=buttons)
                    continue
                except Exception:
                    pass
            await update.message.reply_text(caption, reply_markup=buttons)

    def _build_buttons(
        self,
        asset_id: str,
        source_url: str,
        ext_ref: Dict[str, Any],
    ) -> Optional[InlineKeyboardMarkup]:
        row: List[InlineKeyboardButton] = []
        if source_url and source_url.startswith(("http://", "https://")):
            row.append(InlineKeyboardButton("直达原消息", url=source_url))
        params = ext_ref.get("params") if isinstance(ext_ref, dict) else None
        if isinstance(params, dict) and params.get("file_id") and params.get("chat_id") and params.get("message_id"):
            row.append(
                InlineKeyboardButton(
                    "发送原文件",
                    callback_data=f"orig:{params['chat_id']}:{params['message_id']}",
                )
            )
        if asset_id:
            row.append(InlineKeyboardButton("排除", callback_data=f"excl:{asset_id}"))
        return InlineKeyboardMarkup([row]) if row else None

    async def _on_channel_media(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.channel_post or update.effective_message
        if msg is None:
            return
        source = await self._store.find_source_by_chat(int(msg.chat_id))
        if source is None or not source.get("enabled"):
            return
        await self._ingest_bot_message(msg, ctx, source=source, send_progress=False)

    async def _on_media(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.message
        if msg is None:
            return
        chat_type = getattr(msg.chat, "type", "private")
        if chat_type in ("group", "supergroup", "channel"):
            source = await self._store.find_source_by_chat(int(msg.chat_id))
            if source is not None and source.get("enabled"):
                await self._ingest_bot_message(msg, ctx, source=source, send_progress=False)
                return
            if not self._is_allowed(update):
                return
        elif not self._is_allowed(update):
            return

        caption = (msg.caption or "").strip()
        if msg.photo and is_photo_search_caption(caption):
            await self._search_by_photo(update, msg.photo[-1], ctx)
            return
        await self._ingest_bot_message(msg, ctx, source=None, send_progress=True)

    async def _ingest_bot_message(
        self,
        msg: Any,
        ctx: ContextTypes.DEFAULT_TYPE,
        *,
        source: Optional[Dict[str, Any]],
        send_progress: bool,
    ) -> None:
        caption = (msg.caption or "").strip()
        kind: Optional[str] = None
        file_obj: Any = None
        filename = ""
        mime_type = "application/octet-stream"
        duration: Optional[int] = None
        if msg.photo:
            kind = "image"
            file_obj = msg.photo[-1]
            filename = f"photo_{msg.message_id}.jpg"
            mime_type = "image/jpeg"
        elif msg.video:
            kind = "video"
            file_obj = msg.video
            filename = msg.video.file_name or f"video_{msg.message_id}.mp4"
            mime_type = msg.video.mime_type or "video/mp4"
            duration = msg.video.duration
        elif msg.document:
            mime = (msg.document.mime_type or "").lower()
            if mime.startswith("image/"):
                kind = "image"
            elif mime.startswith("video/"):
                kind = "video"
            file_obj = msg.document
            filename = msg.document.file_name or f"file_{msg.message_id}"
            mime_type = mime or mime_type

        if not kind or file_obj is None:
            return

        async def _download(dest: Path) -> Path:
            tg_file = await ctx.bot.get_file(file_obj.file_id)
            await tg_file.download_to_drive(custom_path=str(dest))
            return dest

        target_chat_id = int(msg.chat_id)
        target_msg_id = int(msg.message_id)
        target_username = getattr(msg.chat, "username", None)
        target_title = getattr(msg.chat, "title", None)

        # Preserve original channel link when user forwards a channel post to the Bot
        fwd = getattr(msg, "forward_origin", None)
        if fwd is not None and getattr(fwd, "chat", None) is not None and getattr(fwd, "message_id", None) is not None:
            target_chat_id = int(fwd.chat.id)
            target_msg_id = int(fwd.message_id)
            target_username = getattr(fwd.chat, "username", None)
            target_title = getattr(fwd.chat, "title", None)

        env = TelegramMediaEnvelope(
            chat_id=target_chat_id,
            message_id=target_msg_id,
            kind=kind,
            filename=filename,
            mime_type=mime_type,
            file_size=int(getattr(file_obj, "file_size", 0) or 0),
            file_unique_id=str(getattr(file_obj, "file_unique_id", "") or ""),
            file_id=str(getattr(file_obj, "file_id", "") or ""),
            media_group_id=str(msg.media_group_id) if getattr(msg, "media_group_id", None) else None,
            chat_username=target_username,
            chat_title=target_title,
            caption=caption,
            duration=duration,
            source_id=int(source["id"]) if source else None,
            store_original=bool(source.get("store_original")) if source else False,
            backup_chat_id=source.get("backup_chat_id") if source else None,
            default_tags=list(source.get("default_tags") or []) if source else [],
            download_fn=_download,
        )
        if send_progress:
            key = f"{target_chat_id}:{env.media_group_id or target_msg_id}"
            self._reply_targets[key] = msg
        await self._grouper.push(env)

    async def _flush_bot_envelopes(self, envelopes: List[TelegramMediaEnvelope]) -> None:
        if not envelopes:
            return
        first = envelopes[0]
        key = f"{first.chat_id}:{first.media_group_id or first.message_id}"
        reply_msg = self._reply_targets.pop(key, None)
        res = await self._pipeline.ingest_batch(envelopes, stop_event=self._stop)
        if reply_msg is None:
            return
        batch_ids = res.get("batch_ids") or []
        if not batch_ids:
            skipped = len(res.get("skipped") or [])
            failed = len(res.get("failed") or [])
            await reply_msg.reply_text(f"处理完成：跳过 {skipped} 条（重复或超限），失败 {failed} 条。")
            return
        status_msg = await reply_msg.reply_text(f"已提交 {len(res['uploaded'])} 个文件到 CineVec，排队预处理中...")
        task = asyncio.create_task(self._poll_batch_progress(batch_ids[0], status_msg))
        self._poll_tasks.add(task)
        task.add_done_callback(self._poll_tasks.discard)

    async def _poll_batch_progress(self, batch_id: str, status_msg: Any) -> None:
        last_text = ""
        for _ in range(120):
            if self._stop.is_set():
                return
            await asyncio.sleep(2.5)
            try:
                batch = await self._cinevec.get_batch(batch_id)
            except Exception:
                continue
            counts = batch.get("counts") or {}
            total = len(batch.get("items") or [])
            searchable = int(counts.get("searchable", 0))
            failed = int(counts.get("failed", 0)) + int(counts.get("blocked", 0))
            text = (
                f"入库进度 ({searchable}/{total} 可检索)：\n"
                f"排队 {counts.get('queued', 0)} · 预处理 {counts.get('preprocess', 0)} · "
                f"编码 {counts.get('encode', 0)} · 写库 {counts.get('index_write', 0)}"
            )
            if failed:
                text += f" · 异常 {failed}"
            if text != last_text:
                try:
                    await status_msg.edit_text(text)
                    last_text = text
                except Exception:
                    pass
            if total > 0 and (searchable + failed) >= total:
                return

    async def _on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        if not self._is_allowed(update):
            await query.answer("无权限", show_alert=True)
            return
        data = query.data or ""
        if data.startswith("excl:"):
            aid = data.split(":", 1)[1]
            await self._cinevec.exclude_asset(aid)
            await query.answer("已从 CineVec 排除并清理向量")
            return
        if data.startswith("orig:"):
            parts = data.split(":")
            if len(parts) == 3:
                cid, mid = int(parts[1]), int(parts[2])
                item = await self._store.find_by_chat_msg(cid, mid)
                fid = item.get("file_id") if item else None
                if not fid:
                    await query.answer("该条记录未缓存 Bot file_id，请点击「直达原消息」查看。", show_alert=True)
                    return
                if item and item["kind"] == "image":
                    await ctx.bot.send_photo(chat_id=query.message.chat_id, photo=fid)
                else:
                    await ctx.bot.send_video(chat_id=query.message.chat_id, video=fid)
                await query.answer("已发送原文件")
