-- =====================================================================
-- 008_openbd_test.sql
-- ・openBD試験収集用テーブル（試行用、不要になったら DROP TABLE で消せる）
-- =====================================================================

CREATE TABLE IF NOT EXISTS openbd_test_books (
    isbn TEXT PRIMARY KEY,
    title TEXT,
    author TEXT,
    published_date DATE,
    publisher TEXT,
    imprint TEXT,
    cover_image_url TEXT,
    description TEXT,
    raw JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_openbd_test_imprint ON openbd_test_books(imprint);
CREATE INDEX IF NOT EXISTS idx_openbd_test_pubdate ON openbd_test_books(published_date);

ALTER TABLE openbd_test_books ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "openbd test read" ON openbd_test_books;
CREATE POLICY "openbd test read" ON openbd_test_books FOR SELECT USING (TRUE);

-- 不要になったらこれで全削除:
--   DROP TABLE openbd_test_books;
