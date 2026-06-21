-- =====================================================================
-- 030_series_aspect_embeddings.sql
-- 作品を6観点に分割した記述テキストと、各観点の埋め込みベクトルを格納する。
--
-- 既存の review_summary (Grok の一括要約) を材料に、Grok で6観点へ structured 分割し、
-- 各観点を個別 embedding する。観点を分けることで「学園恋愛もの」等への希釈を防ぎ、
-- 観点別 (あらすじ似 / 雰囲気似 / 読者層似 等) の検索・重み付けが可能になる。
--
-- 6観点:
--   plot   : あらすじ・設定・舞台・ギミック
--   cast   : 主人公像・キャラ配置・関係性・勢力図
--   theme  : テーマ・葛藤・問題意識
--   mood   : 雰囲気・読後感・感情曲線
--   reader : どんな読者/気分に向くか
--   review : 実際のレビュー傾向・評価/不満点
--
-- *_text に元テキストを残すので、別 embedding モデルへ切替時も再生成だけで済む。
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE openbd_test_series
    ADD COLUMN IF NOT EXISTS plot_text         TEXT,
    ADD COLUMN IF NOT EXISTS plot_embedding    vector(1536),
    ADD COLUMN IF NOT EXISTS cast_text         TEXT,
    ADD COLUMN IF NOT EXISTS cast_embedding    vector(1536),
    ADD COLUMN IF NOT EXISTS theme_text        TEXT,
    ADD COLUMN IF NOT EXISTS theme_embedding   vector(1536),
    ADD COLUMN IF NOT EXISTS mood_text         TEXT,
    ADD COLUMN IF NOT EXISTS mood_embedding    vector(1536),
    ADD COLUMN IF NOT EXISTS reader_text       TEXT,
    ADD COLUMN IF NOT EXISTS reader_embedding  vector(1536),
    -- review は 029 で review_summary/review_embedding が既にあるが、
    -- ここでは「観点として切り出したレビュー傾向」を別に持つ
    ADD COLUMN IF NOT EXISTS review_aspect_text       TEXT,
    ADD COLUMN IF NOT EXISTS review_aspect_embedding  vector(1536),
    ADD COLUMN IF NOT EXISTS aspect_source            TEXT,        -- 生成バージョン
    ADD COLUMN IF NOT EXISTS aspect_built_at          TIMESTAMPTZ;

-- 各観点の HNSW index (cosine)
CREATE INDEX IF NOT EXISTS idx_series_plot_embedding
    ON openbd_test_series USING hnsw (plot_embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_series_cast_embedding
    ON openbd_test_series USING hnsw (cast_embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_series_theme_embedding
    ON openbd_test_series USING hnsw (theme_embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_series_mood_embedding
    ON openbd_test_series USING hnsw (mood_embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_series_reader_embedding
    ON openbd_test_series USING hnsw (reader_embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_series_review_aspect_embedding
    ON openbd_test_series USING hnsw (review_aspect_embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
