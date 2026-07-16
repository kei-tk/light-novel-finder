# vector_cache — 自然言語検索の永続データ資産

自然言語検索（`/api/series/semantic`）と類似検索が使う、6観点ベクトルのインメモリ行列を
ディスクに固めたものです。TTL では失効せず、常にここから即ロードされます。

## ファイル
- `facets.npz` … 6観点 × 3072次元 × 全ベクトル化済シリーズの float32 行列（約600MB）
- `meta.json`  … シリーズID順・ジャンル索引などのメタ情報

いずれも **正本は Supabase の `openbd_test_series`**。ここは派生キャッシュです。

## 更新タイミング（重要）
シリーズの6観点ベクトル（`*_embedding`）を**追加・再生成したときだけ**、次を実行して更新します:

```bash
cd backend
./venv/bin/python -m scripts.rebuild_vector_cache
```

DB を編集しても、このファイルを更新しない限り検索結果には反映されません（意図的な設計）。

## Git / デプロイについて
`facets.npz`（約600MB）は **GitHub の 100MB/ファイル上限を超える**ため git には含めていません
（`.gitignore` 済み）。公開デプロイ時は次のいずれかで配置してください:
- デプロイ先で `rebuild_vector_cache` を1回実行して生成する
- 生成済みの `facets.npz` / `meta.json` をデプロイ成果物として直接配置する（推奨・DB再取得不要で高速起動）
