-- =====================================================================
-- 036_series_cards.sql
-- 一覧・検索・チップの件数・新刊で使う、カード用の細いテーブルと RPC (ranobeNext のフェーズ3)。
--
-- 035 と同じ考え方: openbd_test_series (1.7GB) を毎回読むと Micro インスタンスでは遅いので、
-- カードと検索に使う列だけを series_cards に分け、トリガーで同期する。パイプラインは今までどおり
-- openbd_test_series / openbd_test_books に書くだけでよい。
--
--   search_series    さがす画面の一覧 (入力した語 + タグ + AND/OR + 著者 + 並び順 + ページ送り)
--   facet_counts     チップの件数 (入力した語と著者だけで絞った集合で数える。選んだタグには左右されない)
--   recent_releases  ホームの新作・新刊 / 新刊カレンダーの候補 (最近30日。少なければ新しい順の上位100件)
--
-- 表紙は今の API (_attach_latest_covers) と同じく「カバーのある巻のうち、巻数 → 発売日で一番新しいもの」。
-- 実行: SQL Editor でまるごと実行する。初回の取り込みで全シリーズを読むので、数十秒〜数分かかることがある。
-- =====================================================================

SET LOCAL statement_timeout = '15min';
SET LOCAL search_path = public, extensions;

-- 日本語の部分一致を速くするため (Supabase では extensions スキーマに入る)
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA extensions;

