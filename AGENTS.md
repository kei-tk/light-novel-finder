# Light Novel Finder — 開発引き継ぎ

このファイルは Codex が起動時に自動読込します。プロジェクトのコンテキスト・規約・既知の癖をここにまとめてあります。
常に日本語で回答し、コードのコメントも日本語で記述してください。
---

## プロジェクト概要

日本のライトノベル推薦システム。openBD と hanmoto.com から書誌データを収集し、Gemini でシリーズ判定・タグ付け・雰囲気スコア付与を行い、Supabase に保存する。フロントエンド (Vite + React) はまだ最小限。

- リポジトリ: https://github.com/kei-tk/light-novel-finder (private)
- 主言語: 日本語 (応答も日本語で)
- メインブランチ: `main`

---

## 技術スタック

| 領域 | 技術 |
|---|---|
| バックエンド | Python 3.12 (venv は `backend/venv/`) |
| Python ランタイム | **必ず `backend/venv/bin/python`**。`./venv/bin/python -m scripts.xxx` で実行 |
| DB | Supabase (PostgreSQL) — `app/services/supabase_client.py` で接続 |
| 書誌 API | openBD (`/v1/coverage` + `/v1/get`) と hanmoto.com スクレイピング |
| AI | Google Gemini API (`google-genai` SDK) |
| 並列化 | `ThreadPoolExecutor` (主にエンリッチで `--workers` 指定) |
| フロントエンド | Vite + React + TypeScript + Tailwind |

---

## ディレクトリ構成

```
backend/
  app/
    config.py                  # 設定 (.env からの読込、モデル名等)
    services/
      gemini.py                # Gemini API ラッパ ★ 主要
      supabase_client.py       # Supabase クライアント
      openbd.py / hanmoto.py   # 書誌 API
  migrations/                  # 001-021 連番の SQL
  scripts/                     # データパイプライン (16 ファイル、後述)
  venv/                        # gitignore 済み
  .env                         # 秘匿情報。.env.example が雛形
frontend/                      # Vite + React (未成熟)
.gitignore                     # ルート (.env, venv, node_modules 等)
AGENTS.md                      # このファイル
```

---

## データベース構成

### 現行スキーマ (使用中)

| テーブル | 用途 |
|---|---|
| `openbd_test_books` | 書籍 1冊 = 1行。ISBN 主キー。`series_id` で series に紐付く |
| `openbd_test_series` | シリーズ単位。タイトル/著者/タグ/7軸スコア/popularity/momentum を持つ |
| `tags` | 公式 + AI 生成タグ (`is_ai_generated` で区別) |

### 旧スキーマ (廃止予定、データは残存)

| テーブル | 状態 |
|---|---|
| `series` / `volumes` / `tweets` | データは残っているが新パイプラインからは参照されない。DROP は未実施 |

### 主要カラム (`openbd_test_series`)

- `series_title`, `author`, `volume_count`, `is_series`, `parent_series_id`
- 雰囲気スコア (0-100): `comedy_score` / `serious_score` / `heartwarming_score` / `bittersweet_score` / `romantic_score` / `uplifting_score`
- 認知度: `popularity_score` (長期累計知名度) / `momentum_score` (直近の話題度)
- タグ: `atmosphere_tags` / `genre_tags` / `sub_genre_tags` (TEXT[])

### 主要カラム (`openbd_test_books`)

- `isbn`, `title`, `author`, `illustrator`, `contributors` (JSONB)
- `series_id`, `volume_number`, `volume_number_sub` (上中下用)
- `is_special_edition`, `attached_to_isbn` (特装版を本編に紐付け)
- `description`, `published_date`, `imprint`

---

## 主要スクリプト (16個)

### 収集系
- `discover_openbd.py` — openBD coverage API から ISBN を順次取得し imprint で絞り込み
- `collect_all_labels.py` — 主要レーベル一括収集オーケストレータ
- `rediscover_hanmoto.py` — 未登録 ISBN を hanmoto で救済

### 補完系 (hanmoto から)
- `enrich_author_hanmoto.py` — author が NULL のものを補完
- `enrich_creator_hanmoto.py` — A01/A12 (著/絵) を再取得
- `enrich_descriptions_hanmoto.py` — あらすじを完全版に置換
- `enrich_pubdate_hanmoto.py` — published_date を補完

