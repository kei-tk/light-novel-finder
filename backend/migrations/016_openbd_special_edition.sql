-- =====================================================================
-- 016_openbd_special_edition.sql
-- 特装版/限定版/復刻版 等を本編シリーズから区別するためのフラグを追加。
-- 本編シリーズと特装版シリーズは別行として残し、特装版側に
-- parent_series_id を本編へ向けて設定することで関連付ける。
-- =====================================================================

ALTER TABLE openbd_test_series
    ADD COLUMN IF NOT EXISTS is_special_edition BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_openbd_test_series_special
    ON openbd_test_series(is_special_edition);
