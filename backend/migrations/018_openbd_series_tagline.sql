-- =====================================================================
-- 018_openbd_series_tagline.sql
-- 長文 synopsis と target_audience の代わりに、1文の「どんなラノベか」
-- 紹介文 (tagline) を保持する列を追加する。
-- 検索結果のカード・リストでの短い紹介に利用する想定。
--
-- 既存の synopsis / target_audience 列はそのまま残す (後で不要なら別途 DROP)。
-- =====================================================================

ALTER TABLE openbd_test_series
    ADD COLUMN IF NOT EXISTS tagline TEXT;
