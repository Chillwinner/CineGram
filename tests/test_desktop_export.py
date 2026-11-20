from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest
from fastapi.testclient import TestClient

from tg_plugin.app import create_plugin_app
from tg_plugin.backpressure import BackpressureController
from tg_plugin.cinevec_client import CineVecClient
from tg_plugin.config import PluginSettings
from tg_plugin.desktop_export import (
    DesktopExportImporter,
    flatten_export_text,
    normalize_export_chat_id,
)
from tg_plugin.ingest_pipeline import IngestPipeline, build_telegram_link
from tg_plugin.state_store import StateStore


def test_normalize_chat_id_and_links():
    # Supergroups & channels map to -100... and https://t.me/c/<id>/<msg>
    cid_priv = normalize_export_chat_id(1987654321, "private_supergroup")
    assert cid_priv == -1001987654321
    assert build_telegram_link(cid_priv, 42, chat_type="private_supergroup") == "https://t.me/c/1987654321/42"

    cid_pub = normalize_export_chat_id(1234567890, "public_channel")
    assert cid_pub == -1001234567890
    assert build_telegram_link(cid_pub, 7, chat_username="@movie_clips", chat_type="public_channel") == "https://t.me/movie_clips/7"

    # Saved messages / personal chats map to positive user_id and tg://openmessage?user_id=...
    cid_pm = normalize_export_chat_id(55667788, "saved_messages")
    assert cid_pm == 55667788
    assert build_telegram_link(cid_pm, 99, chat_type="saved_messages") == "tg://openmessage?user_id=55667788&message_id=99"

    # Legacy small groups map to negative id and tg://openmessage?chat_id=...
    cid_grp = normalize_export_chat_id(443322, "private_group")
    assert cid_grp == -443322
    assert build_telegram_link(cid_grp, 12, chat_type="private_group") == "tg://openmessage?chat_id=-443322&message_id=12"


def test_flatten_export_text():
    raw = [
        "Ocean sunset ",
        {"type": "hashtag", "text": "#sunset"},
        " ",
        {"type": "bold", "text": "4K clip"},
    ]
    assert flatten_export_text(raw) == "Ocean sunset #sunset 4K clip"
    assert flatten_export_text("plain caption") == "plain caption"
    assert flatten_export_text(None) == ""


