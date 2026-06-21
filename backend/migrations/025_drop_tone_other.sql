-- =====================================================================
-- 025_drop_tone_other.sql
-- subcategory CHECK 制約から 'tone' と 'other' を除外する。
-- これらに分類されていたタグは別途手動で削除済み (2026-06-08)。
-- 残るサブカテゴリ: relationship / character / profession / setting / plot / format
-- =====================================================================

ALTER TABLE tags DROP CONSTRAINT IF EXISTS tags_subcategory_check;
ALTER TABLE tags ADD CONSTRAINT tags_subcategory_check
    CHECK (
        subcategory IS NULL OR
        subcategory IN ('relationship', 'character', 'profession',
                        'setting', 'plot', 'format')
    );
