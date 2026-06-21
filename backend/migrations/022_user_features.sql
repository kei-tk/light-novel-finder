-- =====================================================================
-- 022_user_features.sql
-- ユーザー機能 (お気に入り / 読了済み / プロフィール) のためのテーブル。
-- Supabase Auth (auth.users) と FK 連携し、RLS で本人のみ書き込み可。
-- =====================================================================

-- ----------------- プロフィール -----------------
-- 認証時に自動作成 (trigger 経由)。表示名や好みタグなどを保持。
CREATE TABLE IF NOT EXISTS profiles (
    id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    display_name TEXT,
    bio TEXT,
    -- 推薦時に重み付けに使う「好みタグ」
    favorite_genres TEXT[],
    favorite_atmospheres TEXT[],
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_profiles_genres ON profiles USING GIN(favorite_genres);
CREATE INDEX IF NOT EXISTS idx_profiles_atmospheres ON profiles USING GIN(favorite_atmospheres);

-- 新規ユーザー作成時に profiles 行を自動作成
CREATE OR REPLACE FUNCTION handle_new_user()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    INSERT INTO public.profiles (id, display_name)
    VALUES (NEW.id, COALESCE(NEW.raw_user_meta_data->>'display_name', split_part(NEW.email, '@', 1)));
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION handle_new_user();

-- ----------------- お気に入りシリーズ -----------------
CREATE TABLE IF NOT EXISTS favorite_series (
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    series_id UUID NOT NULL REFERENCES openbd_test_series(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, series_id)
);

CREATE INDEX IF NOT EXISTS idx_favorite_series_user ON favorite_series(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_favorite_series_series ON favorite_series(series_id);

-- ----------------- 読了済みシリーズ -----------------
CREATE TABLE IF NOT EXISTS read_series (
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    series_id UUID NOT NULL REFERENCES openbd_test_series(id) ON DELETE CASCADE,
    -- 1〜5 の5段階評価 (任意)
    rating SMALLINT,
    finished_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, series_id),
    CONSTRAINT chk_rating_range CHECK (rating IS NULL OR rating BETWEEN 1 AND 5)
);

CREATE INDEX IF NOT EXISTS idx_read_series_user ON read_series(user_id, finished_at DESC);

-- ----------------- RLS ポリシー -----------------
ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE favorite_series ENABLE ROW LEVEL SECURITY;
ALTER TABLE read_series ENABLE ROW LEVEL SECURITY;

-- profiles: 全員 read 可、本人のみ update
DROP POLICY IF EXISTS "profiles read all" ON profiles;
CREATE POLICY "profiles read all" ON profiles FOR SELECT USING (TRUE);

DROP POLICY IF EXISTS "profiles update self" ON profiles;
CREATE POLICY "profiles update self" ON profiles FOR UPDATE USING (auth.uid() = id);

DROP POLICY IF EXISTS "profiles insert self" ON profiles;
CREATE POLICY "profiles insert self" ON profiles FOR INSERT WITH CHECK (auth.uid() = id);

-- favorite_series: 本人のみ全操作
DROP POLICY IF EXISTS "favorite_series self all" ON favorite_series;
CREATE POLICY "favorite_series self all" ON favorite_series FOR ALL
    USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- read_series: 本人のみ全操作
DROP POLICY IF EXISTS "read_series self all" ON read_series;
CREATE POLICY "read_series self all" ON read_series FOR ALL
    USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- updated_at 自動更新トリガ (profiles)
CREATE OR REPLACE FUNCTION trigger_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS set_profiles_updated_at ON profiles;
CREATE TRIGGER set_profiles_updated_at
    BEFORE UPDATE ON profiles
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
