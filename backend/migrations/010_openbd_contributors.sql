-- =====================================================================
-- 010_openbd_contributors.sql
-- ・openbd_test_books に contributors (JSONB) 列を追加
--   全ONIX役割コード + 日本語ラベル付きで構造化保存する。
--   author (A01)/illustrator (A12) は denormalize としてそのまま残す。
-- =====================================================================

ALTER TABLE openbd_test_books ADD COLUMN IF NOT EXISTS contributors JSONB;

-- 役割でフィルタしたい場合のための GIN インデックス
-- 例: WHERE contributors @> '[{"role_code":"A23"}]'  で原作者ありの書籍
CREATE INDEX IF NOT EXISTS idx_openbd_test_contributors ON openbd_test_books USING GIN (contributors);
