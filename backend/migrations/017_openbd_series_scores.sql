-- =====================================================================
-- 017_openbd_series_scores.sql
-- openbd_test_series に「AI 評価スコア」軸を追加。
-- 雰囲気 (6軸) + 総合 (1軸) = 計 7 軸。すべて 0〜100 の SMALLINT、
-- 該当不明・判断不能の場合は NULL。
-- =====================================================================

ALTER TABLE openbd_test_series
    -- 雰囲気スコア (0-100)
    ADD COLUMN IF NOT EXISTS comedy_score        SMALLINT,   -- コメディ
    ADD COLUMN IF NOT EXISTS serious_score       SMALLINT,   -- シリアス
    ADD COLUMN IF NOT EXISTS heartwarming_score  SMALLINT,   -- ほのぼの
    ADD COLUMN IF NOT EXISTS bittersweet_score   SMALLINT,   -- 切ない
    ADD COLUMN IF NOT EXISTS romantic_score      SMALLINT,   -- 胸キュン
    ADD COLUMN IF NOT EXISTS uplifting_score     SMALLINT,   -- 高揚感 (感動・爽快・熱さ)
    -- 総合 (0-100)
    ADD COLUMN IF NOT EXISTS popularity_score    SMALLINT;   -- 人気度・知名度

-- 範囲制約 (NULL は許可)
ALTER TABLE openbd_test_series
    ADD CONSTRAINT chk_comedy_range
        CHECK (comedy_score IS NULL OR comedy_score BETWEEN 0 AND 100),
    ADD CONSTRAINT chk_serious_range
        CHECK (serious_score IS NULL OR serious_score BETWEEN 0 AND 100),
    ADD CONSTRAINT chk_heartwarming_range
        CHECK (heartwarming_score IS NULL OR heartwarming_score BETWEEN 0 AND 100),
    ADD CONSTRAINT chk_bittersweet_range
        CHECK (bittersweet_score IS NULL OR bittersweet_score BETWEEN 0 AND 100),
    ADD CONSTRAINT chk_romantic_range
        CHECK (romantic_score IS NULL OR romantic_score BETWEEN 0 AND 100),
    ADD CONSTRAINT chk_uplifting_range
        CHECK (uplifting_score IS NULL OR uplifting_score BETWEEN 0 AND 100),
    ADD CONSTRAINT chk_popularity_range
        CHECK (popularity_score IS NULL OR popularity_score BETWEEN 0 AND 100);

-- 検索ソート用 index (人気順 / 各雰囲気の強い順 で使う想定)
CREATE INDEX IF NOT EXISTS idx_openbd_test_series_popularity
    ON openbd_test_series(popularity_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_openbd_test_series_comedy
    ON openbd_test_series(comedy_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_openbd_test_series_uplifting
    ON openbd_test_series(uplifting_score DESC NULLS LAST);
