from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import httpx
import pytest
from fastapi.testclient import TestClient

from tg_plugin.app import create_plugin_app
from tg_plugin.bot import TelegramBotRunner
from tg_plugin.cinevec_client import CineVecClient
from tg_plugin.config import PluginSettings
from tg_plugin.state_store import StateStore


class FakeBotAPI:
    def __init__(self, chat_obj: Any = None, file_bytes: bytes = b"fake-jpg") -> None:
        self.chat_obj = chat_obj
        self.file_bytes = file_bytes

    async def get_chat(self, target: Any) -> Any:
        return self.chat_obj

    async def get_file(self, file_id: str) -> Any:
        data = self.file_bytes

        class _TGFile:
            async def download_to_drive(self, custom_path: str) -> None:
                Path(custom_path).write_bytes(data)

        return _TGFile()


@pytest.mark.asyncio
async def test_bot_only_mode_probe_and_forwarded_channel_ingest(tmp_path: Path):
    captured: List[Dict[str, Any]] = []

    def cinevec_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/v1/assets":
            return httpx.Response(200, json={"items": [], "total": 0})
        if request.url.path == "/api/v1/imports":
            return httpx.Response(
                201,
                json={
                    "batch_id": "b-bot-1",
                    "assets": [{"id": "asset-bot-1", "ref": {"ref_id": "tg_-100777_888"}, "lifecycle": "active"}],
                    "jobs": [],
                },
            )
        return httpx.Response(200, json={})

    settings = PluginSettings(
        cinevec_base_url="http://127.0.0.1:8008",
        cinevec_api_token="pica_bot_only",
        tg_bot_token="123456:FAKE_BOT_TOKEN",
        tg_allowed_user_ids=[],
        plugin_data_dir=tmp_path / "data",
        disk_min_free_gb=0.001,
        media_group_debounce_seconds=0.05,
    )
    cinevec = CineVecClient(
        settings.cinevec_base_url,
        settings.cinevec_api_token,
        transport=httpx.MockTransport(cinevec_handler),
    )
    app = create_plugin_app(settings, cinevec=cinevec, start_background=False)

    # Simulate running Bot API without Telethon
    with TestClient(app) as client:
        bot_runner: TelegramBotRunner = app.state.bot_runner
        bot_runner._running = True
        bot_runner._app = SimpleNamespace(
            bot=FakeBotAPI(
                chat_obj=SimpleNamespace(
                    id=-100777,
                    username="orig_channel",
                    title="Origin Channel",
                    type="channel",
                )
            )
        )  # type: ignore[assignment]

        st = client.get("/status")
        assert st.status_code == 200
        assert st.json()["active_mode"] == "bot_only"
        assert st.json()["capabilities"]["can_chat_check"] is True
        assert st.json()["capabilities"]["can_backfill"] is False

        # Creating a source resolves chat_id via Bot get_chat when Bot is running
        created = client.post("/sources", json={"chat_ref": "@orig_channel"})
        assert created.status_code == 201
        assert created.json()["chat_id"] == -100777
        assert created.json()["chat_username"] == "orig_channel"
        sid = created.json()["id"]

        # Running availability check in bot_only mode works via CompositeInspector -> Bot
        chk = client.post("/checks/run", json={"source_id": sid})
        assert chk.status_code == 200
        assert chk.json()["sources_checked"] == 1

        # Backfill gracefully returns 503 explaining MTProto is required
        bf = client.post(f"/sources/{sid}/backfill")
        assert bf.status_code == 503

    # Verify private chat open access when whitelist is empty & forward_origin preserves original channel link
    store = StateStore(settings.db_path)
    await store.init()
    try:
        from tg_plugin.backpressure import BackpressureController
        from tg_plugin.ingest_pipeline import IngestPipeline

        bp = BackpressureController(settings.tmp_dir, cinevec, disk_min_free_gb=0.001)
        pipeline = IngestPipeline(settings, store, cinevec, bp)
        runner = TelegramBotRunner(settings, store, cinevec, pipeline)
        runner._running = True

        async def _capture_ingest(envs, *, stop_event=None):
            captured.extend([{"chat_id": e.chat_id, "message_id": e.message_id, "username": e.chat_username} for e in envs])
            return {"accepted": len(envs), "uploaded": [], "skipped": [], "failed": [], "batch_ids": []}

        pipeline.ingest_batch = _capture_ingest  # type: ignore[method-assign]

        replies: List[str] = []

        async def _reply_text(txt: str) -> None:
            replies.append(txt)

        fake_update = SimpleNamespace(
            effective_user=SimpleNamespace(id=99999),
            effective_chat=SimpleNamespace(id=99999, type="private"),
            message=SimpleNamespace(
                chat_id=99999,
                message_id=12,
                chat=SimpleNamespace(id=99999, type="private", username=None, title=None),
                caption="forwarded shot",
                photo=[SimpleNamespace(file_id="fid_1", file_unique_id="uid_fwd_1", file_size=1234)],
                video=None,
                document=None,
                media_group_id=None,
                forward_origin=SimpleNamespace(
                    chat=SimpleNamespace(id=-100777, username="orig_channel", title="Origin Channel"),
                    message_id=888,
                ),
                reply_text=_reply_text,
            ),
        )
        fake_ctx = SimpleNamespace(bot=FakeBotAPI())
        await runner._on_media(fake_update, fake_ctx)  # type: ignore[arg-type]
        assert captured == [{"chat_id": -100777, "message_id": 888, "username": "orig_channel"}]
        assert len(replies) == 1
    finally:
        await store.close()


