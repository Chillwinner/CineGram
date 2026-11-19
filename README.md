# CineGram

**CineGram** 是 [CineVec](https://github.com/Chillwinner/vec) 的独立 FastAPI Telegram 资源对接插件，负责对接 Telegram 频道/群组（MTProto 实时监听 + 断点历史回溯 + 按规则过滤 + Web 浏览勾选导入 + 定时存活巡检）与可选 Telegram Bot（转发入库 + 搜文/搜图/搜标签），全程通过 CineVec `/api/v1` + Bearer API Token 适配本体。

## 快速开始

1. 安装依赖（已预置 `.venv`）：
```bash
cd telegram-plugin
.venv/bin/pip install -e .
```

2. 启动服务（**无需预先配置 Bot 或 Telegram 账号**即可启动 Web 管理台）：
```bash
.venv/bin/python -m tg_plugin serve
# 浏览器打开轻量 Web 管理台：http://127.0.0.1:8018/
# Swagger API 文档：http://127.0.0.1:8018/docs
```

3. 按需配置（在 Web 管理台「凭据与策略配置」或 `.env` 中填入，均为可选）：
- **CineVec 连接**：填入 `CINEVEC_API_TOKEN`（由 CineVec 前端「设置 -> API Token」生成）。
- **Telegram Bot（可选）**：填入 `TG_BOT_TOKEN` 后保存即可热启用，支持转发入库、群/频道同步与聊天搜图搜文。
- **MTProto 用户号（可选）**：仅在需要历史存量回溯或 >20MB～4GB 大视频时填入 `TG_API_ID` / `TG_API_HASH`，并运行一次 `.venv/bin/python -m tg_plugin login`。

## 核心特性

- **Zero-Retention（默认 `store_original=false`）**：CineVec 仅保存预览图、封面与向量，并在 `source_url` 与 `ref.params` 中保留 `https://t.me/...` 原消息跳转链接与 `file_id`。
- **双重防堆积背压**：配合 CineVec 双通道调度（预处理完成后立即删原片），插件侧还会实时检查磁盘余量（`DISK_MIN_FREE_GB`）与预处理在途数量（`MAX_PREPROCESS_INFLIGHT`），满负荷时自动暂停拉取大视频。
- **定时存活巡检**：
  - 公开群改名 `@username` → 自动更新 `source_url`。
  - 公开群改私密（账号仍在群内）→ 自动切换为 `https://t.me/c/<id>/<msg_id>` 并记录告警。
  - 被踢出 / 群不可访问 / 单条消息被删 → 标为 `lifecycle="source_invalid"`（可配置到期自动 `exclude`）。
- **可选 Bot 交互**：配置 `TG_BOT_TOKEN` 和 `TG_ALLOWED_USER_IDS` 后支持转发入库进度实时编辑、`/s` 搜文、发图搜图、`/m` 元数据搜索及快捷操作按钮。
