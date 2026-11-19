<div align="center">

# CineGram

### [CineVec](https://github.com/Chillwinner/Cinevec) 公式スタンドアロン FastAPI Telegram プラグイン

**Telegram は無限のクラウドストレージですが、半年前の 10 秒クリップを探すのは至難の業です。**
CineGram はチャンネルやグループの画像・最大 4GB の動画を **Zero-Retention** で **CineVec** に流し込み、3D シアターから元の `t.me` メッセージへ一発ジャンプできるようにします。

[English](README.md) · [简体中文](README.zh-CN.md) · **日本語** · [한국어](README.ko.md) · [Español](README.es.md)

[![CineVec Core](https://img.shields.io/badge/Core-CineVec%20%2Fapi%2Fv1-E06A3E?style=for-the-badge)](https://github.com/Chillwinner/Cinevec)
[![FastAPI](https://img.shields.io/badge/FastAPI-Lightweight%20Web%20UI-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Telethon](https://img.shields.io/badge/MTProto-Telethon%20(Up%20to%204GB)-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)](https://docs.telethon.dev/)

[できること](#できること) • [強み](#他ツールとの違い) • [本体 CineVec](#本体プロジェクト-cinevec) • [拡張・差し替え](#拡張とコンポーネント差し替え) • [クイックスタート](#クイックスタート)

</div>

---

## できること

**CineGram** は **[CineVec](https://github.com/Chillwinner/Cinevec)** と `/api/v1/*` + Bearer API Token のみで疎結合に連携する独立 FastAPI サービスです。

- **4 つの独立モード（必須設定なし）**：Bot も MTProto アカウントも任意。Web のみ / Bot のみ / MTProto のみ / デュアルモードを軽量 Web 管理画面（`http://127.0.0.1:8018/`）からホット切替可能。
- **ルールフィルタ＋サムネイル選択インポート**：グループ別の条件フィルタ（種類・サイズ・再生時間・キーワード・遡及日数）に加え、Web でサムネイルを見てチェックした項目だけを取り込めます。
- **セマンティック＆タグ／本文あいまい検索**：管理画面および任意 Bot（`/s`・`/m`・画像検索）から即座に検索可能。

---

## 他ツールとの違い

| 項目 | 一般的な保存スクリプト | CineGram |
| :--- | :--- | :--- |
| **ディスク消費** | 大容量動画を延々とローカルに保存。 | **Zero-Retention + 二重バックプレッシャ**：一時ファイルを送信直後に削除し、空き容量や CineVec 前処理キューに応じて自動一時停止。 |
| **複数枚アルバム** | 2 枚目以降のファイルで本文や `#タグ` が欠落。 | **アルバム本文＆`#タグ` 自動継承**：同一グループ内の全ファイルへ自動引き継ぎ。 |
| **リンク切れ対策** | グループ名変更や非公開化でリンクが放置される。 | **定期生存巡回**：改名・非公開化（`t.me/c/...` へ自動更新）・削除を検知し `source_invalid` を反映。 |
| **重複防止** | バックフィル連打で重複登録。 | **4 層重複排除**：同時実行ロック＋カーソル再開＋ローカル DB＋CineVec `ref_id` 照合。 |

---

## 本体プロジェクト: CineVec

- **メインリポジトリ**: [https://github.com/Chillwinner/Cinevec](https://github.com/Chillwinner/Cinevec)
- CineVec の **Settings → Plugin API Tokens** で発行したトークンを設定するだけで接続完了します。

---

## 拡張とコンポーネント差し替え

- **コード不要**：Bot / MTProto 切替、Zero-Retention / S3 保存切替、フィルタ規則はすべて `.env` または Web 管理画面で変更可能。
- **Telegram 実装や送信先を差し替えたい場合**：[`src/tg_plugin/models.py`](src/tg_plugin/models.py) の `TelegramInspector` プロトコル、または [`src/tg_plugin/cinevec_client.py`](src/tg_plugin/cinevec_client.py) の `CineVecClient` を実装し、`create_plugin_app(..., inspector=..., cinevec=...)` に渡すだけで差し替え可能です。

---

## クイックスタート

```bash
git clone https://github.com/Chillwinner/CineGram.git
cd CineGram
python -m venv .venv && source .venv/bin/activate
pip install -e .
python -m tg_plugin serve
```

**http://127.0.0.1:8018/** を開き、CineVec API Token と任意の Bot Token / MTProto（`python -m tg_plugin login`）を設定してください。テスト実行：`pytest -q`。