-- 1. カード用テーブル ----------------------------------------------------
CREATE TABLE IF NOT EXISTS series_cards (
    series_id             uuid PRIMARY KEY REFERENCES openbd_test_series(id) ON DELETE CASCADE,
    series_title          text NOT NULL,
    author                text,
    illustrator           text,
    imprint               text,
    volume_count          int,
    latest_published_date date,
    popularity_score      int,
    genre_tags            text[] NOT NULL DEFAULT '{}',
    atmosphere_tags       text[] NOT NULL DEFAULT '{}',
    sub_genre_tags        text[] NOT NULL DEFAULT '{}',
    cover_image_url       text,
    -- 入力した語で引く範囲 (タイトル・著者・イラスト・レーベル・あらすじ・タグ名)。デザインの matchQuery と同じ
    search_text           text NOT NULL DEFAULT '',
    updated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_series_cards_search ON series_cards USING gin (search_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_series_cards_tags
    ON series_cards USING gin ((genre_tags || atmosphere_tags || sub_genre_tags));
CREATE INDEX IF NOT EXISTS idx_series_cards_latest ON series_cards (latest_published_date DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_series_cards_popularity ON series_cards (popularity_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_series_cards_author ON series_cards (author);

ALTER TABLE series_cards ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "series_cards read" ON series_cards;
CREATE POLICY "series_cards read" ON series_cards FOR SELECT TO anon, authenticated USING (true);
REVOKE ALL ON series_cards FROM anon, authenticated;
GRANT SELECT ON series_cards TO anon, authenticated;


-- 2. 同期 -----------------------------------------------------------------
-- 1シリーズ分を作り直す。親シリーズでなくなった・消えたシリーズは消す
CREATE OR REPLACE FUNCTION refresh_series_card(p_series_id uuid) RETURNS void
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    DELETE FROM series_cards c
     WHERE c.series_id = p_series_id
       AND NOT EXISTS (SELECT 1 FROM openbd_test_series s WHERE s.id = p_series_id AND s.parent_series_id IS NULL);

    INSERT INTO series_cards (series_id, series_title, author, illustrator, imprint, volume_count,
                              latest_published_date, popularity_score, genre_tags, atmosphere_tags,
                              sub_genre_tags, cover_image_url, search_text, updated_at)
    SELECT s.id, s.series_title, s.author, s.illustrator, s.imprint, s.volume_count,
           s.latest_published_date, s.popularity_score,
           coalesce(s.genre_tags, '{}'), coalesce(s.atmosphere_tags, '{}'), coalesce(s.sub_genre_tags, '{}'),
           coalesce(
               (SELECT b.cover_image_url FROM openbd_test_books b
                 WHERE b.series_id = s.id AND b.cover_image_url IS NOT NULL AND b.cover_image_url <> ''
                 ORDER BY coalesce(b.volume_number, 0) DESC, b.published_date DESC NULLS LAST
                 LIMIT 1),
               s.cover_image_url),
           concat_ws(' ', s.series_title, s.author, s.illustrator, s.imprint, coalesce(s.synopsis, s.description),
                     array_to_string(coalesce(s.genre_tags, '{}') || coalesce(s.atmosphere_tags, '{}')
                                     || coalesce(s.sub_genre_tags, '{}'), ' ')),
           now()
      FROM openbd_test_series s
     WHERE s.id = p_series_id AND s.parent_series_id IS NULL
    ON CONFLICT (series_id) DO UPDATE SET
        series_title = EXCLUDED.series_title, author = EXCLUDED.author, illustrator = EXCLUDED.illustrator,
        imprint = EXCLUDED.imprint, volume_count = EXCLUDED.volume_count,
        latest_published_date = EXCLUDED.latest_published_date, popularity_score = EXCLUDED.popularity_score,
        genre_tags = EXCLUDED.genre_tags, atmosphere_tags = EXCLUDED.atmosphere_tags,
        sub_genre_tags = EXCLUDED.sub_genre_tags, cover_image_url = EXCLUDED.cover_image_url,
        search_text = EXCLUDED.search_text, updated_at = now();
$$;

CREATE OR REPLACE FUNCTION trg_series_card_from_series() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    PERFORM refresh_series_card(NEW.id);
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION trg_series_card_from_books() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    -- 本が別のシリーズに付け替えられたときは、元と先の両方の表紙を作り直す
    IF TG_OP IN ('UPDATE', 'DELETE') AND OLD.series_id IS NOT NULL THEN
        PERFORM refresh_series_card(OLD.series_id);
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') AND NEW.series_id IS NOT NULL
       AND (TG_OP = 'INSERT' OR NEW.series_id IS DISTINCT FROM OLD.series_id) THEN
        PERFORM refresh_series_card(NEW.series_id);
    END IF;
    RETURN NULL;
END;
$$;

-- 関係する列が変わったときだけ動かす (エンリッチでスコアや review などを書き換えても動かない)
DROP TRIGGER IF EXISTS trg_sync_series_cards ON openbd_test_series;
CREATE TRIGGER trg_sync_series_cards
    AFTER INSERT OR UPDATE OF series_title, author, illustrator, imprint, volume_count, latest_published_date,
                              popularity_score, genre_tags, atmosphere_tags, sub_genre_tags, synopsis,
                              description, cover_image_url, parent_series_id
    ON openbd_test_series
    FOR EACH ROW EXECUTE FUNCTION trg_series_card_from_series();

DROP TRIGGER IF EXISTS trg_sync_series_cards_books ON openbd_test_books;
CREATE TRIGGER trg_sync_series_cards_books
    AFTER INSERT OR DELETE OR UPDATE OF series_id, cover_image_url, volume_number, published_date
    ON openbd_test_books
    FOR EACH ROW EXECUTE FUNCTION trg_series_card_from_books();

-- 初回の取り込み (全親シリーズ)
SELECT count(*) FROM (
    SELECT refresh_series_card(s.id) FROM openbd_test_series s WHERE s.parent_series_id IS NULL
) t;


-- 3. RPC ------------------------------------------------------------------
-- LIKE の % _ \ を文字としてそのまま扱う
CREATE OR REPLACE FUNCTION _like_escape(p text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$ SELECT replace(replace(replace(p, '\', '\\'), '%', '\%'), '_', '\_') $$;

-- さがす画面の一覧。タグは3分類をまとめて1つの集合として、AND (全部持つ) / OR (どれかを持つ) で絞る
CREATE OR REPLACE FUNCTION search_series(
    p_q text DEFAULT NULL,
    p_genres text[] DEFAULT '{}', p_atmospheres text[] DEFAULT '{}', p_sub_genres text[] DEFAULT '{}',
    p_tag_mode text DEFAULT 'and',
    p_author text DEFAULT NULL,
    p_sort text DEFAULT 'new',
    p_limit int DEFAULT 48, p_offset int DEFAULT 0)
RETURNS TABLE (
    series_id uuid, series_title text, author text, illustrator text, imprint text, volume_count int,
    latest_published_date date, popularity_score int, genre_tags text[], atmosphere_tags text[],
    sub_genre_tags text[], cover_image_url text, total bigint)
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = public, extensions
AS $$
    WITH sel AS (
        SELECT coalesce(p_genres, '{}') || coalesce(p_atmospheres, '{}') || coalesce(p_sub_genres, '{}') AS tags
    ), hit AS (
        SELECT c.*
          FROM series_cards c, sel
         WHERE (nullif(btrim(p_q), '') IS NULL
                OR c.search_text ILIKE '%' || _like_escape(btrim(p_q)) || '%')
           AND (p_author IS NULL OR c.author = p_author)
           AND (cardinality(sel.tags) = 0
                OR (p_tag_mode = 'or'  AND (c.genre_tags || c.atmosphere_tags || c.sub_genre_tags) && sel.tags)
                OR (p_tag_mode <> 'or' AND (c.genre_tags || c.atmosphere_tags || c.sub_genre_tags) @> sel.tags))
    )
    SELECT h.series_id, h.series_title, h.author, h.illustrator, h.imprint, h.volume_count,
           h.latest_published_date, h.popularity_score, h.genre_tags, h.atmosphere_tags, h.sub_genre_tags,
           h.cover_image_url, count(*) OVER () AS total
      FROM hit h
     ORDER BY
        CASE WHEN p_sort = 'popularity' THEN h.popularity_score END DESC NULLS LAST,
        CASE WHEN p_sort = 'title' THEN h.series_title END ASC,
        h.latest_published_date DESC NULLS LAST,
        h.series_id
     LIMIT least(greatest(p_limit, 1), 100) OFFSET greatest(p_offset, 0)
$$;

-- チップの件数 (入力した語と著者だけで絞る。デザインの数え方と同じ)
CREATE OR REPLACE FUNCTION facet_counts(p_q text DEFAULT NULL, p_author text DEFAULT NULL)
RETURNS TABLE (category text, tag text, n int)
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = public, extensions
AS $$
    WITH hit AS (
        SELECT c.genre_tags, c.atmosphere_tags, c.sub_genre_tags
          FROM series_cards c
         WHERE (nullif(btrim(p_q), '') IS NULL
                OR c.search_text ILIKE '%' || _like_escape(btrim(p_q)) || '%')
           AND (p_author IS NULL OR c.author = p_author)
    )
    SELECT 'genre', t, count(*)::int FROM hit, unnest(hit.genre_tags) t GROUP BY t
    UNION ALL
    SELECT 'atmosphere', t, count(*)::int FROM hit, unnest(hit.atmosphere_tags) t GROUP BY t
    UNION ALL
    SELECT 'sub_genre', t, count(*)::int FROM hit, unnest(hit.sub_genre_tags) t GROUP BY t
$$;

-- ホームの新作・新刊 / 新刊カレンダーの候補 (発売日の新しい順)。未来の日付 (発売予定) は含めない
CREATE OR REPLACE FUNCTION recent_releases(p_days int DEFAULT 30, p_min int DEFAULT 12, p_fallback int DEFAULT 100)
RETURNS TABLE (
    series_id uuid, series_title text, author text, illustrator text, imprint text, volume_count int,
    latest_published_date date, popularity_score int, genre_tags text[], atmosphere_tags text[],
    sub_genre_tags text[], cover_image_url text)
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = public
AS $$
    WITH today AS (SELECT (now() AT TIME ZONE 'Asia/Tokyo')::date AS d),
    recent AS (
        SELECT c.* FROM series_cards c, today
         WHERE c.latest_published_date BETWEEN today.d - p_days AND today.d
    )
    SELECT c.series_id, c.series_title, c.author, c.illustrator, c.imprint, c.volume_count,
           c.latest_published_date, c.popularity_score, c.genre_tags, c.atmosphere_tags, c.sub_genre_tags,
           c.cover_image_url
      FROM recent c
     WHERE (SELECT count(*) FROM recent) >= p_min
    UNION ALL
    -- 最近の作品が少ないときは、今日以前の新しい順に上位 p_fallback 件
    SELECT * FROM (
        SELECT c.series_id, c.series_title, c.author, c.illustrator, c.imprint, c.volume_count,
               c.latest_published_date, c.popularity_score, c.genre_tags, c.atmosphere_tags, c.sub_genre_tags,
               c.cover_image_url
          FROM series_cards c, today
         WHERE c.latest_published_date <= today.d
           AND (SELECT count(*) FROM recent) < p_min
         ORDER BY c.latest_published_date DESC, c.series_id
         LIMIT p_fallback
    ) f
    ORDER BY latest_published_date DESC, series_id
$$;

REVOKE ALL ON FUNCTION refresh_series_card(uuid) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION trg_series_card_from_series() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION trg_series_card_from_books() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION _like_escape(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION search_series(text, text[], text[], text[], text, text, text, int, int) FROM PUBLIC;
REVOKE ALL ON FUNCTION facet_counts(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION recent_releases(int, int, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION refresh_series_card(uuid) TO service_role;
GRANT EXECUTE ON FUNCTION _like_escape(text) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION search_series(text, text[], text[], text[], text, text, text, int, int)
    TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION facet_counts(text, text) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION recent_releases(int, int, int) TO anon, authenticated, service_role;

NOTIFY pgrst, 'reload schema';

-- 確認 (実行後に別に流す): 件数と大きさ
-- SELECT count(*), pg_size_pretty(pg_total_relation_size('series_cards')) FROM series_cards;

-- ---------------------------------------------------------------------
-- 元に戻すとき (DOWN)
-- ---------------------------------------------------------------------
-- DROP TRIGGER IF EXISTS trg_sync_series_cards ON openbd_test_series;
-- DROP TRIGGER IF EXISTS trg_sync_series_cards_books ON openbd_test_books;
-- DROP FUNCTION IF EXISTS recent_releases(int, int, int);
-- DROP FUNCTION IF EXISTS facet_counts(text, text);
-- DROP FUNCTION IF EXISTS search_series(text, text[], text[], text[], text, text, text, int, int);
-- DROP FUNCTION IF EXISTS _like_escape(text);
-- DROP FUNCTION IF EXISTS trg_series_card_from_books();
-- DROP FUNCTION IF EXISTS trg_series_card_from_series();
-- DROP FUNCTION IF EXISTS refresh_series_card(uuid);
-- DROP TABLE IF EXISTS series_cards;