def test_filter_rules_and_selective_browse_import_endpoints(tmp_path: Path):
    from tg_plugin.ingest_pipeline import matches_filter_rules
    from tg_plugin.models import TelegramMediaEnvelope

    env_img = TelegramMediaEnvelope(
        chat_id=-1001,
        message_id=1,
        kind="image",
        filename="meme.jpg",
        file_size=200 * 1024,  # 0.2 MB
        caption="广告 promo",
    )
    env_vid = TelegramMediaEnvelope(
        chat_id=-1001,
        message_id=2,
        kind="video",
        filename="sprint.mp4",
        file_size=12 * 1024 * 1024,  # 12 MB
        duration=15,
        caption="海边跑步 #running",
    )
    rules = {
        "media_kinds": ["video"],
        "min_file_mb": 2.0,
        "min_duration_sec": 5,
        "include_keywords": ["跑步"],
        "exclude_keywords": ["广告"],
    }
    assert matches_filter_rules(env_img, rules)[0] is False
    assert matches_filter_rules(env_vid, rules) == (True, None)

    settings = PluginSettings(
        cinevec_base_url="http://127.0.0.1:8008",
        cinevec_api_token="pica_test",
        plugin_data_dir=tmp_path / "data",
        disk_min_free_gb=0.001,
    )
    cinevec = CineVecClient(
        settings.cinevec_base_url,
        settings.cinevec_api_token,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"status": "ok"})),
    )
    app = create_plugin_app(settings, cinevec=cinevec, start_background=False)

    with TestClient(app) as client:
        created = client.post(
            "/sources",
            json={
                "chat_ref": "@clips_chan",
                "filter_rules": rules,
            },
        )
        assert created.status_code == 201
        sid = created.json()["id"]
        assert created.json()["filter_rules"]["media_kinds"] == ["video"]
        assert created.json()["filter_rules"]["min_duration_sec"] == 5

        patched = client.patch(
            f"/sources/{sid}",
            json={"filter_rules": {"media_kinds": ["video"], "backfill_days": 30}},
        )
        assert patched.status_code == 200
        assert patched.json()["filter_rules"]["backfill_days"] == 30

        # Without Telethon authorized, browse/import-selected return 503 cleanly
        assert client.get(f"/sources/{sid}/messages").status_code == 503
        assert client.post(f"/sources/{sid}/import-selected", json={"message_ids": [2]}).status_code == 503

        # Simulate authorized Telethon worker to verify browse/thumb/import-selected routes
        tw = app.state.telethon_worker
        tw._authorized = True

        async def _fake_browse(source_id: int, *, limit: int = 30, offset_id: int = 0, apply_rules: bool = True):
            return {
                "source_id": source_id,
                "items": [{"message_id": 2, "kind": "video", "filename": "sprint.mp4", "thumb_url": f"/sources/{source_id}/messages/2/thumb"}],
                "next_offset_id": 2,
            }

        async def _fake_thumb(source_id: int, message_id: int):
            return b"thumb-jpeg" if message_id == 2 else None

        async def _fake_import_selected(source_id: int, message_ids: List[int]):
            return {"accepted": len(message_ids), "uploaded": [{"message_id": 2}], "skipped": [], "failed": [], "batch_ids": ["b-sel"]}

        tw.browse_messages = _fake_browse  # type: ignore[method-assign]
        tw.get_message_thumb = _fake_thumb  # type: ignore[method-assign]
        tw.import_selected_messages = _fake_import_selected  # type: ignore[method-assign]

        b = client.get(f"/sources/{sid}/messages?limit=24")
        assert b.status_code == 200
        assert b.json()["items"][0]["message_id"] == 2

        th = client.get(f"/sources/{sid}/messages/2/thumb")
        assert th.status_code == 200
        assert th.content == b"thumb-jpeg"

        imp = client.post(f"/sources/{sid}/import-selected", json={"message_ids": [2]})
        assert imp.status_code == 200
        assert imp.json()["accepted"] == 1
        assert imp.json()["batch_ids"] == ["b-sel"]


