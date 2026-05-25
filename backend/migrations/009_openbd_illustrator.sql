-- =====================================================================
-- 009_openbd_illustrator.sql
-- ・openbd_test_books に illustrator (絵, ONIX A12) 列を追加
--   author 列は A01 (著) のみを格納する運用に変更
-- =====================================================================

ALTER TABLE openbd_test_books ADD COLUMN IF NOT EXISTS illustrator TEXT;

CREATE INDEX IF NOT EXISTS idx_openbd_test_illustrator ON openbd_test_books(illustrator);
