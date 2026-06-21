-- =====================================================================
-- 028_series_embedding.sql
-- openbd_test_series にベクトル埋め込み列を追加し、類似検索 / 自然言語検索 /
-- おすすめ推薦のための pgvector セットアップを行う。
--
-- 埋め込み元: タイトル / 著者 / atmosphere・genre・sub_genre タグ /
--            7軸スコア / シノプシス を整形した文字列。
-- 次元数: 1536 (OpenAI text-embedding-3-small)
-- =====================================================================

-- pgvector 拡張 (Supabase はデフォルトで利用可)
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE openbd_test_series
    ADD COLUMN IF NOT EXISTS embedding         vector(1536),
    ADD COLUMN IF NOT EXISTS embedding_source  TEXT,        -- 'v1_synopsis_tags' 等、再計算可否を判定する用
    ADD COLUMN IF NOT EXISTS embedded_at       TIMESTAMPTZ;

-- 類似検索用 HNSW index (cosine 距離)
-- ef_construction を大きめにすると build は遅いが検索精度が上がる
CREATE INDEX IF NOT EXISTS idx_openbd_test_series_embedding
    ON openbd_test_series USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
