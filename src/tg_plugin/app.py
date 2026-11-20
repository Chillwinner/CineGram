from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response

from tg_plugin.availability_checker import AvailabilityChecker
from tg_plugin.backpressure import BackpressureController
from tg_plugin.bot import TelegramBotRunner
from tg_plugin.cinevec_client import CineVecClient
from tg_plugin.config import PluginSettings, load_settings, mask_secret, save_env_updates
from tg_plugin.ingest_pipeline import IngestPipeline
from tg_plugin.models import (
    BackfillIn,
    ChatProbeResult,
    CheckRunIn,
    ConfigPatchIn,
    ImportSelectedIn,
    MessageProbeResult,
    QuickSearchIn,
    SourceCreateIn,
    SourcePatchIn,
    TelegramInspector,
)
from tg_plugin.state_store import StateStore
from tg_plugin.telethon_worker import TelethonWorker

_STATIC_INDEX = Path(__file__).resolve().parent / "static" / "index.html"
_ALLOWED_PROXY_PREFIXES = (
    "/api/v1/previews/",
    "/api/v1/assets/",
    "/api/previews/",
    "/api/assets/",
)


class _CompositeInspector:
    """Uses MTProto Telethon when authorized, falling back to Bot API when Bot is running."""

    def __init__(self, telethon: TelethonWorker, bot: TelegramBotRunner) -> None:
        self._telethon = telethon
        self._bot = bot

    async def probe_chat(self, source: Dict[str, Any]) -> ChatProbeResult:
        if self._telethon.status["authorized"]:
            return await self._telethon.probe_chat(source)
        if self._bot.status["running"]:
            return await self._bot.probe_chat(source)
        raise RuntimeError("Neither MTProto user account nor Telegram Bot is active")

    async def probe_messages(self, chat_id: int, message_ids: List[int]) -> Dict[int, MessageProbeResult]:
        if self._telethon.status["authorized"]:
            return await self._telethon.probe_messages(chat_id, message_ids)
        if self._bot.status["running"]:
            return await self._bot.probe_messages(chat_id, message_ids)
        raise RuntimeError("Neither MTProto user account nor Telegram Bot is active")


def _to_proxy_url(locator: Optional[str]) -> Optional[str]:
    if not locator or not isinstance(locator, str):
        return None
    if locator.startswith(_ALLOWED_PROXY_PREFIXES):
        return f"/proxy/cinevec?path={quote(locator, safe='')}"
    return locator