def test_export_preview_import_dedup_and_cleanup(tmp_path: Path):
    export_dir = tmp_path / "ChatExport_2026-11-20"
    (export_dir / "photos").mkdir(parents=True)
    (export_dir / "video_files").mkdir(parents=True)

    photo1 = export_dir / "photos" / "photo_1.jpg"
    photo2 = export_dir / "photos" / "photo_2.jpg"
    video1 = export_dir / "video_files" / "clip_3.mp4"
    thumb1 = export_dir / "video_files" / "clip_3.jpg"
    photo1.write_bytes(b"jpg-bytes-1")
    photo2.write_bytes(b"jpg-bytes-2")
    video1.write_bytes(b"mp4-bytes-3")
    thumb1.write_bytes(b"thumb-bytes-3")

    # Outside secret file to ensure '..' traversal is blocked cleanly
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"do-not-read")

    result_payload = {
        "name": "Cine Archive Group",
        "type": "private_supergroup",
        "id": 2233445566,
        "messages": [
            {
                "id": 101,
                "type": "message",
                "date_unixtime": "1732000000",
                "from_id": "user1",
                "photo": "photos/photo_1.jpg",
                "text": ["Hero scene ", {"type": "hashtag", "text": "#action"}],
            },
            {
                # Second album photo right after 101 -> should inherit caption + #action
                "id": 102,
                "type": "message",
                "date_unixtime": "1732000001",
                "from_id": "user1",
                "photo": "photos/photo_2.jpg",
                "text": "",
            },
            {
                "id": 103,
                "type": "message",
                "date_unixtime": "1732000010",
                "from_id": "user1",
                "file": "video_files/clip_3.mp4",
                "thumbnail": "video_files/clip_3.jpg",
                "media_type": "video_file",
                "mime_type": "video/mp4",
                "duration_seconds": 18,
                "text": "City flyover",
            },
            {
                # Missing placeholder from user forgetting slider or Photos checkbox
                "id": 104,
                "type": "message",
                "date_unixtime": "1732000020",
                "from_id": "user1",
                "file": "(File exceeds maximum size. Change data exporting settings to download.)",
                "media_type": "video_file",
                "text": "",
            },
            {
                # Path traversal attempt must be rejected safely
                "id": 105,
                "type": "message",
                "date_unixtime": "1732000030",
                "from_id": "user1",
                "photo": "../secret.jpg",
                "text": "traversal",
            },
        ],
    }
    (export_dir / "result.json").write_text(json.dumps(result_payload, ensure_ascii=False), encoding="utf-8")

    imported_meta_batches: List[List[Dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/v1/assets/search-ref":
            return httpx.Response(200, json={"items": []})
        if request.url.path == "/api/v1/imports":
            raw_text = request.content.decode("latin1", errors="ignore")
            # Extract JSON array field `meta` from multipart body
            import re

            m = re.search(r'name="meta"\r\n\r\n(\[.*?\])\r\n--', raw_text, re.DOTALL)
            assert m is not None
            meta_list = json.loads(m.group(1))
            imported_meta_batches.append(meta_list)
            assets = []
            idx = 0
            for item in meta_list:
                if item.get("is_cover"):
                    continue
                idx += 1
                assets.append(
                    {
                        "id": f"asset-{idx}",
                        "ref": item.get("ref"),
                        "lifecycle": "active",
                    }
                )
            return httpx.Response(200, json={"batch_id": f"batch-{len(imported_meta_batches)}", "assets": assets})
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
        transport=httpx.MockTransport(handler),
    )
    app = create_plugin_app(settings=settings, cinevec=cinevec, start_background=False)

    with TestClient(app) as client:
        # 1) Preview endpoint checks
        prev = client.post("/exports/preview", json={"path": str(export_dir)})
        assert prev.status_code == 200, prev.text
        pdata = prev.json()
        assert pdata["export_count"] == 1
        assert pdata["images"] == 2
        assert pdata["videos"] == 1
        assert pdata["missing_media"] == 2
        assert pdata["exports"][0]["chat_id"] == -1002233445566
        assert pdata["exports"][0]["sample_link"] == "https://t.me/c/2233445566/101"
        assert pdata["warnings"]

        # 2) Import with delete_after_upload=True
        imp = client.post(
            "/exports/import",
            json={
                "path": str(export_dir),
                "default_tags": ["vip"],
                "delete_after_upload": True,
                "sync": True,
            },
        )
        assert imp.status_code == 200, imp.text
        idata = imp.json()
        assert idata["status"]["uploaded"] == 3
        assert idata["status"]["missing_media"] == 2
        assert idata["status"]["deleted_files"] == 4  # 2 photos + 1 video + 1 thumb

        # Uploaded files removed, result.json preserved, outside secret untouched
        assert (export_dir / "result.json").is_file()
        assert not photo1.exists() and not photo2.exists()
        assert not video1.exists() and not thumb1.exists()
        assert outside.exists()

        # Verify uploaded metadata preserves t.me/c links & album caption inheritance
        real_items = [m for m in imported_meta_batches[0] if not m.get("is_cover")]
        assert len(real_items) == 3
        by_ref = {m["ref"]["ref_id"]: m for m in real_items}
        m101 = by_ref["tg_-1002233445566_101"]
        m102 = by_ref["tg_-1002233445566_102"]
        m103 = by_ref["tg_-1002233445566_103"]
        assert m101["source_url"] == "https://t.me/c/2233445566/101"
        assert m102["source_url"] == "https://t.me/c/2233445566/102"
        assert m103["source_url"] == "https://t.me/c/2233445566/103"
        assert m102["description"] == "Hero scene #action"
        assert "action" in m102["tags"] and "vip" in m102["tags"]
        assert m103.get("cover_relative_path")

        # 3) Save watch dir settings via PATCH /config
        patch = client.patch(
            "/config",
            json={"export_watch_dir": str(tmp_path), "export_delete_after_upload": True},
        )
        assert patch.status_code == 200
        assert patch.json()["config"]["export_watch_dir"] == str(tmp_path)
        assert patch.json()["config"]["export_delete_after_upload"] is True


@pytest.mark.asyncio
async def test_watch_dir_auto_scan_idempotent(tmp_path: Path):
    watch_root = tmp_path / "watch"
    sub = watch_root / "ChatExport_chan"
    (sub / "photos").mkdir(parents=True)
    img = sub / "photos" / "p1.jpg"
    img.write_bytes(b"img-bytes")

    payload = {
        "name": "WatchChan",
        "type": "public_channel",
        "id": 99887766,
        "messages": [
            {
                "id": 5,
                "type": "message",
                "date_unixtime": "1732000100",
                "photo": "photos/p1.jpg",
                "text": "First drop",
            }
        ],
    }
    rj = sub / "result.json"
    rj.write_text(json.dumps(payload), encoding="utf-8")
    old_ts = rj.stat().st_mtime - 60
    os.utime(rj, (old_ts, old_ts))

    uploads: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/assets/search-ref":
            return httpx.Response(200, json={"items": []})
        if request.url.path == "/api/v1/imports":
            uploads.append("ok")
            return httpx.Response(
                200,
                json={"batch_id": "b-watch", "assets": [{"id": "aw1", "ref": {"ref_id": "tg_-10099887766_5"}, "lifecycle": "active"}]},
            )
        return httpx.Response(200, json={})

    settings = PluginSettings(
        cinevec_base_url="http://127.0.0.1:8008",
        cinevec_api_token="pica_w",
        plugin_data_dir=tmp_path / "data",
        disk_min_free_gb=0.001,
        export_watch_dir=str(watch_root),
        export_delete_after_upload=True,
    )
    store = StateStore(settings.db_path)
    await store.init()
    cinevec = CineVecClient(settings.cinevec_base_url, settings.cinevec_api_token, transport=httpx.MockTransport(handler))
    try:
        bp = BackpressureController(settings.tmp_dir, cinevec, disk_min_free_gb=0.001)
        pipeline = IngestPipeline(settings, store, cinevec, bp)
        importer = DesktopExportImporter(settings, pipeline)

        first = await importer.scan_watch_dir_once(min_age_seconds=5.0)
        assert first is not None
        assert first["status"]["uploaded"] == 1
        assert len(uploads) == 1
        assert not img.exists()

        # Second scan skips unchanged result.json cleanly
        second = await importer.scan_watch_dir_once(min_age_seconds=5.0)
        assert second is None
        assert len(uploads) == 1
    finally:
        await cinevec.close()
        await store.close()
