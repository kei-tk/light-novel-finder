-- =====================================================================
-- 038_cards_synopsis.sql
-- series_cards にあらすじを持たせ、一覧の RPC でも返す (ranobeNext の「書影なし・あらすじ型」用)。
--
-- 書影を使わないデザインでは、一覧のカードごとにあらすじを3行出す。今は search_text に混ぜているだけなので、
-- 表示用に synopsis 列を足す。中身は openbd_test_series の synopsis、無ければ description (詳細ページと同じ)。
-- RPC の戻り値の列が増えるので、search_series / recent_releases / cards_by_ids は DROP してから作り直す。
-- 列が増えるだけなので、書影ありの版 (main ブランチ) はそのまま動く。
--
-- 実行: SQL Editor でまるごと実行する。全カードのあらすじを埋めるので、数十秒かかることがある。
-- =====================================================================

SET LOCAL statement_timeout = '15min';
SET LOCAL search_path = public, extensions;

ALTER TABLE series_cards ADD COLUMN IF NOT EXISTS synopsis text;

-- 同期: 037 の refresh_series_card に synopsis を足したもの
CREATE OR REPLACE FUNCTION refresh_series_card(p_series_id uuid) RETURNS void
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    DELETE FROM series_cards c
     WHERE c.series_id = p_series_id
       AND NOT EXISTS (SELECT 1 FROM openbd_test_series s
                        WHERE s.id = p_series_id AND s.parent_series_id IS NULL
                          AND NOT coalesce(s.is_special_edition, false));

    INSERT INTO series_cards (series_id, series_title, author, illustrator, imprint, volume_count,
                              latest_published_date, popularity_score, genre_tags, atmosphere_tags,
                              sub_genre_tags, cover_image_url, search_text, book_dates, synopsis, updated_at)
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
           coalesce((SELECT array_agg(DISTINCT b.published_date ORDER BY b.published_date)
                       FROM openbd_test_books b
                      WHERE b.series_id = s.id AND b.published_date IS NOT NULL), '{}'),
           nullif(btrim(coalesce(s.synopsis, s.description)), ''),
           now()
      FROM openbd_test_series s
     WHERE s.id = p_series_id AND s.parent_series_id IS NULL AND NOT coalesce(s.is_special_edition, false)
    ON CONFLICT (series_id) DO UPDATE SET
        series_title = EXCLUDED.series_title, author = EXCLUDED.author, illustrator = EXCLUDED.illustrator,
        imprint = EXCLUDED.imprint, volume_count = EXCLUDED.volume_count,
        latest_published_date = EXCLUDED.latest_published_date, popularity_score = EXCLUDED.popularity_score,
        genre_tags = EXCLUDED.genre_tags, atmosphere_tags = EXCLUDED.atmosphere_tags,
        sub_genre_tags = EXCLUDED.sub_genre_tags, cover_image_url = EXCLUDED.cover_image_url,
        search_text = EXCLUDED.search_text, book_dates = EXCLUDED.book_dates, synopsis = EXCLUDED.synopsis,
        updated_at = now();
$$;

-- 今あるカードにあらすじを埋める (カード全体を作り直すより軽い)
UPDATE series_cards c
   SET synopsis = nullif(btrim(coalesce(s.synopsis, s.description)), '')
  FROM openbd_test_series s
 WHERE s.id = c.series_id;


-- RPC (戻り値に synopsis を足す。中身は 037 と同じ) ------------------------
DROP FUNCTION IF EXISTS search_series(text, text[], text[], text[], text, text, text, int, int);
CREATE FUNCTION search_series(
    p_q text DEFAULT NULL,
    p_genres text[] DEFAULT '{}', p_atmospheres text[] DEFAULT '{}', p_sub_genres text[] DEFAULT '{}',
    p_tag_mode text DEFAULT 'and',
    p_author text DEFAULT NULL,
    p_sort text DEFAULT 'new',
    p_limit int DEFAULT 48, p_offset int DEFAULT 0)
