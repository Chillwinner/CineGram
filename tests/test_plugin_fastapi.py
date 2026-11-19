from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import httpx
from fastapi.testclient import TestClient

from tg_plugin.cinevec_client import CineVecClient
from tg_plugin.config import PluginSettings
from tg_plugin.models import ChatProbeResult, MessageProbeResult
from tg_plugin.app import create_plugin_app


class DummyInspector:
    async def probe_chat(self, source: Dict[str, Any]) -> ChatProbeResult:
        return ChatProbeResult(
            accessible=True,
            chat_id=source.get("chat_id") or -100999,
            username="demo_chan",
            title="Demo Channel",
            chat_type="channel",
        )

    async def probe_messages(self, chat_id: int, message_ids: List[int]) -> Dict[int, MessageProbeResult]:
        return {mid: MessageProbeResult(message_id=mid, exists=True) for mid in message_ids}


def test_plugin_fastapi_crud_status_and_checks(tmp_path: Path):
    def cinevec_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/health":
            return httpx.Response(200, json={"status": "ok", "service": "pica", "api": "v1"})
        return httpx.Response(200, json={})

    settings = PluginSettings(
        cinevec_base_url="http://127.0.0.1:8008",
        cinevec_api_token="pica_test",
        plugin_data_dir=tmp_path / "data",
        disk_min_free_gb=0.001,
    )
    cinevec = CineVecClient(
        settings.cinevec_base_url,
        settings.cinevec_api_token,
        transport=httpx.MockTransport(cinevec_handler),
    )
    app = create_plugin_app(
        settings,
        cinevec=cinevec,
        inspector=DummyInspector(),
        start_background=False,
    )

    with TestClient(app) as client:
        h = client.get("/health")
        assert h.status_code == 200
        assert h.json()["status"] == "ok"

        st = client.get("/status")
        assert st.status_code == 200
        assert st.json()["cinevec"]["ok"] is True
        assert st.json()["backpressure"]["ready"] is True

        created = client.post(
            "/sources",
            json={
                "chat_ref": "@demo_chan",
                "chat_id": -100999,
                "store_original": False,
                "default_tags": ["cinema"],
            },
        )
        assert created.status_code == 201
        sid = created.json()["id"]
        assert created.json()["enabled"] is False
        assert created.json()["default_tags"] == ["cinema"]

        listed = client.get("/sources")
        assert listed.status_code == 200
        assert listed.json()["total"] == 1

        patched = client.patch(f"/sources/{sid}", json={"enabled": True, "store_original": True})
        assert patched.status_code == 200
        assert patched.json()["store_original"] is True

        chk = client.post("/checks/run", json={"source_id": sid})
        assert chk.status_code == 200
        assert chk.json()["sources_checked"] == 1

        logs = client.get("/checks/logs")
        assert logs.status_code == 200
        assert logs.json()["total"] == 1

        items = client.get("/items")
        assert items.status_code == 200
        assert items.json()["items"] == []

        deleted = client.delete(f"/sources/{sid}")
        assert deleted.status_code == 200
        assert client.get("/sources").json()["total"] == 0


def test_web_only_ui_config_persistence_and_search_proxy(tmp_path: Path):
    env_file = tmp_path / ".env"

    def cinevec_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/health":
            return httpx.Response(200, json={"status": "ok", "service": "pica", "api": "v1"})
        if request.url.path == "/api/v1/search/text":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "asset_id": "a-1",
                            "display_name": "runner.jpg",
                            "score": 0.88,
                            "source_url": "https://t.me/demo/10",
                            "video_cover_locator": "/api/v1/assets/a-1/cover?v=1",
                            "previews": [{"locator": "/api/v1/previews/a-1/frames/0", "time_seconds": 0}],
                        }
                    ]
                },
            )
        if request.url.path == "/api/v1/previews/a-1/frames/0":
            return httpx.Response(200, content=b"jpeg-frame-bytes")
        if request.url.path == "/api/v1/search/metadata":
            return httpx.Response(
                200,
                json={
                    "kind": "lexical",
                    "items": [
                        {
                            "id": "a-1",
                            "display_name": "runner.jpg",
                            "description": "海边冲浪集锦 #冲浪",
                            "tags": ["冲浪"],
                            "matched_on": ["tag", "description"],
                            "cover_locator": "/api/v1/assets/a-1/cover?v=1",
                            "video_cover_locator": "/api/v1/assets/a-1/cover?v=1",
                        }
                    ],
                },
            )
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "not found"}})

    settings = PluginSettings(
        cinevec_base_url="http://127.0.0.1:8008",
        cinevec_api_token="pica_initial_token_1234",
        plugin_data_dir=tmp_path / "data",
        disk_min_free_gb=0.001,
        env_file_path=env_file,
    )
    cinevec = CineVecClient(
        settings.cinevec_base_url,
        settings.cinevec_api_token,
        transport=httpx.MockTransport(cinevec_handler),
    )
    app = create_plugin_app(settings, cinevec=cinevec, start_background=False)

    with TestClient(app) as client:
        # Web UI root loads with zero TG credentials
        home = client.get("/")
        assert home.status_code == 200
        assert "CineVec" in home.text
        assert "凭据与策略配置" in home.text

        st = client.get("/status")
        assert st.status_code == 200
        assert st.json()["active_mode"] == "web_only"
        assert st.json()["capabilities"]["can_backfill"] is False

        cfg_get = client.get("/config")
        assert cfg_get.status_code == 200
        assert cfg_get.json()["has_cinevec_token"] is True
        assert "pica_initial_token_1234" not in str(cfg_get.json())
        assert cfg_get.json()["has_bot_token"] is False

        # Updating config without secret must preserve existing token; updating other fields persists to .env
        patched = client.patch(
            "/config",
            json={
                "tg_allowed_user_ids": [111, 222],
                "disk_min_free_gb": 5.0,
                "max_preprocess_inflight": 4,
            },
        )
        assert patched.status_code == 200
        assert patched.json()["config"]["has_cinevec_token"] is True
        assert patched.json()["config"]["tg_allowed_user_ids"] == [111, 222]
        assert patched.json()["config"]["disk_min_free_gb"] == 5.0
        assert env_file.is_file()
        env_text = env_file.read_text(encoding="utf-8")
        assert "TG_ALLOWED_USER_IDS=111,222" in env_text
        assert "MAX_PREPROCESS_INFLIGHT=4" in env_text

        # Search proxy rewrites locators and serves preview bytes safely
        sr = client.post("/search/text", json={"query": "runner", "top_n": 5})
        assert sr.status_code == 200
        item = sr.json()["items"][0]
        proxy_url = item["previews"][0]["locator"]
        assert proxy_url.startswith("/proxy/cinevec?path=")

        img = client.get(proxy_url)
        assert img.status_code == 200
        assert img.content == b"jpeg-frame-bytes"

        # SSRF guard blocks disallowed paths
        bad_proxy = client.get("/proxy/cinevec", params={"path": "http://169.254.169.254/latest"})
        assert bad_proxy.status_code == 400
        bad_traversal = client.get("/proxy/cinevec", params={"path": "/api/v1/previews/../secret"})
        assert bad_traversal.status_code == 400

        # Metadata search endpoint rewrites cover locators too
        md = client.post("/search/metadata", json={"query": "冲浪"})
        assert md.status_code == 200
        md_item = md.json()["items"][0]
        assert md_item["tags"] == ["冲浪"]
        assert md_item["cover_locator"].startswith("/proxy/cinevec?path=")
