from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field


def mask_secret(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) <= 8:
        return "***"
    return f"{cleaned[:4]}...{cleaned[-4:]}"


def save_env_updates(env_path: Path, updates: Dict[str, str]) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    if env_path.is_file():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    remaining = dict(updates)
    new_lines: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            val = remaining.pop(key)
            new_lines.append(f"{key}={val}")
        else:
            new_lines.append(line)
    for key, val in remaining.items():
        new_lines.append(f"{key}={val}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _parse_int_list(raw: Optional[str]) -> List[int]:
    if not raw:
        return []
    out: List[int] = []
    for part in str(raw).split(","):
        cleaned = part.strip()
        if not cleaned:
            continue
        try:
            out.append(int(cleaned))
        except ValueError:
            continue
    return out


class PluginSettings(BaseModel):
    cinevec_base_url: str = "http://127.0.0.1:8008"
    cinevec_api_token: str = ""

    plugin_host: str = "127.0.0.1"
    plugin_port: int = 8018
    plugin_data_dir: Path = Path("data")

    tg_api_id: Optional[int] = None
    tg_api_hash: str = ""
    tg_session_name: str = "tg_user_session"

    tg_bot_token: str = ""
    tg_allowed_user_ids: List[int] = Field(default_factory=list)

    disk_min_free_gb: float = 10.0
    max_preprocess_inflight: int = 2
    max_file_size_mb: int = 4096
    upload_batch_size: int = 10
    media_group_debounce_seconds: float = 1.5
    upload_timeout_seconds: float = 600.0

    default_check_interval_hours: float = 6.0
    auto_exclude_after_days: int = 0
    export_watch_dir: str = ""
    export_delete_after_upload: bool = False
    env_file_path: Optional[Path] = None

    @property
    def db_path(self) -> Path:
        return self.plugin_data_dir / "plugin_state.db"

    @property
    def tmp_dir(self) -> Path:
        return self.plugin_data_dir / "tmp"

    @property
    def session_path(self) -> Path:
        return self.plugin_data_dir / self.tg_session_name

    @property
    def telethon_configured(self) -> bool:
        return bool(self.tg_api_id and self.tg_api_hash.strip())

    @property
    def bot_configured(self) -> bool:
        return bool(self.tg_bot_token.strip())


def load_settings(env_file: Optional[Path] = None) -> PluginSettings:
    resolved_env = env_file if env_file is not None else (Path.cwd() / ".env")
    if resolved_env.is_file():
        load_dotenv(resolved_env, override=False)

    raw_api_id = os.getenv("TG_API_ID", "").strip()
    api_id: Optional[int] = None
    if raw_api_id:
        try:
            api_id = int(raw_api_id)
        except ValueError:
            api_id = None

    return PluginSettings(
        cinevec_base_url=os.getenv("CINEVEC_BASE_URL", "http://127.0.0.1:8008").rstrip("/"),
        cinevec_api_token=os.getenv("CINEVEC_API_TOKEN", "").strip(),
        plugin_host=os.getenv("PLUGIN_HOST", "127.0.0.1").strip(),
        plugin_port=int(os.getenv("PLUGIN_PORT", "8018")),
        plugin_data_dir=Path(os.getenv("PLUGIN_DATA_DIR", "data")),
        tg_api_id=api_id,
        tg_api_hash=os.getenv("TG_API_HASH", "").strip(),
        tg_session_name=os.getenv("TG_SESSION_NAME", "tg_user_session").strip() or "tg_user_session",
        tg_bot_token=os.getenv("TG_BOT_TOKEN", "").strip(),
        tg_allowed_user_ids=_parse_int_list(os.getenv("TG_ALLOWED_USER_IDS")),
        disk_min_free_gb=float(os.getenv("DISK_MIN_FREE_GB", "10.0")),
        max_preprocess_inflight=max(1, int(os.getenv("MAX_PREPROCESS_INFLIGHT", "2"))),
        max_file_size_mb=max(1, int(os.getenv("MAX_FILE_SIZE_MB", "4096"))),
        upload_batch_size=max(1, min(20, int(os.getenv("UPLOAD_BATCH_SIZE", "10")))),
        media_group_debounce_seconds=max(0.2, float(os.getenv("MEDIA_GROUP_DEBOUNCE_SECONDS", "1.5"))),
        upload_timeout_seconds=max(30.0, float(os.getenv("UPLOAD_TIMEOUT_SECONDS", "600.0"))),
        default_check_interval_hours=max(0.05, float(os.getenv("DEFAULT_CHECK_INTERVAL_HOURS", "6.0"))),
        auto_exclude_after_days=max(0, int(os.getenv("AUTO_EXCLUDE_AFTER_DAYS", "0"))),
        export_watch_dir=os.getenv("EXPORT_WATCH_DIR", "").strip(),
        export_delete_after_upload=os.getenv("EXPORT_DELETE_AFTER_UPLOAD", "false").strip().lower()
        in {"1", "true", "yes", "on"},
        env_file_path=resolved_env,
    )
