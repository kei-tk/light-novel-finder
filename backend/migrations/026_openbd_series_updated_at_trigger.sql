-- =====================================================================
-- 026_openbd_series_updated_at_trigger.sql
-- openbd_test_series に updated_at 自動更新トリガーを追加。
-- これがあると UPDATE のたびに updated_at が動くので、エンリッチ run の
-- 中断後に --skip-updated-since <run 開始時刻> で確実に再開できる。
--
-- 同じ仕組みは migration 004 で series / tags 等に既に入っているが、
-- openbd_test_series には漏れていた。
-- =====================================================================

-- set_updated_at 関数は migration 004 で定義済みのはずだが、安全のため
-- 再定義しておく (CREATE OR REPLACE で冪等)。
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_openbd_test_series_updated_at ON openbd_test_series;
CREATE TRIGGER trg_openbd_test_series_updated_at
    BEFORE UPDATE ON openbd_test_series
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
