-- =====================================================================
-- 027_momentum_reason.sql
-- momentum (話題度) が高い理由の注釈を保存するカラムを追加。
-- 例: 「2026-07-31 まで電子50%OFFセール」「劇場アニメ化決定」
--     「コミカライズ連載中」「○○大賞 受賞」など。
-- momentum が低い (話題が無い) series は NULL のままにする。
-- =====================================================================

ALTER TABLE openbd_test_series
    ADD COLUMN IF NOT EXISTS momentum_reason TEXT;

COMMENT ON COLUMN openbd_test_series.momentum_reason IS
    'momentum を高くした具体的な話題の根拠 (映画化/コミカライズ/セール/受賞/X バズ 等)。話題が無ければ NULL。';
