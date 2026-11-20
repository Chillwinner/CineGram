from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol

from pydantic import BaseModel, Field


@dataclass
class TelegramMediaEnvelope:
    chat_id: int
    message_id: int
    kind: str  # "image" | "video"
    filename: str
    mime_type: str = "application/octet-stream"
    file_size: int = 0
    file_unique_id: str = ""
    file_id: str = ""
    media_group_id: Optional[str] = None
    chat_username: Optional[str] = None
    chat_title: Optional[str] = None
    caption: str = ""
    duration: Optional[int] = None
    source_id: Optional[int] = None
    store_original: bool = False
    backup_chat_id: Optional[int] = None
    default_tags: List[str] = field(default_factory=list)
    download_fn: Optional[Callable[[Path], Awaitable[Path]]] = None
    cover_download_fn: Optional[Callable[[], Awaitable[Optional[bytes]]]] = None
    backup_copy_fn: Optional[Callable[[int], Awaitable[Optional[str]]]] = None


@dataclass
class ChatProbeResult:
    accessible: bool
    chat_id: Optional[int] = None
    username: Optional[str] = None
    title: Optional[str] = None
    chat_type: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class MessageProbeResult:
    message_id: int
    exists: bool
    reason: Optional[str] = None


class TelegramInspector(Protocol):
    async def probe_chat(self, source: Dict[str, Any]) -> ChatProbeResult: ...
    async def probe_messages(self, chat_id: int, message_ids: List[int]) -> Dict[int, MessageProbeResult]: ...


class FilterRules(BaseModel):
    media_kinds: List[str] = Field(default_factory=list)  # e.g. ["image", "video"]; empty = all
    min_file_mb: Optional[float] = None
    max_file_mb: Optional[float] = None
    min_duration_sec: Optional[int] = None
    include_keywords: List[str] = Field(default_factory=list)
    exclude_keywords: List[str] = Field(default_factory=list)
    backfill_days: Optional[int] = None


class SourceCreateIn(BaseModel):
    chat_ref: str
    chat_id: Optional[int] = None
    chat_username: Optional[str] = None
    chat_title: Optional[str] = None
    chat_type: Optional[str] = None
    enabled: bool = False
    store_original: bool = False
    backup_chat_id: Optional[int] = None
    default_tags: List[str] = Field(default_factory=list)
    max_file_mb: Optional[int] = None
    check_interval_hours: Optional[float] = None
    filter_rules: Optional[FilterRules] = None


class SourcePatchIn(BaseModel):
    enabled: Optional[bool] = None
    store_original: Optional[bool] = None
    backup_chat_id: Optional[int] = None
    default_tags: Optional[List[str]] = None
    max_file_mb: Optional[int] = None
    check_interval_hours: Optional[float] = None
    chat_title: Optional[str] = None
    chat_username: Optional[str] = None
    filter_rules: Optional[FilterRules] = None


class BackfillIn(BaseModel):
    limit: Optional[int] = None
    days: Optional[int] = None


class ImportSelectedIn(BaseModel):
    message_ids: List[int] = Field(default_factory=list)


class CheckRunIn(BaseModel):
    source_id: Optional[int] = None


class ConfigPatchIn(BaseModel):
    cinevec_base_url: Optional[str] = None
    cinevec_api_token: Optional[str] = None
    tg_bot_token: Optional[str] = None
    tg_allowed_user_ids: Optional[List[int]] = None
    tg_api_id: Optional[int] = None
    tg_api_hash: Optional[str] = None
    tg_session_name: Optional[str] = None
    disk_min_free_gb: Optional[float] = None
    max_preprocess_inflight: Optional[int] = None
    max_file_size_mb: Optional[int] = None
    default_check_interval_hours: Optional[float] = None
    auto_exclude_after_days: Optional[int] = None
    clear_bot_token: bool = False
    clear_telethon: bool = False


class QuickSearchIn(BaseModel):
    query: str
    top_n: int = 8


_TEXT_SEARCH_RE = re.compile(r"^/s(?:@[A-Za-z0-9_]+)?[ \t]+(.+)$", re.IGNORECASE | re.DOTALL)
_PHOTO_SEARCH_RE = re.compile(r"^/s(?:@[A-Za-z0-9_]+)?(?:$|[ \t]+.*)$", re.IGNORECASE | re.DOTALL)


def parse_text_search_query(text: Optional[str]) -> Optional[str]:
    """Require `/s <query>` (with at least one space/tab after `/s`) for text search."""
    raw = (text or "").lstrip()
    m = _TEXT_SEARCH_RE.match(raw)
    if not m:
        return None
    query = m.group(1).strip()
    return query or None


def is_photo_search_caption(text: Optional[str]) -> bool:
    """Match `/s` or `/s ...` when used as a photo caption or reply to a photo."""
    raw = (text or "").strip()
    return bool(_PHOTO_SEARCH_RE.match(raw))


def format_search_hit_caption(item: Dict[str, Any], *, include_source_url: bool = False) -> str:
    """Format a CineVec search hit into a Telegram message caption."""
    aid = item.get("asset_id") or item.get("id")
    name = item.get("display_name") or aid
    score = item.get("score")
    score_str = f"{score:.3f}" if isinstance(score, (int, float)) else "-"
    previews = item.get("previews") or []
    first_prev = previews[0] if previews else {}
    ts = first_prev.get("time_seconds")
    ts_str = f" · {int(ts)}s" if isinstance(ts, (int, float)) and ts > 0 else ""
    lc = item.get("lifecycle_label") or ""
    caption = f"{name}\n相似度: {score_str}{ts_str}"
    if lc and item.get("lifecycle") not in (None, "active"):
        caption += f"\n状态: {lc}"
    if item.get("description"):
        caption += f"\n{item['description'][:120]}"
    if include_source_url:
        src_url = item.get("source_url") or ""
        if src_url.startswith(("http://", "https://")):
            caption += f"\n原消息: {src_url}"
    return caption
