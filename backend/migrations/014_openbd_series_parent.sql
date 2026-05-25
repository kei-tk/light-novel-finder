-- =====================================================================
-- 014_openbd_series_parent.sql
-- 同著者で series_title が他のseries_title を含む場合、親子関係で結ぶ。
-- 例: 親「本好きの下剋上」 vs 子「本好きの下剋上 第一部」
--     親「ソードアート・オンライン」 vs 子「ソードアート・オンライン プログレッシブ」
-- =====================================================================

ALTER TABLE openbd_test_series
    ADD COLUMN IF NOT EXISTS parent_series_id UUID
    REFERENCES openbd_test_series(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_openbd_test_series_parent
    ON openbd_test_series(parent_series_id);
