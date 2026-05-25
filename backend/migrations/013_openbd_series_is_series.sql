-- =====================================================================
-- 013_openbd_series_is_series.sql
-- openbd_test_series に単巻作品も含めるため、シリーズ判定フラグを追加。
-- volume_count >= 2 → is_series=true (複数巻の作品)
-- volume_count = 1  → is_series=false (単巻のみの作品)
-- =====================================================================

ALTER TABLE openbd_test_series
    ADD COLUMN IF NOT EXISTS is_series BOOLEAN NOT NULL DEFAULT false;

-- 既存行を実態に合わせる
UPDATE openbd_test_series
   SET is_series = (volume_count >= 2)
 WHERE is_series IS DISTINCT FROM (volume_count >= 2);

CREATE INDEX IF NOT EXISTS idx_openbd_test_series_is_series
    ON openbd_test_series(is_series);
