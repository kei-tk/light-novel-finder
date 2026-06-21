-- =====================================================================
-- 024_tag_subcategory.sql
-- tags マスタに subcategory を追加し、サブジャンルタグ内での「種類」を保存する。
-- 主に tag_type='sub_genre' のタグに対して下記 8 種のいずれかが入る:
--   relationship / character / profession / setting /
--   plot / tone / format / other
-- atmosphere / genre タグでは NULL のまま運用してよい。
-- =====================================================================

ALTER TABLE tags
    ADD COLUMN IF NOT EXISTS subcategory TEXT;

-- 既存制約を一度落として最新の値セットで張り直す (再実行安全のため)
ALTER TABLE tags DROP CONSTRAINT IF EXISTS tags_subcategory_check;
ALTER TABLE tags ADD CONSTRAINT tags_subcategory_check
    CHECK (
        subcategory IS NULL OR
        subcategory IN ('relationship', 'character', 'profession',
                        'setting', 'plot', 'tone', 'format', 'other')
    );

CREATE INDEX IF NOT EXISTS idx_tags_subcategory ON tags(subcategory);
