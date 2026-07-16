-- =====================================================================
-- 031_aspect_embeddings_3072.sql
-- 6観点の埋め込みを text-embedding-3-small(1536) → text-embedding-3-large(3072) へ移行。
-- シノニム (百合 / 女の子同士の恋愛 / GL) など言い換え耐性の向上を狙う。
--
-- 検索は vector_search のインメモリ全件 cosine で行うため DB インデックスは不要。
-- pgvector の HNSW/ivfflat は 2000次元上限なので 3072 ではインデックスを作らない。
--
-- 既存の 1536 ベクトルは破棄して列を作り直す。テキスト (*_text) は保持するので、
-- reembed_aspects_large.py で *_text から 3072 ベクトルを再生成する。
-- 単体ベクトル (embedding=028, review_embedding=029) は検索未使用のため変更しない。
-- =====================================================================

-- 旧 index が残っていれば削除 (3072 では HNSW 不可)
DROP INDEX IF EXISTS idx_series_plot_embedding;
DROP INDEX IF EXISTS idx_series_cast_embedding;
DROP INDEX IF EXISTS idx_series_theme_embedding;
DROP INDEX IF EXISTS idx_series_mood_embedding;
DROP INDEX IF EXISTS idx_series_reader_embedding;
DROP INDEX IF EXISTS idx_series_review_aspect_embedding;

-- 1536 列を破棄して 3072 で作り直す (テキストは別列なので保持される)
ALTER TABLE openbd_test_series
    DROP COLUMN IF EXISTS plot_embedding,
    DROP COLUMN IF EXISTS cast_embedding,
    DROP COLUMN IF EXISTS theme_embedding,
    DROP COLUMN IF EXISTS mood_embedding,
    DROP COLUMN IF EXISTS reader_embedding,
    DROP COLUMN IF EXISTS review_aspect_embedding;

ALTER TABLE openbd_test_series
    ADD COLUMN plot_embedding          vector(3072),
    ADD COLUMN cast_embedding          vector(3072),
    ADD COLUMN theme_embedding         vector(3072),
    ADD COLUMN mood_embedding          vector(3072),
    ADD COLUMN reader_embedding        vector(3072),
    ADD COLUMN review_aspect_embedding vector(3072);
