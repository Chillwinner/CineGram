from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import List, Optional

import uvicorn
from telethon import TelegramClient

from tg_plugin.app import create_plugin_app
from tg_plugin.config import load_settings


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


def cli(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="tg-plugin", description="CineVec Telegram Plugin CLI")
    parser.add_argument("--env-file", type=Path, default=None, help="Path to .env file")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="Start FastAPI server and background workers")
    sub.add_parser("login", help="Interactive Telethon user session login")

    args = parser.parse_args(argv)
    cmd = args.command or "serve"
    if cmd == "login":
        asyncio.run(_telethon_login(args.env_file))
        return
    settings = load_settings(args.env_file)
    app = create_plugin_app(settings)
    uvicorn.run(app, host=settings.plugin_host, port=settings.plugin_port)


if __name__ == "__main__":
    cli()
