-- =====================================================================
-- 002_series_volumes.sql
-- books テーブル（巻単位）を series（作品単位）+ volumes（巻単位）に分割
--
-- Supabase SQL Editor で実行する。
-- 既存の books / tweets データは自動的に移行される。
-- =====================================================================

-- 1. 新テーブル作成
CREATE TABLE IF NOT EXISTS series (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL UNIQUE,                 -- シリーズ名（巻数なし）
    author TEXT,
    label TEXT,
    synopsis TEXT,                              -- シリーズ全体のあらすじ（未読者向け）
    atmosphere_tags TEXT[],
    genre_tags TEXT[],
    themes TEXT[],
    target_audience TEXT,
    cover_image_url TEXT,                       -- 代表書影
    max_tweet_likes INTEGER DEFAULT 0,
    is_trending BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS volumes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    series_id UUID NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    volume_title TEXT NOT NULL,                 -- 巻タイトル（例: 「○○ 3」）
    volume_number INTEGER,                      -- 巻数（不明ならNULL）
    published_date DATE,
    cover_image_url TEXT,
    max_tweet_likes INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (series_id, volume_title)
);

CREATE INDEX IF NOT EXISTS idx_volumes_series ON volumes(series_id);
CREATE INDEX IF NOT EXISTS idx_volumes_published_date ON volumes(published_date);
CREATE INDEX IF NOT EXISTS idx_series_atmosphere ON series USING GIN(atmosphere_tags);
CREATE INDEX IF NOT EXISTS idx_series_genre ON series USING GIN(genre_tags);

-- 2. tweets に volume_id カラムを追加（旧 book_id は当面残す）
ALTER TABLE tweets ADD COLUMN IF NOT EXISTS volume_id UUID REFERENCES volumes(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_tweets_volume ON tweets(volume_id);

-- 3. 既存 books → series + volumes に移行
--    巻数抽出: 末尾の「 N」「(N)」「（N）」「 第N巻」「 N巻」を切り出す
DO $$
DECLARE
    b RECORD;
    base_title TEXT;
    vol_num INTEGER;
    sid UUID;
    m TEXT[];
BEGIN
    FOR b IN SELECT * FROM books LOOP
        -- 巻数パターンを末尾から抽出
        m := regexp_match(b.title, '^(.*?)[\s　]*(?:第)?[\(（]?([0-9０-９]+)[\)）]?(?:巻)?[\s　]*$');
        IF m IS NOT NULL THEN
            base_title := trim(m[1]);
            -- 全角数字を半角に
            vol_num := (translate(m[2], '０１２３４５６７８９', '0123456789'))::INTEGER;
        ELSE
            base_title := b.title;
            vol_num := NULL;
        END IF;
        IF base_title = '' THEN
            base_title := b.title;
        END IF;

        -- series upsert
        -- エンリッチ系カラム（synopsis/atmosphere_tags/genre_tags/themes/target_audience）は
        -- 旧データを破棄。再エンリッチ前提で NULL のまま入れる。
        INSERT INTO series (title, author, label, cover_image_url, max_tweet_likes, is_trending)
        VALUES (base_title, b.author, b.label, b.cover_image_url,
                COALESCE(b.max_tweet_likes, 0), COALESCE(b.is_trending, FALSE))
        ON CONFLICT (title) DO UPDATE SET
            author = COALESCE(series.author, EXCLUDED.author),
            cover_image_url = COALESCE(series.cover_image_url, EXCLUDED.cover_image_url),
            max_tweet_likes = GREATEST(series.max_tweet_likes, EXCLUDED.max_tweet_likes),
            is_trending = series.is_trending OR EXCLUDED.is_trending,
            updated_at = NOW()
        RETURNING id INTO sid;

        IF sid IS NULL THEN
            SELECT id INTO sid FROM series WHERE title = base_title;
        END IF;

        -- volume upsert
        INSERT INTO volumes (series_id, volume_title, volume_number, published_date,
                             cover_image_url, max_tweet_likes)
        VALUES (sid, b.title, vol_num, b.published_date, b.cover_image_url,
                COALESCE(b.max_tweet_likes, 0))
        ON CONFLICT (series_id, volume_title) DO NOTHING;

        -- tweets.book_id → volume_id に紐付け直し
        UPDATE tweets SET volume_id = (
            SELECT id FROM volumes WHERE series_id = sid AND volume_title = b.title
        ) WHERE book_id = b.id AND volume_id IS NULL;
    END LOOP;
END $$;

-- 4. RLS（読み取りのみ匿名公開）
ALTER TABLE series ENABLE ROW LEVEL SECURITY;
ALTER TABLE volumes ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "series read" ON series;
CREATE POLICY "series read" ON series FOR SELECT USING (TRUE);

DROP POLICY IF EXISTS "volumes read" ON volumes;
CREATE POLICY "volumes read" ON volumes FOR SELECT USING (TRUE);

-- 5. 集計ビュー
DROP VIEW IF EXISTS series_engagement;
CREATE VIEW series_engagement AS
SELECT
    s.id AS series_id,
    s.title,
    s.author,
    s.atmosphere_tags,
    s.genre_tags,
    s.synopsis,
    s.cover_image_url,
    s.is_trending,
    COUNT(v.id) AS volume_count,
    MAX(v.published_date) AS latest_published_date,
    MIN(v.published_date) AS first_published_date,
    COALESCE(SUM(v.max_tweet_likes), 0) AS total_likes,
    COALESCE(MAX(v.max_tweet_likes), 0) AS max_volume_likes
FROM series s
LEFT JOIN volumes v ON v.series_id = s.id
GROUP BY s.id;

-- 6. （任意）旧 books テーブルは当面残す。確認後に DROP TABLE books CASCADE;
