from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_json_list(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return [str(x) for x in val] if isinstance(val, list) else []
    except Exception:
        return []


def _parse_json_obj(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except Exception:
        return {}


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._conn: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_ref TEXT NOT NULL,
                chat_id INTEGER UNIQUE,
                chat_username TEXT,
                chat_title TEXT,
                chat_type TEXT,
                enabled INTEGER NOT NULL DEFAULT 0,
                store_original INTEGER NOT NULL DEFAULT 0,
                backup_chat_id INTEGER,
                default_tags_json TEXT NOT NULL DEFAULT '[]',
                max_file_mb INTEGER,
                filter_rules_json TEXT NOT NULL DEFAULT '{}',
                last_message_id INTEGER NOT NULL DEFAULT 0,
                backfill_min_message_id INTEGER,
                backfill_completed INTEGER NOT NULL DEFAULT 0,
                check_interval_hours REAL,
                last_checked_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS synced_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                media_group_id TEXT,
                file_unique_id TEXT,
                file_id TEXT,
                ref_id TEXT NOT NULL,
                asset_id TEXT,
                batch_id TEXT,
                source_url TEXT NOT NULL,
                kind TEXT NOT NULL,
                file_size INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                lifecycle TEXT NOT NULL DEFAULT 'active',
                unavailable_since TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(chat_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_synced_file_uid ON synced_items(file_unique_id);
            CREATE INDEX IF NOT EXISTS idx_synced_source ON synced_items(source_id);
            CREATE INDEX IF NOT EXISTS idx_synced_lifecycle ON synced_items(lifecycle);

            CREATE TABLE IF NOT EXISTS check_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                sources_checked INTEGER NOT NULL DEFAULT 0,
                messages_checked INTEGER NOT NULL DEFAULT 0,
                urls_updated INTEGER NOT NULL DEFAULT 0,
                marked_invalid INTEGER NOT NULL DEFAULT 0,
                restored_active INTEGER NOT NULL DEFAULT 0,
                auto_excluded INTEGER NOT NULL DEFAULT 0,
                details_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        await self._conn.commit()
        cur = await self._conn.execute("PRAGMA table_info(sources)")
        cols = {row[1] for row in await cur.fetchall()}
        if "filter_rules_json" not in cols:
            await self._conn.execute(
                "ALTER TABLE sources ADD COLUMN filter_rules_json TEXT NOT NULL DEFAULT '{}'"
            )
            await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("StateStore has not been initialized")
        return self._conn

    @staticmethod
    def _source_from_row(row: aiosqlite.Row) -> Dict[str, Any]:
        return {
            "id": int(row["id"]),
            "chat_ref": row["chat_ref"],
            "chat_id": row["chat_id"],
            "chat_username": row["chat_username"],
            "chat_title": row["chat_title"],
            "chat_type": row["chat_type"],
            "enabled": bool(row["enabled"]),
            "store_original": bool(row["store_original"]),
            "backup_chat_id": row["backup_chat_id"],
            "default_tags": _parse_json_list(row["default_tags_json"]),
            "max_file_mb": row["max_file_mb"],
            "filter_rules": _parse_json_obj(row["filter_rules_json"]) if "filter_rules_json" in row.keys() else {},
            "last_message_id": int(row["last_message_id"] or 0),
            "backfill_min_message_id": row["backfill_min_message_id"],
            "backfill_completed": bool(row["backfill_completed"]),
            "check_interval_hours": row["check_interval_hours"],
            "last_checked_at": row["last_checked_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _item_from_row(row: aiosqlite.Row) -> Dict[str, Any]:
        return {
            "id": int(row["id"]),
            "source_id": row["source_id"],
            "chat_id": int(row["chat_id"]),
            "message_id": int(row["message_id"]),
            "media_group_id": row["media_group_id"],
            "file_unique_id": row["file_unique_id"],
            "file_id": row["file_id"],
            "ref_id": row["ref_id"],
            "asset_id": row["asset_id"],
            "batch_id": row["batch_id"],
            "source_url": row["source_url"],
            "kind": row["kind"],
            "file_size": int(row["file_size"] or 0),
            "status": row["status"],
            "lifecycle": row["lifecycle"],
            "unavailable_since": row["unavailable_since"],
            "error_message": row["error_message"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def create_source(self, data: Dict[str, Any]) -> Dict[str, Any]:
        ts = utc_iso()
        cur = await self.conn.execute(
            """
            INSERT INTO sources(
                chat_ref, chat_id, chat_username, chat_title, chat_type,
                enabled, store_original, backup_chat_id, default_tags_json,
                max_file_mb, filter_rules_json, check_interval_hours, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(data["chat_ref"]).strip(),
                data.get("chat_id"),
                data.get("chat_username"),
                data.get("chat_title"),
                data.get("chat_type"),
                1 if data.get("enabled", False) else 0,
                1 if data.get("store_original", False) else 0,
                data.get("backup_chat_id"),
                json.dumps(list(data.get("default_tags") or []), ensure_ascii=False),
                data.get("max_file_mb"),
                json.dumps(dict(data.get("filter_rules") or {}), ensure_ascii=False),
                data.get("check_interval_hours"),
                ts,
                ts,
            ),
        )
        await self.conn.commit()
        created = await self.get_source(int(cur.lastrowid))
        assert created is not None
        return created

    async def list_sources(self, *, enabled_only: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM sources"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id ASC"
        cur = await self.conn.execute(sql)
        rows = await cur.fetchall()
        return [self._source_from_row(r) for r in rows]

    async def get_source(self, source_id: int) -> Optional[Dict[str, Any]]:
        cur = await self.conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,))
        row = await cur.fetchone()
        return self._source_from_row(row) if row else None

    async def find_source_by_chat(self, chat_id: int) -> Optional[Dict[str, Any]]:
        cur = await self.conn.execute("SELECT * FROM sources WHERE chat_id = ?", (chat_id,))
        row = await cur.fetchone()
        return self._source_from_row(row) if row else None

    async def update_source(self, source_id: int, **fields: Any) -> Optional[Dict[str, Any]]:
        col_map = {
            "chat_ref": "chat_ref",
            "chat_id": "chat_id",
            "chat_username": "chat_username",
            "chat_title": "chat_title",
            "chat_type": "chat_type",
            "last_message_id": "last_message_id",
            "backfill_min_message_id": "backfill_min_message_id",
            "max_file_mb": "max_file_mb",
            "check_interval_hours": "check_interval_hours",
            "last_checked_at": "last_checked_at",
            "backup_chat_id": "backup_chat_id",
        }
        updates: Dict[str, Any] = {}
        for k, col in col_map.items():
            if k in fields:
                updates[col] = fields[k]
        for bkey in ("enabled", "store_original", "backfill_completed"):
            if bkey in fields and fields[bkey] is not None:
                updates[bkey] = 1 if fields[bkey] else 0
        if "default_tags" in fields and fields["default_tags"] is not None:
            updates["default_tags_json"] = json.dumps(list(fields["default_tags"]), ensure_ascii=False)
        if "filter_rules" in fields and fields["filter_rules"] is not None:
            updates["filter_rules_json"] = json.dumps(dict(fields["filter_rules"]), ensure_ascii=False)
        if not updates:
            return await self.get_source(source_id)
        updates["updated_at"] = utc_iso()
        sets = ", ".join(f"{k} = ?" for k in updates)
        await self.conn.execute(
            f"UPDATE sources SET {sets} WHERE id = ?",
            (*updates.values(), source_id),
        )
        await self.conn.commit()
        return await self.get_source(source_id)

    async def delete_source(self, source_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    async def find_by_chat_msg(self, chat_id: int, message_id: int) -> Optional[Dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM synced_items WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )
        row = await cur.fetchone()
        return self._item_from_row(row) if row else None

    async def find_by_file_unique_id(self, file_unique_id: str) -> Optional[Dict[str, Any]]:
        if not file_unique_id:
            return None
        cur = await self.conn.execute(
            "SELECT * FROM synced_items WHERE file_unique_id = ? AND status != 'failed' LIMIT 1",
            (file_unique_id,),
        )
        row = await cur.fetchone()
        return self._item_from_row(row) if row else None

    async def get_synced_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        cur = await self.conn.execute("SELECT * FROM synced_items WHERE id = ?", (item_id,))
        row = await cur.fetchone()
        return self._item_from_row(row) if row else None

    async def upsert_synced_item(self, data: Dict[str, Any]) -> Dict[str, Any]:
        ts = utc_iso()
        await self.conn.execute(
            """
            INSERT INTO synced_items(
                source_id, chat_id, message_id, media_group_id, file_unique_id,
                file_id, ref_id, asset_id, batch_id, source_url, kind, file_size,
                status, lifecycle, unavailable_since, error_message, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, message_id) DO UPDATE SET
                source_id = COALESCE(excluded.source_id, synced_items.source_id),
                media_group_id = COALESCE(excluded.media_group_id, synced_items.media_group_id),
                file_unique_id = COALESCE(excluded.file_unique_id, synced_items.file_unique_id),
                file_id = COALESCE(excluded.file_id, synced_items.file_id),
                ref_id = excluded.ref_id,
                asset_id = COALESCE(excluded.asset_id, synced_items.asset_id),
                batch_id = COALESCE(excluded.batch_id, synced_items.batch_id),
                source_url = excluded.source_url,
                kind = excluded.kind,
                file_size = excluded.file_size,
                status = excluded.status,
                lifecycle = excluded.lifecycle,
                unavailable_since = excluded.unavailable_since,
                error_message = excluded.error_message,
                updated_at = excluded.updated_at
            """,
            (
                data.get("source_id"),
                int(data["chat_id"]),
                int(data["message_id"]),
                data.get("media_group_id"),
                data.get("file_unique_id") or None,
                data.get("file_id") or None,
                data["ref_id"],
                data.get("asset_id"),
                data.get("batch_id"),
                data["source_url"],
                data["kind"],
                int(data.get("file_size") or 0),
                data.get("status", "pending"),
                data.get("lifecycle", "active"),
                data.get("unavailable_since"),
                data.get("error_message"),
                ts,
                ts,
            ),
        )
        await self.conn.commit()
        saved = await self.find_by_chat_msg(int(data["chat_id"]), int(data["message_id"]))
        assert saved is not None
        return saved

    async def update_synced_item(self, item_id: int, **fields: Any) -> Optional[Dict[str, Any]]:
        allowed = {
            "asset_id",
            "batch_id",
            "source_url",
            "status",
            "lifecycle",
            "unavailable_since",
            "error_message",
            "file_id",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return await self.get_synced_item(item_id)
        updates["updated_at"] = utc_iso()
        sets = ", ".join(f"{k} = ?" for k in updates)
        await self.conn.execute(
            f"UPDATE synced_items SET {sets} WHERE id = ?",
            (*updates.values(), item_id),
        )
        await self.conn.commit()
        return await self.get_synced_item(item_id)

    async def list_synced_items(
        self,
        *,
        source_id: Optional[int] = None,
        status: Optional[str] = None,
        lifecycle: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if lifecycle:
            clauses.append("lifecycle = ?")
            params.append(lifecycle)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([int(limit), int(offset)])
        cur = await self.conn.execute(
            f"SELECT * FROM synced_items {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params,
        )
        rows = await cur.fetchall()
        return [self._item_from_row(r) for r in rows]

    async def list_checkable_items_for_source(self, source_id: int) -> List[Dict[str, Any]]:
        cur = await self.conn.execute(
            """
            SELECT * FROM synced_items
            WHERE source_id = ?
              AND asset_id IS NOT NULL
              AND status IN ('uploaded', 'indexed', 'searchable')
              AND lifecycle IN ('active', 'source_invalid', 'missing')
            ORDER BY message_id ASC
            """,
            (source_id,),
        )
        rows = await cur.fetchall()
        return [self._item_from_row(r) for r in rows]

    async def count_items_by_status(self) -> Dict[str, int]:
        cur = await self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM synced_items GROUP BY status"
        )
        rows = await cur.fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}

    async def list_recent_batch_ids(self, limit: int = 20) -> List[str]:
        cur = await self.conn.execute(
            """
            SELECT DISTINCT batch_id FROM synced_items
            WHERE batch_id IS NOT NULL AND batch_id != '' AND status = 'uploaded'
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        rows = await cur.fetchall()
        return [str(r["batch_id"]) for r in rows if r["batch_id"]]

    async def record_check_log(self, log: Dict[str, Any]) -> Dict[str, Any]:
        cur = await self.conn.execute(
            """
            INSERT INTO check_logs(
                started_at, finished_at, sources_checked, messages_checked,
                urls_updated, marked_invalid, restored_active, auto_excluded, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                log["started_at"],
                log["finished_at"],
                int(log.get("sources_checked", 0)),
                int(log.get("messages_checked", 0)),
                int(log.get("urls_updated", 0)),
                int(log.get("marked_invalid", 0)),
                int(log.get("restored_active", 0)),
                int(log.get("auto_excluded", 0)),
                json.dumps(log.get("details") or {}, ensure_ascii=False),
            ),
        )
        await self.conn.commit()
        return {"id": int(cur.lastrowid), **log}

    async def list_check_logs(self, limit: int = 50) -> List[Dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM check_logs ORDER BY id DESC LIMIT ?",
            (int(limit),),
        )
        rows = await cur.fetchall()
        return [
            {
                "id": int(r["id"]),
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "sources_checked": int(r["sources_checked"]),
                "messages_checked": int(r["messages_checked"]),
                "urls_updated": int(r["urls_updated"]),
                "marked_invalid": int(r["marked_invalid"]),
                "restored_active": int(r["restored_active"]),
                "auto_excluded": int(r["auto_excluded"]),
                "details": _parse_json_obj(r["details_json"]),
            }
            for r in rows
        ]