@pytest.mark.asyncio
async def test_bot_and_telethon_require_slash_s_space_to_search(tmp_path: Path):
    from tg_plugin.backpressure import BackpressureController
    from tg_plugin.ingest_pipeline import IngestPipeline
    from tg_plugin.models import is_photo_search_caption, parse_text_search_query
    from tg_plugin.telethon_worker import TelethonWorker

    assert parse_text_search_query("sunset") is None
    assert parse_text_search_query("/s") is None
    assert parse_text_search_query("/s   ") is None
    assert parse_text_search_query("/ssunset") is None
    assert parse_text_search_query("/s sunset by sea") == "sunset by sea"
    assert parse_text_search_query("/S@mybot sunset") == "sunset"

    assert is_photo_search_caption("") is False
    assert is_photo_search_caption("搜") is False
    assert is_photo_search_caption("/something") is False
    assert is_photo_search_caption("/s") is True
    assert is_photo_search_caption("/s ") is True
    assert is_photo_search_caption("/s wrench") is True

    text_queries: List[str] = []
    image_searches: List[bytes] = []

    def cinevec_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/v1/search/text":
            import json as _json
            body = _json.loads(request.content.decode("utf-8"))
            text_queries.append(body["query"])
            return httpx.Response(
                200,
                json={"items": [{"asset_id": "a1", "display_name": "clip.mp4", "score": 0.88, "source_url": "https://t.me/c/1/2"}]},
            )
        if request.url.path == "/api/v1/search/image":
            image_searches.append(request.content)
            return httpx.Response(200, json={"items": [{"asset_id": "a2", "display_name": "wrench.jpg", "score": 0.91}]})
        return httpx.Response(200, json={})

    settings = PluginSettings(
        cinevec_base_url="http://127.0.0.1:8008",
        cinevec_api_token="pica_search",
        tg_bot_token="123456:FAKE_BOT_TOKEN",
        tg_allowed_user_ids=[111],
        plugin_data_dir=tmp_path / "data",
        disk_min_free_gb=0.001,
    )
    store = StateStore(settings.db_path)
    await store.init()
    try:
        cinevec = CineVecClient(
            settings.cinevec_base_url,
            settings.cinevec_api_token,
            transport=httpx.MockTransport(cinevec_handler),
        )
        bp = BackpressureController(settings.tmp_dir, cinevec, disk_min_free_gb=0.001)
        pipeline = IngestPipeline(settings, store, cinevec, bp)

        # 1) Bot checks
        bot = TelegramBotRunner(settings, store, cinevec, pipeline)
        bot_replies: List[str] = []

        async def _bot_reply(txt: str, **_kw: Any) -> None:
            bot_replies.append(txt)

        def _make_bot_update(text: str):
            return SimpleNamespace(
                effective_user=SimpleNamespace(id=111),
                effective_chat=SimpleNamespace(id=111, type="private"),
                message=SimpleNamespace(
                    text=text,
                    reply_to_message=None,
                    reply_text=_bot_reply,
                ),
            )

        # Plain text must NOT trigger search
        await bot._on_text(_make_bot_update("sunset"), SimpleNamespace())  # type: ignore[arg-type]
        assert text_queries == []

        # Bare /s without space+query must NOT search
        await bot._cmd_search_text(_make_bot_update("/s"), SimpleNamespace(args=[]))  # type: ignore[arg-type]
        assert text_queries == []
        assert any("/s" in r for r in bot_replies)

        # `/s sunset` triggers search
        await bot._cmd_search_text(_make_bot_update("/s sunset"), SimpleNamespace(args=["sunset"]))  # type: ignore[arg-type]
        assert text_queries == ["sunset"]

        # 2) Telethon User Account checks
        tw = TelethonWorker(settings, store, pipeline, cinevec=cinevec)
        tw._authorized = True
        tw_replies: List[str] = []

        class _FakeClient:
            async def download_media(self, _msg: Any, file: Any = None, **_kw: Any) -> Any:
                if hasattr(file, "write"):
                    file.write(b"fake-photo-bytes")
                return file

        tw._client = _FakeClient()  # type: ignore[assignment]

        def _make_tl_event(text: str, *, out: bool, sender_id: int = 111, photo: Any = None):
            async def _reply(txt: str, **_kw: Any) -> None:
                tw_replies.append(txt)

            msg = SimpleNamespace(
                id=50,
                message=text,
                raw_text=text,
                out=out,
                sender_id=sender_id,
                photo=photo,
                is_reply=False,
            )
            return SimpleNamespace(chat_id=999, message=msg, reply=_reply)

        # Normal chat / bare /s / missing space must NOT search
        await tw._on_new_message(_make_tl_event("hello world", out=True))
        await tw._on_new_message(_make_tl_event("/s", out=True))
        await tw._on_new_message(_make_tl_event("/ssunset", out=True))
        assert text_queries == ["sunset"]

        # Unauthorized incoming `/s cat` must be ignored
        await tw._on_new_message(_make_tl_event("/s cat", out=False, sender_id=999999))
        assert text_queries == ["sunset"]

        # Own account (`out=True`) `/s cat` triggers search
        await tw._on_new_message(_make_tl_event("/s cat", out=True))
        assert text_queries == ["sunset", "cat"]
        assert any("clip.mp4" in r for r in tw_replies)

        # Photo with `/s` caption triggers image search on user account
        await tw._on_new_message(_make_tl_event("/s", out=True, photo=SimpleNamespace(id=77)))
        assert len(image_searches) == 1
        assert any("wrench.jpg" in r for r in tw_replies)
    finally:
        await store.close()
