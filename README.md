<div align="center">

# CineGram

### Standalone FastAPI Telegram Bridge for [CineVec](https://github.com/Chillwinner/Cinevec)

**Telegram is an infinite cloud drive — until you try to find that one 10-second clip from six months ago.**
CineGram streams photos and up-to-4GB videos from your Telegram channels & groups into **CineVec** with **Zero-Retention**, so you can search them semantically in 3D and jump straight back to the original `t.me` message.

**English** · [简体中文](README.zh-CN.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Español](README.es.md)

[![CineVec Core](https://img.shields.io/badge/Core-CineVec%20%2Fapi%2Fv1-E06A3E?style=for-the-badge)](https://github.com/Chillwinner/Cinevec)
[![FastAPI](https://img.shields.io/badge/FastAPI-Lightweight%20Web%20UI-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Telethon](https://img.shields.io/badge/MTProto-Telethon%20(Up%20to%204GB)-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)](https://docs.telethon.dev/)
[![Bot API](https://img.shields.io/badge/Telegram-Optional%20Bot%20Search-111111?style=for-the-badge&logo=telegram&logoColor=white)](https://core.telegram.org/bots/api)

[What It Does](#what-cinegram-does) • [Why CineGram](#why-cinegram-beats-typical-telegram-archivers) • [Companion Core](#companion-core-cinevec) • [Extend & Swap](#extending-or-swapping-components) • [Quickstart](#quickstart-deployment)

</div>

---

## What CineGram Does

**CineGram** is an independent FastAPI microservice that connects Telegram channels, groups, and private chats to **[CineVec](https://github.com/Chillwinner/Cinevec)** purely via `/api/v1/*` + Bearer API Token — zero database coupling, zero bloat.

- **4 Independent Modes (Nothing Mandatory)**: Run **Web-only**, **Bot-only**, **MTProto-only**, or **Dual** mode. Neither a Bot token nor a Telegram user login is forced on you — configure or hot-toggle either anytime inside the built-in Web Console (`http://127.0.0.1:8018/`).
- **Selective Ingestion**: Combine per-group **filter rules** (media type, min/max MB, video duration, include/exclude keywords, lookback days) with a **Web thumbnail browser** that lets you cherry-pick individual messages without ever downloading unchecked videos.
- **Built-In Semantic & Lexical Search Console**: Search CineVec by visual meaning or fuzzy tag/caption match right inside CineGram's Web UI or via optional Telegram Bot (`/s`, `/m`, or drop an image).

---

## Why CineGram Beats Typical Telegram Archivers

| Feature | Typical Telegram Dumpers | CineGram |
| :--- | :--- | :--- |
| **Disk Usage** | Downloads terabytes of MP4s forever until your SSD cries. | **Zero-Retention + Double Backpressure**: streams media to CineVec (`store_original=false`), deletes local temp files immediately, and auto-pauses when disk free space or CineVec's preprocess queue hits your threshold. |
| **Multi-File Albums** | Only the 1st photo gets the caption; items 2–10 lose all context and `#tags`. | **Media Group Caption & `#tag` Inheritance**: automatically propagates the album's caption and `#hashtags` to every sibling file — even when cherry-picking a single photo! |
| **Dead Links & Private Shifts** | Stale links rot silently when a group renames or goes private. | **Scheduled Availability Watcher**: detects `@username` renames, public→private transitions (auto-migrating links to `t.me/c/...`), kicked chats, and deleted messages (`lifecycle="source_invalid"`). |
| **Duplicate Protection** | Clicking backfill twice creates duplicate junk. | **4-Layer Dedup**: concurrent backfill lock + cursor resume + local `(chat_id, message_id)` & `file_unique_id` + remote CineVec `ref_id` lookup. |
| **No Auto-Spam** | Scrapes every chat in your account on startup. | **Opt-In Only**: added groups default to `disabled`; nothing is listened to until you explicitly enable it. |

---

## Companion Core: CineVec

CineGram is the official Telegram plugin for **[CineVec](https://github.com/Chillwinner/Cinevec)** — the self-hosted multimodal media search engine with a Three.js 3D Screening Room, EmbeddingGemma 2 / Chinese-CLIP, and Milvus Lite / REST v2.

- **Main Repository**: [https://github.com/Chillwinner/Cinevec](https://github.com/Chillwinner/Cinevec)
- **Integration Contract**: Strictly HTTP `/api/v1/*` with a Bearer API token created in CineVec **Settings → Plugin API Tokens**.

---

## Extending or Swapping Components

Do you need to write code to customize CineGram? **Usually no — and when you do, the boundaries are clean:**

### 1. Zero-Code Switches (`.env` or Web Console)
- Switch between **Bot API**, **MTProto (Telethon)**, or **Both** with hot-reload from `http://127.0.0.1:8018/`.
- Toggle **Zero-Retention (`store_original=false`)** vs **S3 Hosted (`store_original=true`)** or automatic **Private Backup Channel** per group.
- Adjust disk/queue backpressure, filter rules, and auto-exclude retention days at runtime.

### 2. Swapping the Telegram Library or Target Backend
- **Replace Telethon with Pyrogram/Kurigram or a custom probe**: implement the 2-method `TelegramInspector` `Protocol` in [`src/tg_plugin/models.py`](src/tg_plugin/models.py) (`probe_chat`, `probe_messages`) and pass it to `create_plugin_app(..., inspector=...)` in [`src/tg_plugin/app.py`](src/tg_plugin/app.py).
- **Point to another retrieval backend**: swap or subclass `CineVecClient` in [`src/tg_plugin/cinevec_client.py`](src/tg_plugin/cinevec_client.py) and inject it via `create_plugin_app(..., cinevec=...)` — deduplication, album propagation, backpressure, and the Web UI keep working untouched.

---

## Quickstart Deployment

### Option A: One-Command Docker (Recommended)

```bash
git clone https://github.com/Chillwinner/CineGram.git
cd CineGram
docker compose up -d --build
```

Then open **http://127.0.0.1:8018/** — the built-in onboarding guide will walk you through checking your CineVec connection, pasting your `pica_...` token, and choosing **Bot**, **Desktop Export (`exports/` auto-watch)**, or **MTProto**.

> Drop Telegram Desktop export folders (`result.json` + media) into `./exports` and CineGram auto-imports them with zero `TG_API_ID` needed.

### Option B: Local Python (3.9+)

```bash
git clone https://github.com/Chillwinner/CineGram.git
cd CineGram
python -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -e .

# Start the standalone FastAPI service & lightweight Web Console (port 8018)
python -m tg_plugin serve
```

Open **http://127.0.0.1:8018/** (Swagger docs at `/docs`):
1. Open **Settings (凭据与策略配置)** and paste your **CineVec API Token** (`pica_...`).
2. *(Optional)* Paste a **Telegram Bot Token** and click Save — the Bot hot-starts immediately for search & channel forwards (≤ 20 MB).
3. *(Optional)* For **history backfill, 2GB–4GB videos, Web thumbnail cherry-picking, and message-level deletion checks**, fill in `TG_API_ID` & `TG_API_HASH` (from [my.telegram.org](https://my.telegram.org)) and run once:
   ```bash
   python -m tg_plugin login
   ```
4. *(Optional)* Run tests: `pytest -q`.