def _rewrite_search_for_web(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(payload)
    items = []
    for item in payload.get("items") or []:
        row = dict(item)
        if row.get("video_cover_locator"):
            row["video_cover_locator"] = _to_proxy_url(row["video_cover_locator"])
        if row.get("cover_locator"):
            row["cover_locator"] = _to_proxy_url(row["cover_locator"])
        previews = []
        for prev in row.get("previews") or []:
            p = dict(prev)
            if p.get("locator"):
                p["locator"] = _to_proxy_url(p["locator"])
            previews.append(p)
        row["previews"] = previews
        items.append(row)
    out["items"] = items
    return out


def _masked_config(cfg: PluginSettings) -> Dict[str, Any]:
    return {
        "cinevec_base_url": cfg.cinevec_base_url,
        "has_cinevec_token": bool(cfg.cinevec_api_token),
        "cinevec_token_masked": mask_secret(cfg.cinevec_api_token),
        "has_bot_token": cfg.bot_configured,
        "bot_token_masked": mask_secret(cfg.tg_bot_token),
        "tg_allowed_user_ids": list(cfg.tg_allowed_user_ids),
        "tg_api_id": cfg.tg_api_id,
        "has_tg_api_hash": bool(cfg.tg_api_hash),
        "tg_api_hash_masked": mask_secret(cfg.tg_api_hash),
        "tg_session_name": cfg.tg_session_name,
        "disk_min_free_gb": cfg.disk_min_free_gb,
        "max_preprocess_inflight": cfg.max_preprocess_inflight,
        "max_file_size_mb": cfg.max_file_size_mb,
        "default_check_interval_hours": cfg.default_check_interval_hours,
        "auto_exclude_after_days": cfg.auto_exclude_after_days,
        "env_file": str(cfg.env_file_path) if cfg.env_file_path else None,
    }


def create_plugin_app(
    settings: Optional[PluginSettings] = None,
    *,
    cinevec: Optional[CineVecClient] = None,
    inspector: Optional[TelegramInspector] = None,
    start_background: bool = True,
) -> FastAPI:
    cfg = settings or load_settings()
    store = StateStore(cfg.db_path)
    owns_cinevec = cinevec is None
    cinevec_client = cinevec or CineVecClient(
        cfg.cinevec_base_url,
        cfg.cinevec_api_token,
        upload_timeout=cfg.upload_timeout_seconds,
    )
    backpressure = BackpressureController(
        cfg.tmp_dir,
        cinevec_client,
        disk_min_free_gb=cfg.disk_min_free_gb,
        max_preprocess_inflight=cfg.max_preprocess_inflight,
    )
    pipeline = IngestPipeline(cfg, store, cinevec_client, backpressure)
    telethon_worker = TelethonWorker(cfg, store, pipeline, cinevec=cinevec_client)
    bot_runner = TelegramBotRunner(cfg, store, cinevec_client, pipeline)
    active_inspector: TelegramInspector = inspector or _CompositeInspector(telethon_worker, bot_runner)
    checker = AvailabilityChecker(cfg, store, cinevec_client, active_inspector)

    stop_event = asyncio.Event()
    check_task_holder: Dict[str, Optional[asyncio.Task]] = {"task": None}

    def _mode_and_caps() -> Dict[str, Any]:
        telethon_ok = bool(telethon_worker.status["authorized"])
        bot_ok = bool(bot_runner.status["running"])
        if telethon_ok and bot_ok:
            mode = "dual"
        elif telethon_ok:
            mode = "telethon_only"
        elif bot_ok:
            mode = "bot_only"
        else:
            mode = "web_only"
        return {
            "active_mode": mode,
            "capabilities": {
                "can_backfill": telethon_ok,
                "can_live_channel_sync": telethon_ok or bot_ok,
                "can_bot_search": bot_ok,
                "can_chat_check": inspector is not None or telethon_ok or bot_ok,
                "can_message_check": inspector is not None or telethon_ok,
            },
        }

    async def _periodic_check_loop() -> None:
        while not stop_event.is_set():
            interval_sec = max(60.0, float(cfg.default_check_interval_hours) * 3600.0)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
                break
            except asyncio.TimeoutError:
                pass
            if inspector is None and not telethon_worker.status["authorized"] and not bot_runner.status["running"]:
                continue
            try:
                await checker.run_once()
            except Exception:
                pass

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop_event.clear()
        await store.init()
        backpressure.seed_batches(await store.list_recent_batch_ids())
        if start_background:
            try:
                await telethon_worker.start()
            except Exception:
                pass
            try:
                await bot_runner.start()
            except Exception:
                pass
            check_task_holder["task"] = asyncio.create_task(_periodic_check_loop())
        try:
            yield
        finally:
            stop_event.set()
            ctask = check_task_holder.get("task")
            if ctask is not None:
                ctask.cancel()
                try:
                    await ctask
                except asyncio.CancelledError:
                    pass
            if start_background:
                await bot_runner.stop()
                await telethon_worker.stop()
            if owns_cinevec:
                await cinevec_client.close()
            await store.close()

    app = FastAPI(
        title="CineGram · CineVec Telegram Plugin",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = cfg
    app.state.store = store
    app.state.cinevec = cinevec_client
    app.state.backpressure = backpressure
    app.state.pipeline = pipeline
    app.state.telethon_worker = telethon_worker
    app.state.checker = checker
    app.state.bot_runner = bot_runner

    @app.get("/", response_class=HTMLResponse)
    async def web_console() -> HTMLResponse:
        if not _STATIC_INDEX.is_file():
            raise HTTPException(status_code=404, detail="Web UI asset missing")
        return HTMLResponse(_STATIC_INDEX.read_text(encoding="utf-8"))

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        return {"status": "ok", "service": "cinevec-telegram-plugin", "version": "0.1.0"}

    @app.get("/status")
    async def status() -> Dict[str, Any]:
        cinevec_ok = False
        cinevec_detail: Any = None
        try:
            cinevec_detail = await cinevec_client.health()
            cinevec_ok = True
        except Exception as exc:
            cinevec_detail = str(exc)
        bp = await backpressure.snapshot()
        counts = await store.count_items_by_status()
        sources = await store.list_sources()
        mc = _mode_and_caps()
        return {
            "active_mode": mc["active_mode"],
            "capabilities": mc["capabilities"],
            "cinevec": {"ok": cinevec_ok, "base_url": cfg.cinevec_base_url, "detail": cinevec_detail},
            "telethon": telethon_worker.status,
            "bot": bot_runner.status,
            "backpressure": bp,
            "sources_total": len(sources),
            "items_by_status": counts,
        }

    @app.get("/config")
    async def get_config() -> Dict[str, Any]:
        return _masked_config(cfg)

    @app.patch("/config")
    async def patch_config(body: ConfigPatchIn) -> Dict[str, Any]:
        env_updates: Dict[str, str] = {}
        cinevec_changed = False
        bot_changed = False
        telethon_changed = False

        if body.cinevec_base_url is not None:
            cleaned = body.cinevec_base_url.strip().rstrip("/")
            if cleaned and cleaned != cfg.cinevec_base_url:
                cfg.cinevec_base_url = cleaned
                env_updates["CINEVEC_BASE_URL"] = cleaned
                cinevec_changed = True

        if body.cinevec_api_token is not None:
            cleaned = body.cinevec_api_token.strip()
            if cleaned:
                cfg.cinevec_api_token = cleaned
                env_updates["CINEVEC_API_TOKEN"] = cleaned
                cinevec_changed = True

        if body.clear_bot_token:
            if cfg.tg_bot_token:
                cfg.tg_bot_token = ""
                env_updates["TG_BOT_TOKEN"] = ""
                bot_changed = True
        elif body.tg_bot_token is not None:
            cleaned = body.tg_bot_token.strip()
            if cleaned and cleaned != cfg.tg_bot_token:
                cfg.tg_bot_token = cleaned
                env_updates["TG_BOT_TOKEN"] = cleaned
                bot_changed = True

        if body.tg_allowed_user_ids is not None:
            cfg.tg_allowed_user_ids = [int(x) for x in body.tg_allowed_user_ids]
            env_updates["TG_ALLOWED_USER_IDS"] = ",".join(str(x) for x in cfg.tg_allowed_user_ids)

        if body.clear_telethon:
            cfg.tg_api_id = None
            cfg.tg_api_hash = ""
            env_updates["TG_API_ID"] = ""
            env_updates["TG_API_HASH"] = ""
            telethon_changed = True
        else:
            if body.tg_api_id is not None and body.tg_api_id != cfg.tg_api_id:
                cfg.tg_api_id = int(body.tg_api_id)
                env_updates["TG_API_ID"] = str(cfg.tg_api_id)
                telethon_changed = True
            if body.tg_api_hash is not None:
                cleaned = body.tg_api_hash.strip()
                if cleaned and cleaned != cfg.tg_api_hash:
                    cfg.tg_api_hash = cleaned
                    env_updates["TG_API_HASH"] = cleaned
                    telethon_changed = True
            if body.tg_session_name is not None:
                cleaned = body.tg_session_name.strip()
                if cleaned and cleaned != cfg.tg_session_name:
                    cfg.tg_session_name = cleaned
                    env_updates["TG_SESSION_NAME"] = cleaned
                    telethon_changed = True

        if body.disk_min_free_gb is not None:
            cfg.disk_min_free_gb = max(0.0, float(body.disk_min_free_gb))
            env_updates["DISK_MIN_FREE_GB"] = str(cfg.disk_min_free_gb)
        if body.max_preprocess_inflight is not None:
            cfg.max_preprocess_inflight = max(1, int(body.max_preprocess_inflight))
            env_updates["MAX_PREPROCESS_INFLIGHT"] = str(cfg.max_preprocess_inflight)
        if body.max_file_size_mb is not None:
            cfg.max_file_size_mb = max(1, int(body.max_file_size_mb))
            env_updates["MAX_FILE_SIZE_MB"] = str(cfg.max_file_size_mb)
        if body.default_check_interval_hours is not None:
            cfg.default_check_interval_hours = max(0.05, float(body.default_check_interval_hours))
            env_updates["DEFAULT_CHECK_INTERVAL_HOURS"] = str(cfg.default_check_interval_hours)
        if body.auto_exclude_after_days is not None:
            cfg.auto_exclude_after_days = max(0, int(body.auto_exclude_after_days))
            env_updates["AUTO_EXCLUDE_AFTER_DAYS"] = str(cfg.auto_exclude_after_days)

        if env_updates and cfg.env_file_path is not None:
            save_env_updates(cfg.env_file_path, env_updates)

        if cinevec_changed:
            await cinevec_client.update_credentials(cfg.cinevec_base_url, cfg.cinevec_api_token)
        backpressure.update_limits(
            disk_min_free_gb=cfg.disk_min_free_gb,
            max_preprocess_inflight=cfg.max_preprocess_inflight,
        )
        bot_runner.update_settings(cfg)
        telethon_worker.update_settings(cfg)

        warnings: List[str] = []
        if start_background and bot_changed:
            try:
                await bot_runner.restart()
            except Exception as exc:
                warnings.append(f"Bot 启动/重启失败: {exc}")
        if start_background and telethon_changed:
            try:
                await telethon_worker.restart()
            except Exception as exc:
                warnings.append(f"Telethon 重启失败: {exc}")

        mc = _mode_and_caps()
        return {
            "config": _masked_config(cfg),
            "active_mode": mc["active_mode"],
            "capabilities": mc["capabilities"],
            "telethon": telethon_worker.status,
            "bot": bot_runner.status,
            "warnings": warnings,
        }

    @app.get("/sources")
    async def list_sources() -> Dict[str, Any]:
        items = await store.list_sources()
        return {"items": items, "total": len(items)}

    @app.post("/sources", status_code=201)
    async def create_source(body: SourceCreateIn) -> Dict[str, Any]:
        created = await store.create_source(body.model_dump())
        if telethon_worker.status["authorized"]:
            try:
                resolved = await telethon_worker.resolve_source(created["id"])
                if resolved is not None:
                    created = resolved
            except Exception:
                pass
        elif bot_runner.status["running"]:
            try:
                resolved = await bot_runner.resolve_source(created["id"])
                if resolved is not None:
                    created = resolved
            except Exception:
                pass
        return created

    @app.patch("/sources/{source_id}")
    async def patch_source(source_id: int, body: SourcePatchIn) -> Dict[str, Any]:
        updated = await store.update_source(source_id, **body.model_dump(exclude_unset=True))
        if updated is None:
            raise HTTPException(status_code=404, detail="Source not found")
        return updated

    @app.delete("/sources/{source_id}")
    async def delete_source(source_id: int) -> Dict[str, Any]:
        ok = await store.delete_source(source_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Source not found")
        return {"deleted": True, "id": source_id}

    @app.post("/sources/{source_id}/backfill")
    async def trigger_backfill(source_id: int, body: Optional[BackfillIn] = None) -> Dict[str, Any]:
        src = await store.get_source(source_id)
        if src is None:
            raise HTTPException(status_code=404, detail="Source not found")
        if not telethon_worker.status["authorized"]:
            raise HTTPException(
                status_code=503,
                detail="历史回溯需要 MTProto 用户账号登录；当前未登录或仅处于 Web/Bot 模式。",
            )
        limit = body.limit if body else None
        days = body.days if body else None
        started = telethon_worker.start_backfill(source_id, limit=limit, days=days)
        return {"source_id": source_id, "started": started}

    @app.get("/sources/{source_id}/messages")
    async def browse_source_messages(
        source_id: int,
        limit: int = Query(30, ge=1, le=100),
        offset_id: int = Query(0, ge=0),
        apply_rules: bool = Query(True),
    ) -> Dict[str, Any]:
        src = await store.get_source(source_id)
        if src is None:
            raise HTTPException(status_code=404, detail="Source not found")
        if not telethon_worker.status["authorized"]:
            raise HTTPException(
                status_code=503,
                detail="浏览群消息预览需要 MTProto 用户账号登录。",
            )
        return await telethon_worker.browse_messages(
            source_id,
            limit=limit,
            offset_id=offset_id,
            apply_rules=apply_rules,
        )

    @app.get("/sources/{source_id}/messages/{message_id}/thumb")
    async def get_source_message_thumb(source_id: int, message_id: int) -> Response:
        if not telethon_worker.status["authorized"]:
            raise HTTPException(status_code=503, detail="浏览缩略图需要 MTProto 用户账号登录。")
        data = await telethon_worker.get_message_thumb(source_id, message_id)
        if not data:
            raise HTTPException(status_code=404, detail="Thumbnail not found")
        return Response(content=data, media_type="image/jpeg")

    @app.post("/sources/{source_id}/import-selected")
    async def import_selected_messages(source_id: int, body: ImportSelectedIn) -> Dict[str, Any]:
        src = await store.get_source(source_id)
        if src is None:
            raise HTTPException(status_code=404, detail="Source not found")
        if not telethon_worker.status["authorized"]:
            raise HTTPException(
                status_code=503,
                detail="按勾选导入群消息需要 MTProto 用户账号登录。",
            )
        return await telethon_worker.import_selected_messages(source_id, body.message_ids)

    @app.post("/checks/run")
    async def run_check(body: Optional[CheckRunIn] = None) -> Dict[str, Any]:
        if inspector is None and not telethon_worker.status["authorized"] and not bot_runner.status["running"]:
            raise HTTPException(
                status_code=503,
                detail="需要先配置并启动 Bot 或登录 MTProto 用户账号后，才能探测 Telegram 群组/消息。",
            )
        sid = body.source_id if body else None
        return await checker.run_once(source_id=sid)

    @app.get("/checks/logs")
    async def list_check_logs(limit: int = Query(50, ge=1, le=200)) -> Dict[str, Any]:
        items = await store.list_check_logs(limit=limit)
        return {"items": items, "total": len(items)}

    @app.get("/items")
    async def list_items(
        source_id: Optional[int] = None,
        status_filter: Optional[str] = Query(None, alias="status"),
        lifecycle: Optional[str] = None,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> Dict[str, Any]:
        items = await store.list_synced_items(
            source_id=source_id,
            status=status_filter,
            lifecycle=lifecycle,
            limit=limit,
            offset=offset,
        )
        return {"items": items, "limit": limit, "offset": offset}

    @app.post("/items/{item_id}/retry")
    async def retry_item(item_id: int) -> Dict[str, Any]:
        item = await store.get_synced_item(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Item not found")
        if not telethon_worker.status["authorized"] or telethon_worker._client is None:
            raise HTTPException(
                status_code=503,
                detail="按消息 ID 重新拉取需要 MTProto 用户账号登录；若通过 Bot 入库请重新转发该消息。",
            )
        source = await store.get_source(int(item["source_id"])) if item.get("source_id") else {}
        entity = await telethon_worker._client.get_entity(int(item["chat_id"]))
        msg = await telethon_worker._client.get_messages(entity, ids=int(item["message_id"]))
        if msg is None:
            raise HTTPException(status_code=404, detail="Original Telegram message no longer exists")
        env = telethon_worker._to_envelope(msg, source or {"id": item.get("source_id") or 0}, entity)
        if env is None:
            raise HTTPException(status_code=400, detail="Message has no supported image/video media")
        await store.update_synced_item(item_id, status="pending", error_message=None)
        res = await pipeline.ingest_batch([env], stop_event=stop_event)
        return {"item_id": item_id, "result": res}

    @app.post("/items/{item_id}/exclude")
    async def exclude_item(item_id: int) -> Dict[str, Any]:
        item = await store.get_synced_item(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Item not found")
        aid = item.get("asset_id")
        if not aid:
            raise HTTPException(status_code=400, detail="Item has no CineVec asset_id")
        await cinevec_client.exclude_asset(aid)
        updated = await store.update_synced_item(item_id, status="excluded", lifecycle="excluded")
        return {"item": updated}

    @app.post("/items/{item_id}/restore")
    async def restore_item(item_id: int) -> Dict[str, Any]:
        item = await store.get_synced_item(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Item not found")
        aid = item.get("asset_id")
        if not aid:
            raise HTTPException(status_code=400, detail="Item has no CineVec asset_id")
        await cinevec_client.restore_asset(aid)
        updated = await store.update_synced_item(
            item_id,
            status="uploaded",
            lifecycle="active",
            unavailable_since=None,
        )
        return {"item": updated}

    @app.post("/search/text")
    async def quick_search_text(body: QuickSearchIn) -> Dict[str, Any]:
        query = body.query.strip()
        if not query:
            raise HTTPException(status_code=400, detail="Query cannot be empty")
        res = await cinevec_client.search_text(query, top_n=max(1, min(30, body.top_n)))
        return _rewrite_search_for_web(res)

    @app.post("/search/metadata")
    async def quick_search_metadata(body: QuickSearchIn) -> Dict[str, Any]:
        query = body.query.strip()
        if not query:
            raise HTTPException(status_code=400, detail="Query cannot be empty")
        res = await cinevec_client.search_metadata(query)
        return _rewrite_search_for_web(res)

    @app.get("/proxy/cinevec")
    async def proxy_cinevec_image(path: str = Query(...)) -> Response:
        cleaned = path.strip()
        if (
            not cleaned.startswith(_ALLOWED_PROXY_PREFIXES)
            or ".." in cleaned
            or "://" in cleaned
            or "@" in cleaned
        ):
            raise HTTPException(status_code=400, detail="Disallowed proxy path")
        data = await cinevec_client.fetch_asset_bytes(cleaned)
        return Response(content=data, media_type="image/jpeg")

    return app
