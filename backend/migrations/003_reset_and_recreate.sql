-- =====================================================================
-- 003_reset_and_recreate.sql
-- 全データを破棄して series + volumes + tweets スキーマで再作成する
--
-- ⚠️ 警告: このSQLは既存データを完全に削除します
--    実行前にデータの確認・バックアップを行ってください
--
-- Supabase SQL Editor で実行
-- =====================================================================

-- 1. 既存オブジェクトを削除（依存関係順）
DROP VIEW IF EXISTS series_engagement CASCADE;
DROP VIEW IF EXISTS book_engagement CASCADE;
DROP TABLE IF EXISTS tweets CASCADE;
DROP TABLE IF EXISTS volumes CASCADE;
DROP TABLE IF EXISTS series CASCADE;
DROP TABLE IF EXISTS books CASCADE;

-- 2. series テーブル（作品単位）
CREATE TABLE series (
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

-- 3. volumes テーブル（巻単位）
CREATE TABLE volumes (
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

-- 4. tweets テーブル
CREATE TABLE tweets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tweet_id TEXT NOT NULL UNIQUE,
    content TEXT,
    tweet_type TEXT,                            -- announcement / other
    tweeted_at TIMESTAMPTZ,
    volume_id UUID REFERENCES volumes(id) ON DELETE SET NULL,
    like_count INTEGER DEFAULT 0,
    retweet_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 5. インデックス
CREATE INDEX idx_volumes_series ON volumes(series_id);
CREATE INDEX idx_volumes_published_date ON volumes(published_date);
CREATE INDEX idx_series_atmosphere ON series USING GIN(atmosphere_tags);
CREATE INDEX idx_series_genre ON series USING GIN(genre_tags);
CREATE INDEX idx_tweets_volume ON tweets(volume_id);
CREATE INDEX idx_tweets_tweeted_at ON tweets(tweeted_at);

-- 6. RLS（読み取りのみ匿名公開）
ALTER TABLE series ENABLE ROW LEVEL SECURITY;
ALTER TABLE volumes ENABLE ROW LEVEL SECURITY;
ALTER TABLE tweets ENABLE ROW LEVEL SECURITY;

CREATE POLICY "series read"  ON series  FOR SELECT USING (TRUE);
CREATE POLICY "volumes read" ON volumes FOR SELECT USING (TRUE);
CREATE POLICY "tweets read"  ON tweets  FOR SELECT USING (TRUE);

-- 7. updated_at 自動更新トリガー（series のみ）
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_series_updated_at ON series;
CREATE TRIGGER trg_series_updated_at
    BEFORE UPDATE ON series
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 8. 集計ビュー
CREATE OR REPLACE VIEW series_engagement AS
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
