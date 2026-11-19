from __future__ import annotations

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
