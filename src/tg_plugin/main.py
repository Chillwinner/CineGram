from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import List, Optional

import uvicorn
from telethon import TelegramClient

from tg_plugin.app import create_plugin_app
from tg_plugin.backpressure import BackpressureController
from tg_plugin.cinevec_client import CineVecClient
from tg_plugin.config import load_settings
from tg_plugin.desktop_export import DesktopExportImporter
from tg_plugin.ingest_pipeline import IngestPipeline
from tg_plugin.state_store import StateStore


async def _telethon_login(env_file: Optional[Path]) -> None:
    settings = load_settings(env_file)
    if not settings.telethon_configured:
        raise SystemExit("Please configure TG_API_ID and TG_API_HASH in .env first.")
    settings.plugin_data_dir.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(
        str(settings.session_path),
        int(settings.tg_api_id),
        settings.tg_api_hash,
    )
    await client.start()
    me = await client.get_me()
    print(f"Logged in as {getattr(me, 'username', None) or getattr(me, 'first_name', me.id)} (session: {settings.session_path}.session)")
    await client.disconnect()


async def _run_export_import(
    env_file: Optional[Path],
    export_path: str,
    *,
    chat_username: Optional[str],
    tags: List[str],
    store_original: bool,
    delete_after_upload: bool,
) -> None:
    settings = load_settings(env_file)
    store = StateStore(settings.db_path)
    await store.init()
    cinevec = CineVecClient(
        settings.cinevec_base_url,
        settings.cinevec_api_token,
        upload_timeout=settings.upload_timeout_seconds,
    )
    try:
        bp = BackpressureController(
            settings.tmp_dir,
            cinevec,
            disk_min_free_gb=settings.disk_min_free_gb,
            max_preprocess_inflight=settings.max_preprocess_inflight,
        )
        pipeline = IngestPipeline(settings, store, cinevec, bp)
        importer = DesktopExportImporter(settings, pipeline)
        res = await importer.import_path(
            export_path,
            chat_username=chat_username,
            default_tags=tags,
            store_original=store_original,
            delete_after_upload=delete_after_upload,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
    finally:
        await cinevec.close()
        await store.close()


def cli(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="tg-plugin", description="CineVec Telegram Plugin CLI")
    parser.add_argument("--env-file", type=Path, default=None, help="Path to .env file")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="Start FastAPI server and background workers")
    sub.add_parser("login", help="Interactive Telethon user session login")
    imp = sub.add_parser("import-export", help="Import Telegram Desktop JSON export folder into CineVec")
    imp.add_argument("path", help="Path to ChatExport folder or result.json")
    imp.add_argument("--username", default=None, help="Optional chat @username for public links")
    imp.add_argument("--tag", action="append", default=[], help="Default tag (repeatable)")
    imp.add_argument("--store-original", action="store_true", help="Also store originals in CineVec S3")
    imp.add_argument("--delete-after-upload", action="store_true", help="Delete uploaded local export media files")

    args = parser.parse_args(argv)
    cmd = args.command or "serve"
    if cmd == "login":
        asyncio.run(_telethon_login(args.env_file))
        return
    if cmd == "import-export":
        asyncio.run(
            _run_export_import(
                args.env_file,
                args.path,
                chat_username=args.username,
                tags=list(args.tag or []),
                store_original=bool(args.store_original),
                delete_after_upload=bool(args.delete_after_upload),
            )
        )
        return
    settings = load_settings(args.env_file)
    app = create_plugin_app(settings)
    uvicorn.run(app, host=settings.plugin_host, port=settings.plugin_port)


if __name__ == "__main__":
    cli()
