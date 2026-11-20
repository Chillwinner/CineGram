from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx


class CineVecAPIError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(f"[{status_code}] {code}: {message}")


def rewrite_v1_locator(locator: str) -> str:
    if locator.startswith("/api/previews/"):
        return "/api/v1/previews/" + locator[len("/api/previews/"):]
    if locator.startswith("/api/assets/"):
        return "/api/v1/assets/" + locator[len("/api/assets/"):]
    if locator.startswith("/api/faces/"):
        return "/api/v1/faces/" + locator[len("/api/faces/"):]
    return locator


class CineVecClient:
    def __init__(
        self,
        base_url: str,
        api_token: str,
        *,
        timeout: float = 60.0,
        upload_timeout: float = 600.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token.strip()
        self._timeout = timeout
        self._upload_timeout = upload_timeout
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None

    def _headers(self) -> Dict[str, str]:
        if not self._api_token:
            return {}
        return {"Authorization": f"Bearer {self._api_token}"}

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=self._headers(),
                timeout=self._timeout,
                transport=self._transport,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def update_credentials(self, base_url: str, api_token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token.strip()
        await self.close()

    def _raise_for_error(self, resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        code = "http_error"
        message = resp.text
        try:
            body = resp.json()
            if isinstance(body, dict) and isinstance(body.get("error"), dict):
                code = str(body["error"].get("code") or code)
                message = str(body["error"].get("message") or message)
        except Exception:
            pass
        raise CineVecAPIError(resp.status_code, code, message)

    async def health(self) -> Dict[str, Any]:
        client = self._ensure_client()
        resp = await client.get("/api/v1/health")
        self._raise_for_error(resp)
        return resp.json()

    async def import_media(
        self,
        items: List[Dict[str, Any]],
        *,
        batch_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Stream media files from disk into POST /api/v1/imports.
        Each item expects:
          - file_path: Path
          - filename: str
          - mime_type: str
          - meta: dict (relative_path, store_original, vectorize, description, source_url, display_name, tags, ref)
          - cover_bytes: Optional[bytes]
        """
        client = self._ensure_client()
        meta_list: List[Dict[str, Any]] = []
        with contextlib.ExitStack() as stack:
            files_payload: List[Any] = []
            for idx, item in enumerate(items):
                file_path = Path(item["file_path"])
                filename = str(item.get("filename") or file_path.name)
                mime_type = str(item.get("mime_type") or "application/octet-stream")
                meta = dict(item["meta"])
                rel = str(meta.get("relative_path") or filename)
                meta["relative_path"] = rel

                fh = stack.enter_context(file_path.open("rb"))
                files_payload.append(("files", (filename, fh, mime_type)))

                cover_bytes = item.get("cover_bytes")
                if cover_bytes:
                    cov_rel = f"__covers__/{idx}_{Path(rel).stem}.jpg"
                    meta["cover_relative_path"] = cov_rel
                    meta_list.append(meta)
                    meta_list.append({"relative_path": cov_rel, "is_cover": True})
                    files_payload.append(("files", (Path(cov_rel).name, cover_bytes, "image/jpeg")))
                else:
                    meta_list.append(meta)

            data: Dict[str, str] = {"meta": json.dumps(meta_list, ensure_ascii=False)}
            if batch_id:
                data["batch_id"] = batch_id

            resp = await client.post(
                "/api/v1/imports",
                data=data,
                files=files_payload,
                timeout=self._upload_timeout,
            )
        self._raise_for_error(resp)
        return resp.json()

    async def get_batch(self, batch_id: str) -> Dict[str, Any]:
        client = self._ensure_client()
        resp = await client.get(f"/api/v1/batches/{batch_id}")
        self._raise_for_error(resp)
        return resp.json()

    async def get_job(self, job_id: str) -> Dict[str, Any]:
        client = self._ensure_client()
        resp = await client.get(f"/api/v1/jobs/{job_id}")
        self._raise_for_error(resp)
        return resp.json()

    async def find_asset_by_ref_id(self, ref_id: str) -> Optional[Dict[str, Any]]:
        client = self._ensure_client()
        resp = await client.get("/api/v1/assets", params={"ref_id": ref_id, "excluded": "false", "limit": 5})
        self._raise_for_error(resp)
        items = resp.json().get("items") or []
        return items[0] if items else None

    async def patch_asset(
        self,
        asset_id: str,
        *,
        description: Optional[str] = None,
        display_name: Optional[str] = None,
        source_url: Optional[str] = None,
        tags: Optional[List[str]] = None,
        lifecycle: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if description is not None:
            payload["description"] = description
        if display_name is not None:
            payload["display_name"] = display_name
        if source_url is not None:
            payload["source_url"] = source_url
        if tags is not None:
            payload["tags"] = tags
        if lifecycle is not None:
            payload["lifecycle"] = lifecycle
        client = self._ensure_client()
        resp = await client.patch(f"/api/v1/assets/{asset_id}", json=payload)
        self._raise_for_error(resp)
        return resp.json()

    async def exclude_asset(self, asset_id: str) -> Dict[str, Any]:
        client = self._ensure_client()
        resp = await client.post(f"/api/v1/assets/{asset_id}/exclude")
        self._raise_for_error(resp)
        return resp.json()

    async def restore_asset(self, asset_id: str) -> Dict[str, Any]:
        client = self._ensure_client()
        resp = await client.post(f"/api/v1/assets/{asset_id}/restore")
        self._raise_for_error(resp)
        return resp.json()

    async def search_text(
        self,
        query: str,
        *,
        top_n: Optional[int] = None,
        score_threshold: Optional[float] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"query": query}
        if top_n is not None:
            body["top_n"] = top_n
        if score_threshold is not None:
            body["score_threshold"] = score_threshold
        client = self._ensure_client()
        resp = await client.post("/api/v1/search/text", json=body)
        self._raise_for_error(resp)
        return resp.json()

    async def search_image(
        self,
        image_bytes: bytes,
        *,
        filename: str = "query.jpg",
        top_n: Optional[int] = None,
        score_threshold: Optional[float] = None,
    ) -> Dict[str, Any]:
        data: Dict[str, str] = {}
        if top_n is not None:
            data["top_n"] = str(top_n)
        if score_threshold is not None:
            data["score_threshold"] = str(score_threshold)
        client = self._ensure_client()
        resp = await client.post(
            "/api/v1/search/image",
            data=data,
            files={"image": (filename, image_bytes, "image/jpeg")},
        )
        self._raise_for_error(resp)
        return resp.json()

    async def search_face(
        self,
        image_bytes: bytes,
        *,
        filename: str = "face.jpg",
        face_index: Optional[int] = None,
        top_n: Optional[int] = None,
        score_threshold: Optional[float] = None,
    ) -> Dict[str, Any]:
        data: Dict[str, str] = {}
        if face_index is not None:
            data["face_index"] = str(face_index)
        if top_n is not None:
            data["top_n"] = str(top_n)
        if score_threshold is not None:
            data["score_threshold"] = str(score_threshold)
        client = self._ensure_client()
        resp = await client.post(
            "/api/v1/search/face",
            data=data,
            files={"image": (filename, image_bytes, "image/jpeg")},
        )
        self._raise_for_error(resp)
        return resp.json()

    async def search_metadata(self, query: str) -> Dict[str, Any]:
        client = self._ensure_client()
        resp = await client.get("/api/v1/search/metadata", params={"q": query})
        self._raise_for_error(resp)
        return resp.json()

    async def fetch_asset_bytes(self, locator_path: str) -> bytes:
        path = rewrite_v1_locator(locator_path)
        client = self._ensure_client()
        resp = await client.get(path)
        self._raise_for_error(resp)
        return resp.content
