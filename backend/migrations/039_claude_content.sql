-- =====================================================================
-- 039_claude_content.sql
-- 新作の文章 (タグ・紹介文・3観点の原文) を、スケジュール実行の Claude が作るようにする。
-- Gemini / Grok を使わなくなるので、誰がいつ作ったかを残し、X 投稿用のハッシュタグを足す。
--
--   x_hashtags       : X に投稿するときの分類用ハッシュタグ (# は付けずに保存)。アプリでは使わない
--   content_source   : 文章を作ったもの (例: 'claude_v1')。既存の Gemini / Grok 由来は NULL のまま
--   content_built_at : 文章を作った日時。NULL のシリーズが、次の文章づくりの対象になる
--
-- 実行: SQL Editor でまるごと実行する。列を足すだけなので、すぐ終わる。
-- =====================================================================

ALTER TABLE openbd_test_series
    ADD COLUMN IF NOT EXISTS x_hashtags       text[],
    ADD COLUMN IF NOT EXISTS content_source   text,
    ADD COLUMN IF NOT EXISTS content_built_at timestamptz;

NOTIFY pgrst, 'reload schema';

-- 確認 (実行後に別に流す): 3列があること
-- SELECT column_name, data_type FROM information_schema.columns
--  WHERE table_name = 'openbd_test_series'
--    AND column_name IN ('x_hashtags', 'content_source', 'content_built_at');

-- ---------------------------------------------------------------------
-- 元に戻すとき (DOWN)
-- ---------------------------------------------------------------------
-- ALTER TABLE openbd_test_series
--     DROP COLUMN IF EXISTS x_hashtags,
--     DROP COLUMN IF EXISTS content_source,
--     DROP COLUMN IF EXISTS content_built_at;
