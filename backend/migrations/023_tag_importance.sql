-- =====================================================================
-- 023_tag_importance.sql
-- tags マスタテーブルに重要度 (importance) を追加する。
--   importance: 1=低 / 2=中 / 3=高。NULL は 1 として扱う (アプリ側の規約)。
-- 重要度が高いタグほど Gemini に「適切かどうか厳しく判定する」よう
-- プロンプトで強調する用途。
-- =====================================================================

ALTER TABLE tags
    ADD COLUMN IF NOT EXISTS importance SMALLINT
        CHECK (importance IS NULL OR importance BETWEEN 1 AND 3);

CREATE INDEX IF NOT EXISTS idx_tags_importance ON tags(importance DESC NULLS LAST);
