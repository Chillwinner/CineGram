<div align="center">

# CineGram

### [CineVec](https://github.com/Chillwinner/Cinevec) 官方独立 FastAPI Telegram 资源对接插件

**Telegram 是全世界最大的免费无限云盘——直到你想翻出半年前某个群里发过的 10 秒镜头。**
CineGram 以 **Zero-Retention（零原片留存）** 方式，把 Telegram 频道与群组里的图片和最高 4GB 视频流式送进 **CineVec**，让你在 3D 放映室秒搜画面，并一键跳回原 `t.me` 消息。

[English](README.md) · **简体中文** · [日本語](README.ja.md) · [한국어](README.ko.md) · [Español](README.es.md)

[![CineVec Core](https://img.shields.io/badge/Core-CineVec%20%2Fapi%2Fv1-E06A3E?style=for-the-badge)](https://github.com/Chillwinner/Cinevec)
[![FastAPI](https://img.shields.io/badge/FastAPI-独立轻量%20Web%20台-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Telethon](https://img.shields.io/badge/MTProto-Telethon%20(最高%204GB)-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)](https://docs.telethon.dev/)
[![Bot API](https://img.shields.io/badge/Telegram-可选%20Bot%20随手搜-111111?style=for-the-badge&logo=telegram&logoColor=white)](https://core.telegram.org/bots/api)

[能做什么](#它能做什么) • [核心优点](#为什么选-cinegram相较同类采集器的优点) • [配套本体](#配套主项目cinevec) • [扩展与替换](#如何替换底层或二次扩展) • [快速部署](#快速部署教程)

</div>

---

## 它能做什么

**CineGram** 是一个完全独立于主仓库的 FastAPI 微服务，全程仅通过 HTTP `/api/v1/*` + Bearer API Token 与 **[CineVec](https://github.com/Chillwinner/Cinevec)** 对话，不碰本体数据库，零耦合。

- **4 种模式自由组合（零强制）**：Bot Token 与 MTProto 用户号**都不是必填项**，支持仅 Web、仅 Bot、仅用户号或双模并行；内置轻量 Web 管理台（`http://127.0.0.1:8018/`），随时填凭据热生效。
- **规则过滤 + 网页浏览勾选导入**：支持按群设置过滤规则（仅视频/仅图、大小与时长下限、包含/屏蔽关键词、回溯天数），也支持在网页先看缩略图和文案，**只勾选想要的那几条再下载导入**。
- **语义 + 标签/文案模糊双检索**：内置管理台快捷检索与可选 Telegram Bot（`/s` 搜画面、发图搜图、`/m` 模糊查标签和文案）。

---

## 为什么选 CineGram（相较同类采集器的优点）

| 对比维度 | 常见 Telegram 搬运脚本 | CineGram |
| :--- | :--- | :--- |
| **硬盘占用** | 把几百 GB 视频全拷到本地硬盘，很快爆盘。 | **Zero-Retention + 双重背压**：默认 `store_original=false` 流式上传、用完即删本地临时文件，且自动监测剩余磁盘与 CineVec 预处理队列，满载自动暂停拉取。 |
| **多图/多视频相册** | Telegram 只把文案和 `#标签` 挂在第 1 条，第 2～10 个文件全部丢失描述。 | **相册文案与 `#标签` 自动继承**：整组所有文件自动继承完整文案与标签，哪怕在浏览页只勾第 3 张图也不会丢上下文。 |
| **群改私密 / 删消息** | 链接失效了根本不知道，越积越多死链。 | **定时存活巡检**：自动识别 `@username` 改名、公开群转私密（自动切换 `t.me/c/...` 链接并告警）、被踢出或单条消息被删（自动回写 `source_invalid`，支持超期自动清理）。 |
| **防重复导入** | 连点两次回溯或监听撞车就入库两份。 | **4 层防重**：并发回溯锁 + 断点游标 + 本地 `(chat_id, message_id)` / `file_unique_id` + CineVec `ref_id` 兜底查重。 |
| **防误扫全账号** | 登录就疯狂扫遍所有群。 | **严格手动开启**：添加的群默认「已停用」，不手动点击启用绝不监听任何群。 |

---

## 配套主项目：CineVec

CineGram 是 **[CineVec](https://github.com/Chillwinner/Cinevec)** 的官方 Telegram 接入插件（主项目提供 Three.js 3D 影院检索、EmbeddingGemma 2 / Chinese-CLIP 与 Milvus Lite / REST v2）。

- **主项目仓库**：[https://github.com/Chillwinner/Cinevec](https://github.com/Chillwinner/Cinevec)
- **鉴权方式**：在 CineVec 前端 **设置 → API Token** 一键签发 `pica_...` 令牌填入即可。

---

## 如何替换底层或二次扩展

需要自己改代码实现接口吗？**常规使用零代码，深度定制也只需改极少位置：**

### 1. 零代码即可切换（`.env` 或 Web 管理台热配）
- Bot 模式 / MTProto 用户号模式 / 双模随时热启停；
- 按群独立切换 **Zero-Retention（零留存）** / **托管原片到 S3** / **自动转存私有备份频道**；
- 动态调整磁盘水位、预处理并发、过滤规则与定时巡检间隔。

### 2. 想替换 Telegram 底层库或对接其他检索后端？
- **替换 Telegram 探测/采集底层**（例如换成 Pyrogram / Kurigram）：只需实现 [`src/tg_plugin/models.py`](src/tg_plugin/models.py) 中的 `TelegramInspector` 协议（`probe_chat`、`probe_messages`），并在 [`src/tg_plugin/app.py`](src/tg_plugin/app.py) 调用 `create_plugin_app(..., inspector=...)` 注入即可。
- **对接其他媒资/检索后端**：替换或继承 [`src/tg_plugin/cinevec_client.py`](src/tg_plugin/cinevec_client.py) 中的 `CineVecClient`，通过 `create_plugin_app(..., cinevec=...)` 注入——本地去重、相册继承、背压与 Web 管理台全部无痛复用。

---

## 快速部署教程

### 方式一：Docker 一键启动（推荐）

```bash
git clone https://github.com/Chillwinner/CineGram.git
cd CineGram
docker compose up -d --build
```

启动后打开 **http://127.0.0.1:8018/**，内置新手引导会自动检测 CineVec 连接、提示填入 `pica_...` Token，并带你选择 **Bot 转发/搜索**、**Desktop 导出导入（支持 `./exports` 目录自动监听）** 或 **MTProto 用户号**。

### 方式二：本地 Python 运行（3.9+）

```bash
git clone https://github.com/Chillwinner/CineGram.git
cd CineGram
python -m venv .venv
source .venv/bin/activate        # Windows PowerShell 用: .\.venv\Scripts\Activate.ps1
pip install -e .

# 启动独立 FastAPI 服务与轻量 Web 管理台（默认 8018 端口）
python -m tg_plugin serve
```

浏览器打开 **http://127.0.0.1:8018/**（Swagger 文档在 `/docs`）：
1. 切到 **「凭据与策略配置」**，填入从 CineVec 签发的 **API Token**（`pica_...`）。
2. *（可选）* 填入 **Bot Token** 点保存即可热启用 Bot（转发频道消息、私聊搜图/搜文，单文件 ≤ 20MB）。
3. *（可选）* 如需**拉取历史存量、下载 2GB～4GB 大视频、网页浏览勾选导入、消息级删除巡检**，去 [my.telegram.org](https://my.telegram.org) 申请 `TG_API_ID` 与 `TG_API_HASH` 填入配置，并运行一次登录：
   ```bash
   python -m tg_plugin login
   ```
4. *（可选）* 运行自动化测试：`pytest -q`（切勿提交 `.env`、`data/` 与 `*.session`）。
