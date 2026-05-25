-- =====================================================================
-- 012_openbd_series_normalize_key.sql
-- detect_series.py が生成する series_title (共通プレフィックス) は別グループ間で
-- 衝突する場合があるため、ユニーク識別子として normalized_title (数字完全除去) を分離する。
--
-- - normalized_title NOT NULL 列を追加
-- - UNIQUE (series_title, author) → UNIQUE (normalized_title, author) に変更
-- - 既存データはクリア (FK は ON DELETE SET NULL なので books.series_id は NULL に戻る)
-- =====================================================================

-- 既存データをクリア
DELETE FROM openbd_test_series;

-- normalized_title 列を追加
ALTER TABLE openbd_test_series
    ADD COLUMN IF NOT EXISTS normalized_title TEXT NOT NULL DEFAULT '';

-- DEFAULT は移行用のみ
ALTER TABLE openbd_test_series
    ALTER COLUMN normalized_title DROP DEFAULT;

-- 旧 unique 制約を削除
ALTER TABLE openbd_test_series
    DROP CONSTRAINT IF EXISTS openbd_test_series_series_title_author_key;

-- 新 unique 制約 (normalized_title, author)
ALTER TABLE openbd_test_series
    ADD CONSTRAINT openbd_test_series_normalized_author_unique
    UNIQUE (normalized_title, author);

CREATE INDEX IF NOT EXISTS idx_openbd_test_series_normalized
    ON openbd_test_series(normalized_title);
