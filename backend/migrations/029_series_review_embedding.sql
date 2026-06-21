-- =====================================================================
-- 029_series_review_embedding.sql
-- レビュー・感想を Grok (web_search + x_search) で収集して生成した
-- 「物語性の詳細要約」と、その埋め込みベクトルを格納する列を追加。
--
-- あらすじ由来の embedding (028) とは別枠で持つ。両方を使い分け / ブレンドできる。
--   028: embedding            ← あらすじ + タグ + スコア
--   029: review_embedding     ← レビュー収集要約 (読者目線)
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE openbd_test_series
    ADD COLUMN IF NOT EXISTS review_summary        TEXT,          -- Grok 生成の詳細要約
    ADD COLUMN IF NOT EXISTS review_summary_model  TEXT,          -- 生成モデル (例: grok-4.3)
    ADD COLUMN IF NOT EXISTS review_sources        JSONB,         -- 参照した URL 一覧
    ADD COLUMN IF NOT EXISTS review_embedding      vector(1536),  -- review_summary の埋め込み
    ADD COLUMN IF NOT EXISTS review_embedded_at    TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_openbd_test_series_review_embedding
    ON openbd_test_series USING hnsw (review_embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
