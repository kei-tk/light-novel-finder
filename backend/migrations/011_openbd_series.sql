-- =====================================================================
-- 011_openbd_series.sql
-- シリーズテーブル (openbd_test_series) を作成。
-- 構造は openbd_test_books に揃え、Gemini API でエンリッチする項目
-- (synopsis, atmosphere_tags, genre_tags 等) は NULL で受け入れる。
-- openbd_test_books には series_id (FK) と volume_number 列を追加。
-- =====================================================================

-- 旧 011 で誤って追加していた可能性のある series_title 列を除去
ALTER TABLE openbd_test_books DROP COLUMN IF EXISTS series_title;

CREATE TABLE IF NOT EXISTS openbd_test_series (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- 基本情報 (openbd_test_books と同形式)
    series_title TEXT NOT NULL,
    author TEXT NOT NULL,                       -- A01
    illustrator TEXT,                           -- A12
    contributors JSONB,                         -- ONIX全役割 (代表巻のもの)
    publisher TEXT,
    imprint TEXT,
    cover_image_url TEXT,                       -- 代表書影 (通常 vol 1)
    description TEXT,                           -- vol 1 の description

    -- シリーズ集計 (detect_series.py が更新)
    volume_count INT DEFAULT 0,
    first_published_date DATE,
    latest_published_date DATE,

    -- Gemini API でエンリッチ予定 (production の series テーブルと同じ)
    synopsis TEXT,                              -- シリーズ全体のあらすじ
    atmosphere_tags TEXT[],
    genre_tags TEXT[],
    sub_genre_tags TEXT[],
    target_audience TEXT,

    raw JSONB,                                   -- detect_series メタや Gemini 結果保存用
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (series_title, author)
);

CREATE INDEX IF NOT EXISTS idx_openbd_test_series_title ON openbd_test_series(series_title);
CREATE INDEX IF NOT EXISTS idx_openbd_test_series_author ON openbd_test_series(author);
CREATE INDEX IF NOT EXISTS idx_openbd_test_series_imprint ON openbd_test_series(imprint);
CREATE INDEX IF NOT EXISTS idx_openbd_test_series_atmosphere ON openbd_test_series USING GIN(atmosphere_tags);
CREATE INDEX IF NOT EXISTS idx_openbd_test_series_genre ON openbd_test_series USING GIN(genre_tags);
CREATE INDEX IF NOT EXISTS idx_openbd_test_series_sub_genre ON openbd_test_series USING GIN(sub_genre_tags);
CREATE INDEX IF NOT EXISTS idx_openbd_test_series_contributors ON openbd_test_series USING GIN(contributors);

ALTER TABLE openbd_test_series ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "openbd_test_series read" ON openbd_test_series;
CREATE POLICY "openbd_test_series read" ON openbd_test_series FOR SELECT USING (TRUE);

-- openbd_test_books に FK と巻数列
ALTER TABLE openbd_test_books ADD COLUMN IF NOT EXISTS series_id UUID
    REFERENCES openbd_test_series(id) ON DELETE SET NULL;
ALTER TABLE openbd_test_books ADD COLUMN IF NOT EXISTS volume_number INT;

CREATE INDEX IF NOT EXISTS idx_openbd_test_books_series_id ON openbd_test_books(series_id);
CREATE INDEX IF NOT EXISTS idx_openbd_test_books_series_vol ON openbd_test_books(series_id, volume_number);