### シリーズ判定
- `gemini_detect_series.py` ★ — 同一 contributor で Union-Find → Gemini でシリーズ分類 (main/sub_series/special_edition)
- `fix_orphan_books.py` — Gemini 応答漏れの書籍を 1冊 series 化
- `cleanup_orphan_series.py` — 紐付け 0冊の series を削除

### エンリッチ (タグ/スコア)
- `enrich_openbd_series.py` ★ — Gemini で 7軸スコア + タグ + momentum を付与
  - `--use-grounding` で Google Search グラウンディング
  - `--two-phase` で Phase1=Flash + Phase2=Pro+grounding
  - `--multipass` でサブカテゴリ毎に Phase1 (recall 広め) + Phase2 (precision 厳密) =
    1 シリーズ ~16 call。recall (タグの拾い漏れ) と precision (誤付与) を両立する
  - `--workers N` で並列化
  - 既評価シリーズ 14件を anchor として渡しキャリブレーション

### 監査・クリーンアップ
- `audit_series_quality.py` ★ — 9種類の品質チェック (孤立/重複/タグ欠落/著者バラバラ等)
- `merge_duplicate_series.py` ★ — 同名+同著者の重複 series を統合
- `cleanup_ai_tags.py` — AI 生成タグの管理
- `recount_tag_usage.py` ★ — tags.usage_count を openbd_test_series から再集計
- `cleanup_sub_genre_tags.py` ★ — Gemini で sub_genre タグを subcategory 7 種に分類、重複統合、低利用 AI タグ削除
- `set_tag_importance.py` ★ — tags.importance を一覧 / 個別 / 一括 / TSV で更新する CLI
- `retag_changed.py` ★ — 変更タグ (CHANGED_TAGS) だけを候補に全シリーズを 1 call/series で再評価。reset → AI 判定で再付与
- `retag_genre.py` ★ — ジャンルタグだけを Pro+grounding で再評価。estimate/dry-run/apply 3 モード。事前にリクエスト量とコスト概算を表示
- `clean_titles.py` — タイトルに混入した著者名を除去
- `inspect_title_author_overlap.py` — 著者名混入を診断 (read-only)

★ = 最近作成/改修されたもの

---

## Gemini モデル運用

### 設定 (`backend/app/config.py`)
```python
gemini_analysis_model    = "gemini-2.5-pro"
gemini_enrichment_model  = "gemini-3.1-pro-preview"
gemini_parse_model       = "gemini-2.5-flash"
```

### `gemini.py` の重要な挙動

- **safety_settings**: 全カテゴリ `BLOCK_NONE` で送信 (ラノベタイトルの SAFETY 誤検知回避)
- **PROHIBITED_CONTENT フォールバックチェーン** (`_generate_with_retry`):
  1. 指定モデル (例: `gemini-3.1-flash-lite`)
  2. `gemini-3.1-pro-preview` + grounding ON
  3. `gemini-2.5-pro` + grounding OFF (検索由来の prohibited 回避)
- **レート制限**: 429/quota エラーで指数待機リトライ (5回まで)
- **空レスポンス診断**: `[diag] finish_reason=... prompt_feedback=...` を必ず出力

### モデル名の検証

新モデルを使う前に `client.models.list()` で実在を確認すること。`gemini-3.1-pro` は存在しない (正式名は `gemini-3.1-pro-preview`)。

---

## 既知の問題と対処

### 1. PROHIBITED_CONTENT は safety_settings で無効化不可

Google ハードコード filter。「ロリ」「JS」など特定語彙は全モデルで弾かれる。Pro 系は緩めだが完全には防げない。10件程度は手動補完が必要 (現在残 10件)。

### 2. httpx HTTP/2 + workers=75 で `pseudo-header in trailer`

Supabase REST API への高並列アクセスで発生。Cloudflare が HTML 400 を返す。`enrich_openbd_series.py` の `_retry_supabase()` で指数バックオフリトライ済み。緩和策: workers を 25-50 に下げる。

### 3. RPD (1日 100K) 超過時の挙動