RETURNS TABLE (
    series_id uuid, series_title text, author text, illustrator text, imprint text, volume_count int,
    latest_published_date date, popularity_score int, genre_tags text[], atmosphere_tags text[],
    sub_genre_tags text[], cover_image_url text, synopsis text, total bigint)
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = public, extensions
AS $$
    WITH sel AS (
        SELECT coalesce(p_genres, '{}') || coalesce(p_atmospheres, '{}') || coalesce(p_sub_genres, '{}') AS tags
    ), hit AS (
        SELECT c.*, card_released_date(c.book_dates, c.latest_published_date) AS released
          FROM series_cards c, sel
         WHERE card_visible(c.book_dates, c.latest_published_date)
           AND (nullif(btrim(p_q), '') IS NULL
                OR c.search_text ILIKE '%' || _like_escape(btrim(p_q)) || '%')
           AND (p_author IS NULL OR c.author = p_author)
           AND (cardinality(sel.tags) = 0
                OR (p_tag_mode = 'or'  AND (c.genre_tags || c.atmosphere_tags || c.sub_genre_tags) && sel.tags)
                OR (p_tag_mode <> 'or' AND (c.genre_tags || c.atmosphere_tags || c.sub_genre_tags) @> sel.tags))
    )
    SELECT h.series_id, h.series_title, h.author, h.illustrator, h.imprint, h.volume_count,
           h.released AS latest_published_date, h.popularity_score, h.genre_tags, h.atmosphere_tags,
           h.sub_genre_tags, h.cover_image_url, h.synopsis, count(*) OVER () AS total
      FROM hit h
     ORDER BY
        CASE WHEN p_sort = 'popularity' THEN h.popularity_score END DESC NULLS LAST,
        CASE WHEN p_sort = 'title' THEN h.series_title END ASC,
        h.released DESC NULLS LAST,
        h.series_id
     LIMIT least(greatest(p_limit, 1), 100) OFFSET greatest(p_offset, 0)
$$;

DROP FUNCTION IF EXISTS recent_releases(int, int, int);
CREATE FUNCTION recent_releases(p_days int DEFAULT 30, p_min int DEFAULT 12, p_fallback int DEFAULT 100)
RETURNS TABLE (
    series_id uuid, series_title text, author text, illustrator text, imprint text, volume_count int,
    latest_published_date date, popularity_score int, genre_tags text[], atmosphere_tags text[],
    sub_genre_tags text[], cover_image_url text, synopsis text)
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = public
AS $$
    WITH today AS (SELECT (now() AT TIME ZONE 'Asia/Tokyo')::date AS d),
    released AS (
        SELECT c.*, card_released_date(c.book_dates, c.latest_published_date) AS released
          FROM series_cards c
    ),
    recent AS (
        SELECT r.* FROM released r, today WHERE r.released BETWEEN today.d - p_days AND today.d
    )
    SELECT c.series_id, c.series_title, c.author, c.illustrator, c.imprint, c.volume_count,
           c.released AS latest_published_date, c.popularity_score, c.genre_tags, c.atmosphere_tags,
           c.sub_genre_tags, c.cover_image_url, c.synopsis
      FROM recent c
     WHERE (SELECT count(*) FROM recent) >= p_min
    UNION ALL
    SELECT * FROM (
        SELECT c.series_id, c.series_title, c.author, c.illustrator, c.imprint, c.volume_count,
               c.released AS latest_published_date, c.popularity_score, c.genre_tags, c.atmosphere_tags,
               c.sub_genre_tags, c.cover_image_url, c.synopsis
          FROM released c
         WHERE c.released IS NOT NULL
           AND (SELECT count(*) FROM recent) < p_min
         ORDER BY c.released DESC, c.series_id
         LIMIT p_fallback
    ) f
    ORDER BY latest_published_date DESC, series_id
$$;

DROP FUNCTION IF EXISTS cards_by_ids(uuid[]);
CREATE FUNCTION cards_by_ids(p_ids uuid[])
RETURNS TABLE (
    series_id uuid, series_title text, author text, illustrator text, imprint text, volume_count int,
    latest_published_date date, popularity_score int, genre_tags text[], atmosphere_tags text[],
    sub_genre_tags text[], cover_image_url text, synopsis text)
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = public
AS $$
    SELECT c.series_id, c.series_title, c.author, c.illustrator, c.imprint, c.volume_count,
           card_released_date(c.book_dates, c.latest_published_date), c.popularity_score,
           c.genre_tags, c.atmosphere_tags, c.sub_genre_tags, c.cover_image_url, c.synopsis
      FROM series_cards c
     WHERE c.series_id = ANY (p_ids)
       AND card_visible(c.book_dates, c.latest_published_date)
$$;

REVOKE ALL ON FUNCTION search_series(text, text[], text[], text[], text, text, text, int, int) FROM PUBLIC;
REVOKE ALL ON FUNCTION recent_releases(int, int, int) FROM PUBLIC;
REVOKE ALL ON FUNCTION cards_by_ids(uuid[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION search_series(text, text[], text[], text[], text, text, text, int, int)
    TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION recent_releases(int, int, int) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION cards_by_ids(uuid[]) TO anon, authenticated, service_role;

NOTIFY pgrst, 'reload schema';

-- 確認 (実行後に別に流す): あらすじのあるカードの数と、全体の大きさ
-- SELECT count(*) AS cards, count(synopsis) AS with_synopsis,
--        pg_size_pretty(pg_total_relation_size('series_cards')) AS size
--   FROM series_cards;

-- 元に戻すとき (DOWN): 037 を実行し直す (synopsis 列は残っても害はない)
