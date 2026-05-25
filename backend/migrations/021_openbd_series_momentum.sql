-- =====================================================================
-- 021_openbd_series_momentum.sql
-- 「直近の話題度」を表す momentum_score 列を追加。
--
-- popularity_score (= 累計知名度・長期連載・累計部数 等の総合的な認知度)
-- では最近発売の作品が全て低スコアになり差別化できないため、
-- 直近の話題度を別軸で評価する。
-- =====================================================================

ALTER TABLE openbd_test_series
    ADD COLUMN IF NOT EXISTS momentum_score SMALLINT;

ALTER TABLE openbd_test_series
    ADD CONSTRAINT chk_momentum_range
        CHECK (momentum_score IS NULL OR momentum_score BETWEEN 0 AND 100);

CREATE INDEX IF NOT EXISTS idx_openbd_test_series_momentum
    ON openbd_test_series(momentum_score DESC NULLS LAST);
