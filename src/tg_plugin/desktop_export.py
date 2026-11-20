from __future__ import annotations

import asyncio
import json
import mimetypes
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from tg_plugin.config import PluginSettings
from tg_plugin.ingest_pipeline import IngestPipeline, build_telegram_link, matches_filter_rules
from tg_plugin.models import FilterRules, TelegramMediaEnvelope

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
_CHANNEL_SUPERGROUP_TYPES = {
    "public_channel",
    "private_channel",
    "public_supergroup",
    "private_supergroup",
}
_PERSONAL_TYPES = {"personal_chat", "saved_messages", "bot_chat"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_export_chat_id(raw_id: int, chat_type: str) -> int:
    """Convert Telegram Desktop export `id` into Telethon/Bot API signed `chat_id`."""
    cleaned_type = (chat_type or "").strip().lower()
    val = int(raw_id)
    if cleaned_type in _CHANNEL_SUPERGROUP_TYPES:
        raw_str = str(val)
        if raw_str.startswith("-100"):
            return val
        return int(f"-100{abs(val)}")
    if cleaned_type in _PERSONAL_TYPES:
        return abs(val)
    if cleaned_type == "private_group":
        return -abs(val)
    # Unknown type: preserve negative prefix if already present, otherwise treat like -100 channel/supergroup
    if val < 0:
        return val
    return int(f"-100{val}")


def flatten_export_text(raw_text: Any) -> str:
    """Flatten Telegram Desktop export `text` field (str or mixed entity list) into plain text."""
    if raw_text is None:
        return ""
    if isinstance(raw_text, str):
        return raw_text.strip()
    if isinstance(raw_text, list):
        parts: List[str] = []
        for item in raw_text:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
        return "".join(parts).strip()
    return str(raw_text).strip()


def _is_not_included_placeholder(raw_path: str) -> bool:
    low = (raw_path or "").strip().lower()
    return not low or "file not included" in low or "exceeds maximum size" in low


def _safe_export_file(export_dir: Path, raw_rel_path: str) -> Optional[Path]:
    rel = (raw_rel_path or "").strip()
    if _is_not_included_placeholder(rel):
        return None
    candidate_rel = Path(rel)
    if candidate_rel.is_absolute():
        return None
    root_resolved = export_dir.resolve()
    target = (root_resolved / candidate_rel).resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError:
        return None
    if not target.is_file():
        return None
    return target


def find_result_json_files(root_path: Path) -> List[Path]:
    """Locate one or more `result.json` files from a file, export directory, or watch parent folder."""
    if root_path.is_file():
        if root_path.name == "result.json":
            return [root_path]
        return []
    if not root_path.is_dir():
        return []
    direct = root_path / "result.json"
    if direct.is_file():
        return [direct]
    found = sorted(p for p in root_path.rglob("result.json") if p.is_file())
    return found


def _classify_message_media(
    msg: Dict[str, Any],
    export_dir: Path,
) -> Tuple[Optional[str], Optional[Path], str, bool]:
    """
    Inspect a Telegram Desktop message object.
    Returns `(kind, file_path, mime_type, is_missing_media)`.
    """
    photo_rel = msg.get("photo")
    if isinstance(photo_rel, str) and photo_rel.strip():
        resolved = _safe_export_file(export_dir, photo_rel)
        if resolved is None:
            return "image", None, "image/jpeg", True
        mime = mimetypes.guess_type(resolved.name)[0] or "image/jpeg"
        return "image", resolved, mime, False

    file_rel = msg.get("file")
    if isinstance(file_rel, str) and file_rel.strip():
        media_type = str(msg.get("media_type") or "").strip().lower()
        mime_hint = str(msg.get("mime_type") or "").strip().lower()
        # Skip non-visual Telegram attachments cleanly (stickers, audio, voice, generic docs)
        if media_type in {"sticker", "voice_message", "audio_file"}:
            return None, None, mime_hint, False

        ext = Path(file_rel).suffix.lower() if not _is_not_included_placeholder(file_rel) else ""
        inferred_kind: Optional[str] = None
        if media_type in {"video_file", "animation", "video_message"} or mime_hint.startswith("video/") or ext in _VIDEO_EXTS:
            inferred_kind = "video"
        elif mime_hint.startswith("image/") or ext in _IMAGE_EXTS:
            inferred_kind = "image"
        elif _is_not_included_placeholder(file_rel) and media_type in {"", "video_file", "animation"}:
            # Placeholder written when Video files wasn't ticked or exceeded size slider
            return "video", None, mime_hint or "video/mp4", True
        else:
            return None, None, mime_hint, False

        resolved = _safe_export_file(export_dir, file_rel)
        if resolved is None:
            return inferred_kind, None, mime_hint or ("video/mp4" if inferred_kind == "video" else "image/jpeg"), True
        mime = mime_hint or mimetypes.guess_type(resolved.name)[0] or (
            "video/mp4" if inferred_kind == "video" else "image/jpeg"
        )
        return inferred_kind, resolved, mime, False

    return None, None, "", False


def _parse_single_result_json(
    json_path: Path,
    *,
    chat_username: Optional[str] = None,
    default_tags: Optional[List[str]] = None,
    store_original: bool = False,
) -> Dict[str, Any]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    export_dir = json_path.parent
    raw_id = int(data.get("id") or 0)
    chat_type = str(data.get("type") or "").strip().lower()
    chat_title = str(data.get("name") or "").strip() or json_path.parent.name
    chat_id = normalize_export_chat_id(raw_id, chat_type) if raw_id else 0

    envelopes: List[TelegramMediaEnvelope] = []
    source_paths: Dict[Tuple[int, int], List[Path]] = {}
    images_count = 0
    videos_count = 0
    missing_media_count = 0
    total_bytes = 0
    sample_link: Optional[str] = None

    prev_sender_key: Optional[str] = None
    prev_msg_id: Optional[int] = None
    prev_ts: Optional[int] = None
    current_group_id: Optional[str] = None

    for msg in data.get("messages") or []:
        if not isinstance(msg, dict) or msg.get("type") != "message":
            continue
        msg_id = int(msg.get("id") or 0)
        if msg_id <= 0:
            continue
        kind, media_path, mime_type, is_missing = _classify_message_media(msg, export_dir)
        if kind is None:
            prev_sender_key = None
            prev_msg_id = None
            current_group_id = None
            continue
        if is_missing or media_path is None:
            missing_media_count += 1
            continue

        try:
            fsize = int(msg.get("file_size") or 0) or media_path.stat().st_size
        except OSError:
            missing_media_count += 1
            continue

        total_bytes += fsize
        if kind == "image":
            images_count += 1
        else:
            videos_count += 1

        try:
            ts_unix = int(msg.get("date_unixtime") or 0)
        except (TypeError, ValueError):
            ts_unix = 0
        sender_key = str(msg.get("from_id") or msg.get("from") or "__channel__")
        if (
            prev_sender_key == sender_key
            and prev_msg_id is not None
            and msg_id == prev_msg_id + 1
            and ts_unix > 0
            and prev_ts is not None
            and abs(ts_unix - prev_ts) <= 2
        ):
            if current_group_id is None:
                current_group_id = f"export_{chat_id}_{prev_msg_id}"
                if envelopes:
                    envelopes[-1].media_group_id = current_group_id
        else:
            current_group_id = None

        prev_sender_key = sender_key
        prev_msg_id = msg_id
        prev_ts = ts_unix if ts_unix > 0 else prev_ts

        duration_val: Optional[int] = None
        if kind == "video" and msg.get("duration_seconds") is not None:
            try:
                duration_val = int(msg["duration_seconds"])
            except (TypeError, ValueError):
                duration_val = None

        thumb_path: Optional[Path] = None
        thumb_rel = msg.get("thumbnail")
        if isinstance(thumb_rel, str) and thumb_rel.strip():
            thumb_path = _safe_export_file(export_dir, thumb_rel)

        async def _use_local_file(_dest: Path, *, _src: Path = media_path) -> Path:
            return _src

        cover_fn = None
        if thumb_path is not None:

            async def _read_local_thumb(*, _tp: Path = thumb_path) -> Optional[bytes]:
                try:
                    return _tp.read_bytes()
                except OSError:
                    return None

            cover_fn = _read_local_thumb

        if sample_link is None and chat_id != 0:
            sample_link = build_telegram_link(chat_id, msg_id, chat_username, chat_type)

        caption = flatten_export_text(msg.get("text"))
        env = TelegramMediaEnvelope(
            chat_id=chat_id,
            message_id=msg_id,
            kind=kind,
            filename=media_path.name,
            mime_type=mime_type,
            file_size=fsize,
            file_unique_id="",
            file_id="",
            media_group_id=current_group_id,
            chat_username=chat_username,
            chat_title=chat_title,
            chat_type=chat_type,
            caption=caption,
            duration=duration_val,
            source_id=None,
            store_original=store_original,
            default_tags=list(default_tags or []),
            download_fn=_use_local_file,
            cover_download_fn=cover_fn,
        )
        envelopes.append(env)
        paths_for_msg = [media_path]
        if thumb_path is not None:
            paths_for_msg.append(thumb_path)
        source_paths[(chat_id, msg_id)] = paths_for_msg

    return {
        "json_path": str(json_path),
        "chat_id": chat_id,
        "chat_title": chat_title,
        "chat_type": chat_type,
        "sample_link": sample_link,
        "images": images_count,
        "videos": videos_count,
        "missing_media": missing_media_count,
        "total_bytes": total_bytes,
        "envelopes": envelopes,
        "source_paths": source_paths,
    }


class DesktopExportImporter:
    """Parses Telegram Desktop JSON exports (`result.json` + local media) and feeds `IngestPipeline`."""

    def __init__(self, settings: PluginSettings, pipeline: IngestPipeline) -> None:
        self._settings = settings
        self._pipeline = pipeline
        self._task: Optional[asyncio.Task] = None
        self._processed_watch_sigs: Dict[str, Tuple[int, int]] = {}
        self._status: Dict[str, Any] = {
            "running": False,
            "current_path": None,
            "started_at": None,
            "finished_at": None,
            "scanned_files": 0,
            "accepted": 0,
            "uploaded": 0,
            "skipped": 0,
            "missing_media": 0,
            "failed": 0,
            "deleted_files": 0,
            "batch_ids": [],
            "last_error": None,
            "history": [],
        }

    def update_settings(self, settings: PluginSettings) -> None:
        self._settings = settings

    @property
    def status(self) -> Dict[str, Any]:
        return dict(self._status)

    def preview(self, path: str, *, chat_username: Optional[str] = None) -> Dict[str, Any]:
        target = Path(path).expanduser()
        files = find_result_json_files(target)
        if not files:
            raise FileNotFoundError(f"未在指定路径找到 result.json：{target}")
        exports: List[Dict[str, Any]] = []
        total_images = 0
        total_videos = 0
        total_missing = 0
        total_bytes = 0
        for jp in files:
            parsed = _parse_single_result_json(jp, chat_username=chat_username)
            total_images += parsed["images"]
            total_videos += parsed["videos"]
            total_missing += parsed["missing_media"]
            total_bytes += parsed["total_bytes"]
            exports.append(
                {
                    "json_path": parsed["json_path"],
                    "chat_id": parsed["chat_id"],
                    "chat_title": parsed["chat_title"],
                    "chat_type": parsed["chat_type"],
                    "sample_link": parsed["sample_link"],
                    "images": parsed["images"],
                    "videos": parsed["videos"],
                    "missing_media": parsed["missing_media"],
                    "total_bytes": parsed["total_bytes"],
                }
            )
        warnings: List[str] = []
        if total_missing > 0:
            warnings.append(
                f"发现 {total_missing} 条消息未包含本地媒体文件（导出时可能未勾选 Photos / Video files 或超出单文件体积滑块限制）。"
            )
        return {
            "path": str(target),
            "export_count": len(exports),
            "exports": exports,
            "images": total_images,
            "videos": total_videos,
            "missing_media": total_missing,
            "total_bytes": total_bytes,
            "warnings": warnings,
        }

    async def import_path(
        self,
        path: str,
        *,
        chat_username: Optional[str] = None,
        default_tags: Optional[List[str]] = None,
        store_original: bool = False,
        delete_after_upload: bool = False,
        filter_rules: Optional[FilterRules] = None,
        stop_event: Optional[asyncio.Event] = None,
    ) -> Dict[str, Any]:
        target = Path(path).expanduser()
        files = find_result_json_files(target)
        if not files:
            raise FileNotFoundError(f"未在指定路径找到 result.json：{target}")

        rules_dict = filter_rules.model_dump() if filter_rules is not None else None
        self._status.update(
            {
                "running": True,
                "current_path": str(target),
                "started_at": _now_iso(),
                "finished_at": None,
                "scanned_files": len(files),
                "accepted": 0,
                "uploaded": 0,
                "skipped": 0,
                "missing_media": 0,
                "failed": 0,
                "deleted_files": 0,
                "batch_ids": [],
                "last_error": None,
            }
        )
        exports_summary: List[Dict[str, Any]] = []
        try:
            for jp in files:
                if stop_event is not None and stop_event.is_set():
                    break
                parsed = _parse_single_result_json(
                    jp,
                    chat_username=chat_username,
                    default_tags=default_tags,
                    store_original=store_original,
                )
                envelopes: List[TelegramMediaEnvelope] = parsed["envelopes"]
                filtered_out = 0
                if rules_dict:
                    kept_envs: List[TelegramMediaEnvelope] = []
                    for env in envelopes:
                        ok, _ = matches_filter_rules(env, rules_dict)
                        if ok:
                            kept_envs.append(env)
                        else:
                            filtered_out += 1
                    envelopes = kept_envs

                res = await self._pipeline.ingest_batch(envelopes, stop_event=stop_event)
                uploaded_rows = res.get("uploaded") or []
                deleted_here = 0
                if delete_after_upload and uploaded_rows:
                    seen_paths: Set[Path] = set()
                    for row in uploaded_rows:
                        cid = int(row.get("chat_id") or 0)
                        mid = int(row.get("message_id") or 0)
                        for p in parsed["source_paths"].get((cid, mid), []):
                            if p in seen_paths:
                                continue
                            seen_paths.add(p)
                            try:
                                if p.is_file():
                                    p.unlink()
                                    deleted_here += 1
                            except OSError:
                                pass

                self._status["accepted"] += int(res.get("accepted") or 0)
                self._status["uploaded"] += len(uploaded_rows)
                self._status["skipped"] += len(res.get("skipped") or []) + filtered_out
                self._status["missing_media"] += int(parsed["missing_media"])
                self._status["failed"] += len(res.get("failed") or [])
                self._status["deleted_files"] += deleted_here
                for bid in res.get("batch_ids") or []:
                    if bid and bid not in self._status["batch_ids"]:
                        self._status["batch_ids"].append(bid)

                exports_summary.append(
                    {
                        "json_path": parsed["json_path"],
                        "chat_id": parsed["chat_id"],
                        "chat_title": parsed["chat_title"],
                        "chat_type": parsed["chat_type"],
                        "sample_link": parsed["sample_link"],
                        "accepted": int(res.get("accepted") or 0),
                        "uploaded": len(uploaded_rows),
                        "skipped": len(res.get("skipped") or []) + filtered_out,
                        "missing_media": int(parsed["missing_media"]),
                        "failed": len(res.get("failed") or []),
                        "deleted_files": deleted_here,
                    }
                )
        except Exception as exc:
            self._status["last_error"] = str(exc)
            raise
        finally:
            self._status["running"] = False
            self._status["finished_at"] = _now_iso()

        entry = {
            "path": str(target),
            "finished_at": self._status["finished_at"],
            "uploaded": self._status["uploaded"],
            "skipped": self._status["skipped"],
            "missing_media": self._status["missing_media"],
            "failed": self._status["failed"],
            "deleted_files": self._status["deleted_files"],
        }
        history = list(self._status.get("history") or [])
        history.insert(0, entry)
        self._status["history"] = history[:20]
        return {"status": dict(self._status), "exports": exports_summary}

    def start_background_import(
        self,
        path: str,
        *,
        chat_username: Optional[str] = None,
        default_tags: Optional[List[str]] = None,
        store_original: bool = False,
        delete_after_upload: bool = False,
        filter_rules: Optional[FilterRules] = None,
        stop_event: Optional[asyncio.Event] = None,
    ) -> bool:
        if self._task is not None and not self._task.done():
            return False

        async def _runner() -> None:
            try:
                await self.import_path(
                    path,
                    chat_username=chat_username,
                    default_tags=default_tags,
                    store_original=store_original,
                    delete_after_upload=delete_after_upload,
                    filter_rules=filter_rules,
                    stop_event=stop_event,
                )
            except Exception as exc:
                self._status["last_error"] = str(exc)

        self._task = asyncio.create_task(_runner())
        return True

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def scan_watch_dir_once(
        self,
        *,
        stop_event: Optional[asyncio.Event] = None,
        min_age_seconds: float = 10.0,
    ) -> Optional[Dict[str, Any]]:
        watch_raw = (self._settings.export_watch_dir or "").strip()
        if not watch_raw or self._status.get("running"):
            return None
        watch_dir = Path(watch_raw).expanduser()
        if not watch_dir.is_dir():
            return None

        now_sec = time.time()
        last_result: Optional[Dict[str, Any]] = None
        for jp in find_result_json_files(watch_dir):
            if stop_event is not None and stop_event.is_set():
                break
            try:
                st = jp.stat()
            except OSError:
                continue
            if (now_sec - st.st_mtime) < min_age_seconds:
                continue
            key = str(jp.resolve())
            sig = (st.st_mtime_ns, st.st_size)
            if self._processed_watch_sigs.get(key) == sig:
                continue
            res = await self.import_path(
                str(jp),
                delete_after_upload=bool(self._settings.export_delete_after_upload),
                stop_event=stop_event,
            )
            self._processed_watch_sigs[key] = sig
            last_result = res
        return last_result