現状: 60秒待機 × 5回 → 諦めて None を返す。**grounding は外さない**。RPD 超過時はスクリプト全体を即停止すべき (未実装、改善余地あり)。

### 4. Flash-Lite は PROHIBITED_CONTENT に過敏

「桃音しおんのラノベ日記」のような無害なタイトルでも grounding 経由のスニペットを prohibited 判定されることが多い。フォールバックチェーンで救済。

---

## 最近の作業履歴 (2026-05)

### 完了済み
- ✅ 旧 series/volumes/tweets 依存スクリプト 13個を削除 (コミット 6f54a0d)
- ✅ H. 重複 series マージ実施: 26グループ/68行 → 42行削除 + 51 books 張替え
- ✅ G. タイトル乖離: 真の問題 3件に parent_series_id を設定 (彩雲国物語外伝 / おと×まほ短編集 / 通学シリーズ映画スピンオフ)
- ✅ PROHIBITED_CONTENT フォールバックチェーン実装
- ✅ git リポジトリ初期化 + GitHub private push

### 監査スクリプト最終結果
```
✓ A 孤立 series: 0       ✓ D タグ欠落: 0        ✓ G タイトル乖離: 真の問題 0
✓ B 単巻 is_series:  0    ⚠ E スコア部分欠損: 698  ✓ H 重複 series: 0
⚠ C 未エンリッチ:    10   ✓ F 著者バラバラ: 0     ✓ I FK 破損 books: 0
```

---

## 残タスク・将来課題

| 優先度 | 内容 |
|---|---|
| 中 | E. スコア部分欠損 698件 (2軸以上 NULL のものだけ再エンリッチ ≈ 400件想定) |
| 中 | RPD 超過時にスクリプト全体を即停止する制御 |
| 低 | C. 未エンリッチ 10件 (PROHIBITED で全モデル弾かれ、手動補完) |
| 低 | 旧 series/volumes/tweets テーブルの DROP (要バックアップ) |
| 低 | フロントエンドの本格実装 (検索 UI / スコア軸フィルタ) |

---

## 開発の進め方・規約

### コマンド実行

```bash
# Python 実行は必ず venv 経由
cd backend
./venv/bin/python -m scripts.xxx [args]

# よく使う組み合わせ
./venv/bin/python -m scripts.audit_series_quality           # 品質監査
./venv/bin/python -m scripts.merge_duplicate_series --dry-run
./venv/bin/python -m scripts.enrich_openbd_series \
    --model gemini-3.1-flash-lite --use-grounding --workers 25
```

### git ワークフロー

- 機能追加は branch を切る (`feature/xxx`) のが望ましいが、現状は main 直 commit でも OK
- destructive な DB 操作 (削除/マージ) は必ず `--dry-run` を先に実行
- コミットメッセージは日本語で OK、変更の WHY を1〜2文で書く

### コーディング規約

- 新規ファイル作成より既存編集を優先
- コメントは「なぜ」の説明だけ、「何を」は書かない
- 日本語コメント可
- print ログは `[セクション名]` プレフィックス推奨 (例: `[全 series 取得]`)
- スコアは 1刻みの精度を維持する (5/10 刻みに丸めない)

---

## ユーザーの好み

(過去のやり取りから判明したこと)

- **応答は短く**: 結論 → 簡潔な根拠の順。長文は避ける
- **確認を取る**: DB を変更する破壊的操作の前に dry-run と確認 ([y/N])
- **モデル名は実在確認してから**: 推測で `gemini-3.1-pro` 等を使わない
- **API コスト意識**: Flash → Pro へのフォールバックは PROHIBITED 時だけ等、段階的に
- **コミットは指示されたときのみ**: 勝手にコミットしない
- **GitHub への push も明示指示されたときのみ**

---

## 緊急時のロールバック

```bash
# 直近のコミットを取り消し
git revert HEAD

# 特定ファイルを HEAD の状態に戻す
git restore <file>

# 削除したファイルを復元
git checkout <commit> -- <file>
```

DB 操作のロールバックは migrations フォルダに対応する DOWN 用 SQL がないので、Supabase ダッシュボードからバックアップ復元が必要。
