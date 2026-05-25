-- =====================================================================
-- 006_remove_themes.sql
-- ・series.themes カラム削除
-- ・tags テーブルから tag_type='theme' のタグを削除
-- ・tag_type の CHECK 制約から 'theme' を除く
-- =====================================================================

-- 1. series から themes 列削除
ALTER TABLE series DROP COLUMN IF EXISTS themes;

-- 2. tags マスタから theme タイプを削除
DELETE FROM tags WHERE tag_type = 'theme';

-- 3. CHECK 制約を貼り直す
ALTER TABLE tags DROP CONSTRAINT IF EXISTS tags_tag_type_check;
ALTER TABLE tags ADD CONSTRAINT tags_tag_type_check
  CHECK (tag_type IN ('atmosphere', 'genre', 'sub_genre'));
