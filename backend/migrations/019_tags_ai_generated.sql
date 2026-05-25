-- =====================================================================
-- 019_tags_ai_generated.sql
-- tags マスタテーブルに AI 生成タグの管理用フラグを追加する。
--   is_ai_generated: true = AI が発明したタグ (低優先), false = 手動キュレーション
--   usage_count    : 何シリーズで使われたか (cleanup の判断材料)
--   first_seen_at  : 最初に AI が生成した日時
-- =====================================================================

ALTER TABLE tags
    ADD COLUMN IF NOT EXISTS is_ai_generated BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS usage_count     INT     NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS first_seen_at   TIMESTAMPTZ DEFAULT NOW();

-- フィルタ用 index
CREATE INDEX IF NOT EXISTS idx_tags_ai_generated ON tags(is_ai_generated);
CREATE INDEX IF NOT EXISTS idx_tags_usage_count ON tags(usage_count DESC);
