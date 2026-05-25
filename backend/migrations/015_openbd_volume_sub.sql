-- =====================================================================
-- 015_openbd_volume_sub.sql
-- 上下分冊 (例: 「魔王学院の不適合者4〈上〉」「4〈下〉」) を表現するため、
-- volume_number_sub 列を追加。
-- detect_series.py が、〈〉《》 で囲まれたトークンをサブ巻として認識し、
-- 同 volume_number 内で発売日順に 1, 2, ... を割り当てる。
-- =====================================================================

ALTER TABLE openbd_test_books
    ADD COLUMN IF NOT EXISTS volume_number_sub INT;

CREATE INDEX IF NOT EXISTS idx_openbd_test_books_vol_sub
    ON openbd_test_books(series_id, volume_number, volume_number_sub);
