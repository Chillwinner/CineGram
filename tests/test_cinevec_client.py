from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

from tg_plugin.cinevec_client import CineVecClient, rewrite_v1_locator


@pytest.mark.asyncio
async def test_cinevec_client_bearer_upload_batch_patch_and_locator_rewrite(tmp_path: Path):
    seen: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": dict(request.url.params),
                "auth": request.headers.get("authorization"),
                "content": request.content,
            }
        )
        if request.url.path == "/api/v1/imports" and request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "batch_id": "b-123",
                    "assets": [{"id": "asset-1", "ref": {"ref_id": "tg_-1001_42"}, "lifecycle": "active"}],
                    "jobs": [{"id": "job-1", "phase": "queued"}],
                },
            )
        if request.url.path == "/api/v1/batches/b-123":
            return httpx.Response(
                200,
                json={"batch_id": "b-123", "items": [], "counts": {"queued": 0, "preprocess": 0, "searchable": 1}},
            )
        if request.url.path == "/api/v1/assets" and request.method == "GET":
            return httpx.Response(200, json={"items": [{"id": "asset-1"}], "total": 1})
        if request.url.path == "/api/v1/assets/asset-1" and request.method == "PATCH":
            return httpx.Response(200, json={"id": "asset-1", "lifecycle": "source_invalid"})
        if request.url.path == "/api/v1/previews/asset-1/frames/0":
            return httpx.Response(200, content=b"jpeg-preview")
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "not found"}})

    sample = tmp_path / "clip.mp4"
    sample.write_bytes(b"fake-video-payload")

    client = CineVecClient(
        "http://127.0.0.1:8008",
        "pica_secret_tok",
        transport=httpx.MockTransport(handler),
    )
    try:
        imp = await client.import_media(
            [
                {
                    "file_path": sample,
                    "filename": "clip.mp4",
                    "mime_type": "video/mp4",
                    "cover_bytes": b"cover-bytes",
                    "meta": {
                        "relative_path": "tg_-1001_42_clip.mp4",
                        "display_name": "Clip 42",
                        "store_original": False,
                        "vectorize": True,
                        "tags": ["action", "outdoor"],
                        "source_url": "https://t.me/c/1/42",
                        "ref": {"ref_id": "tg_-1001_42", "locator": "https://t.me/c/1/42"},
                    },
                }
            ]
        )
        assert imp["batch_id"] == "b-123"
        assert seen[0]["auth"] == "Bearer pica_secret_tok"
        assert b"Clip 42" in seen[0]["content"]
        assert b"cover-bytes" in seen[0]["content"]

        batch = await client.get_batch("b-123")
        assert batch["counts"]["searchable"] == 1

        found = await client.find_asset_by_ref_id("tg_-1001_42")
        assert found is not None and found["id"] == "asset-1"
        assert seen[2]["query"]["ref_id"] == "tg_-1001_42"

        patched = await client.patch_asset("asset-1", lifecycle="source_invalid")
        assert patched["lifecycle"] == "source_invalid"
        assert json.loads(seen[3]["content"])["lifecycle"] == "source_invalid"

        assert rewrite_v1_locator("/api/previews/asset-1/frames/0") == "/api/v1/previews/asset-1/frames/0"
        assert rewrite_v1_locator("/api/assets/asset-1/cover?v=1") == "/api/v1/assets/asset-1/cover?v=1"
        preview = await client.fetch_asset_bytes("/api/previews/asset-1/frames/0")
        assert preview == b"jpeg-preview"
    finally:
        await client.close()
