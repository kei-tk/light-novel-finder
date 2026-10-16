-- =====================================================================
-- 037_cards_released_only.sql
-- 一覧・検索・新刊・似ているシリーズから、特装版のシリーズと、発売前の巻を外す (2026-10-16 決定)。
--
-- 1. 特装版 (openbd_test_series.is_special_edition) のシリーズは series_cards / series_vectors に入れない
-- 2. 発売前の巻は「まだ無い」ものとして扱う
--    - 並び順や「最新刊」には、今日 (JST) までに発売された巻のうち一番新しい日付を使う
--    - 1巻も発売されていない (予約だけの) シリーズは出さない
--    - latest_published_date (= 全巻の最大) をそのまま使うと、続巻の予約が入っただけで日付が未来になり、
--      続刊中のシリーズまで消えてしまうので、巻ごとの発売日 (book_dates) を持っておいて検索のたびに計算する
--      (今日の日付が変わると自動で反映される。毎日の再計算は要らない)
--
-- 実行: SQL Editor でまるごと実行する。全シリーズのカードを作り直すので、数十秒〜数分かかることがある。
-- =====================================================================

SET LOCAL statement_timeout = '15min';
SET LOCAL search_path = public, extensions;

-- 1. 巻ごとの発売日を持つ ---------------------------------------------------
ALTER TABLE series_cards ADD COLUMN IF NOT EXISTS book_dates date[] NOT NULL DEFAULT '{}';

-- 今日 (JST) までに発売された一番新しい日付。巻の日付が無いシリーズは、シリーズの日付が今日以前ならそれを使う
CREATE OR REPLACE FUNCTION card_released_date(p_book_dates date[], p_latest date) RETURNS date
LANGUAGE sql STABLE PARALLEL SAFE
AS $$
    SELECT CASE
        WHEN cardinality(p_book_dates) > 0 THEN
            (SELECT max(d) FROM unnest(p_book_dates) d WHERE d <= (now() AT TIME ZONE 'Asia/Tokyo')::date)
        WHEN p_latest <= (now() AT TIME ZONE 'Asia/Tokyo')::date THEN p_latest
    END
$$;

-- 出してよいカードか: 発売済みの巻がある、または日付がまったく分からない (= 予約だけのシリーズではない)
CREATE OR REPLACE FUNCTION card_visible(p_book_dates date[], p_latest date) RETURNS boolean
LANGUAGE sql STABLE PARALLEL SAFE
AS $$
    SELECT card_released_date(p_book_dates, p_latest) IS NOT NULL
        OR (cardinality(p_book_dates) = 0 AND p_latest IS NULL)
$$;


-- 2. カードの同期を作り直す (特装版を除き、巻の発売日も持つ) -------------------
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
                              sub_genre_tags, cover_image_url, search_text, book_dates, updated_at)
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
           now()
      FROM openbd_test_series s
     WHERE s.id = p_series_id AND s.parent_series_id IS NULL AND NOT coalesce(s.is_special_edition, false)
    ON CONFLICT (series_id) DO UPDATE SET
        series_title = EXCLUDED.series_title, author = EXCLUDED.author, illustrator = EXCLUDED.illustrator,
        imprint = EXCLUDED.imprint, volume_count = EXCLUDED.volume_count,
        latest_published_date = EXCLUDED.latest_published_date, popularity_score = EXCLUDED.popularity_score,
        genre_tags = EXCLUDED.genre_tags, atmosphere_tags = EXCLUDED.atmosphere_tags,
        sub_genre_tags = EXCLUDED.sub_genre_tags, cover_image_url = EXCLUDED.cover_image_url,
        search_text = EXCLUDED.search_text, book_dates = EXCLUDED.book_dates, updated_at = now();
$$;

-- 特装版かどうかが変わったときも作り直す
DROP TRIGGER IF EXISTS trg_sync_series_cards ON openbd_test_series;
CREATE TRIGGER trg_sync_series_cards
    AFTER INSERT OR UPDATE OF series_title, author, illustrator, imprint, volume_count, latest_published_date,
                              popularity_score, genre_tags, atmosphere_tags, sub_genre_tags, synopsis,
                              description, cover_image_url, parent_series_id, is_special_edition
    ON openbd_test_series
    FOR EACH ROW EXECUTE FUNCTION trg_series_card_from_series();

-- 全シリーズを作り直す (特装版のカードが消え、book_dates が入る)
DELETE FROM series_cards c USING openbd_test_series s
 WHERE s.id = c.series_id AND coalesce(s.is_special_edition, false);
SELECT count(*) FROM (
    SELECT refresh_series_card(s.id) FROM openbd_test_series s
     WHERE s.parent_series_id IS NULL AND NOT coalesce(s.is_special_edition, false)
) t;


-- 3. series_vectors (035) からも特装版を外す -----------------------------------
CREATE OR REPLACE FUNCTION sync_series_vectors() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, extensions
AS $$
BEGIN
    IF NEW.parent_series_id IS NULL
       AND NOT coalesce(NEW.is_special_edition, false)
       AND NEW.theme_embedding_1536 IS NOT NULL
       AND NEW.mood_embedding_1536 IS NOT NULL
       AND NEW.reader_embedding_1536 IS NOT NULL THEN
        INSERT INTO series_vectors (series_id, author, genre_tags, theme, mood, reader, updated_at)
        VALUES (NEW.id, NEW.author, coalesce(NEW.genre_tags, '{}'),
                NEW.theme_embedding_1536, NEW.mood_embedding_1536, NEW.reader_embedding_1536, now())
        ON CONFLICT (series_id) DO UPDATE SET
            author = EXCLUDED.author, genre_tags = EXCLUDED.genre_tags,
            theme = EXCLUDED.theme, mood = EXCLUDED.mood, reader = EXCLUDED.reader,
            updated_at = now();
    ELSE
        DELETE FROM series_vectors WHERE series_id = NEW.id;
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_sync_series_vectors ON openbd_test_series;
CREATE TRIGGER trg_sync_series_vectors
    AFTER INSERT OR UPDATE OF parent_series_id, author, genre_tags, is_special_edition,
                              theme_embedding_1536, mood_embedding_1536, reader_embedding_1536
    ON openbd_test_series
    FOR EACH ROW EXECUTE FUNCTION sync_series_vectors();

DELETE FROM series_vectors v USING openbd_test_series s
 WHERE s.id = v.series_id AND coalesce(s.is_special_edition, false);


-- 4. RPC を、発売済みの日付で並べ・表示するように作り直す ------------------------
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
           h.released AS latest_published_date, h.popularity_score, h.genre_tags, h.atmosphere_tags, h.sub_genre_tags,
           h.cover_image_url, count(*) OVER () AS total
      FROM hit h
     ORDER BY
        CASE WHEN p_sort = 'popularity' THEN h.popularity_score END DESC NULLS LAST,
        CASE WHEN p_sort = 'title' THEN h.series_title END ASC,
        h.released DESC NULLS LAST,
        h.series_id
     LIMIT least(greatest(p_limit, 1), 100) OFFSET greatest(p_offset, 0)
$$;

CREATE OR REPLACE FUNCTION facet_counts(p_q text DEFAULT NULL, p_author text DEFAULT NULL)
RETURNS TABLE (category text, tag text, n int)
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = public, extensions
AS $$
    WITH hit AS (
        SELECT c.genre_tags, c.atmosphere_tags, c.sub_genre_tags
          FROM series_cards c
         WHERE card_visible(c.book_dates, c.latest_published_date)
           AND (nullif(btrim(p_q), '') IS NULL
                OR c.search_text ILIKE '%' || _like_escape(btrim(p_q)) || '%')
           AND (p_author IS NULL OR c.author = p_author)
    )
    SELECT 'genre', t, count(*)::int FROM hit, unnest(hit.genre_tags) t GROUP BY t
    UNION ALL
    SELECT 'atmosphere', t, count(*)::int FROM hit, unnest(hit.atmosphere_tags) t GROUP BY t
    UNION ALL
    SELECT 'sub_genre', t, count(*)::int FROM hit, unnest(hit.sub_genre_tags) t GROUP BY t
$$;

CREATE OR REPLACE FUNCTION recent_releases(p_days int DEFAULT 30, p_min int DEFAULT 12, p_fallback int DEFAULT 100)
RETURNS TABLE (
    series_id uuid, series_title text, author text, illustrator text, imprint text, volume_count int,
    latest_published_date date, popularity_score int, genre_tags text[], atmosphere_tags text[],
    sub_genre_tags text[], cover_image_url text)
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
           c.sub_genre_tags, c.cover_image_url
      FROM recent c
     WHERE (SELECT count(*) FROM recent) >= p_min
    UNION ALL
    SELECT * FROM (
        SELECT c.series_id, c.series_title, c.author, c.illustrator, c.imprint, c.volume_count,
               c.released AS latest_published_date, c.popularity_score, c.genre_tags, c.atmosphere_tags,
               c.sub_genre_tags, c.cover_image_url
          FROM released c
         WHERE c.released IS NOT NULL
           AND (SELECT count(*) FROM recent) < p_min
         ORDER BY c.released DESC, c.series_id
         LIMIT p_fallback
    ) f
    ORDER BY latest_published_date DESC, series_id
$$;

-- id を指定してカードを取る (似ているシリーズ・おすすめ・マイページ・詳細ページ用)。
-- 発売済みの日付を latest_published_date として返し、出さないカード (予約だけ) は返さない。並びは呼ぶ側で整える
CREATE OR REPLACE FUNCTION cards_by_ids(p_ids uuid[])
RETURNS TABLE (
    series_id uuid, series_title text, author text, illustrator text, imprint text, volume_count int,
    latest_published_date date, popularity_score int, genre_tags text[], atmosphere_tags text[],
    sub_genre_tags text[], cover_image_url text)
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = public
AS $$
    SELECT c.series_id, c.series_title, c.author, c.illustrator, c.imprint, c.volume_count,
           card_released_date(c.book_dates, c.latest_published_date), c.popularity_score,
           c.genre_tags, c.atmosphere_tags, c.sub_genre_tags, c.cover_image_url
      FROM series_cards c
     WHERE c.series_id = ANY (p_ids)
       AND card_visible(c.book_dates, c.latest_published_date)
$$;

REVOKE ALL ON FUNCTION card_released_date(date[], date) FROM PUBLIC;
REVOKE ALL ON FUNCTION card_visible(date[], date) FROM PUBLIC;
REVOKE ALL ON FUNCTION cards_by_ids(uuid[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION card_released_date(date[], date) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION card_visible(date[], date) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION cards_by_ids(uuid[]) TO anon, authenticated, service_role;

NOTIFY pgrst, 'reload schema';

-- 確認 (実行後に別に流す): カードの件数 / 出す件数 / 予約だけで隠れる件数 / 特装版が残っていないこと
-- SELECT count(*) AS cards,
--        count(*) FILTER (WHERE card_visible(book_dates, latest_published_date)) AS visible,
--        count(*) FILTER (WHERE NOT card_visible(book_dates, latest_published_date)) AS upcoming_only,
--        (SELECT count(*) FROM series_cards c JOIN openbd_test_series s ON s.id = c.series_id
--          WHERE s.is_special_edition) AS special_left
--   FROM series_cards;

-- ---------------------------------------------------------------------
-- 元に戻すとき (DOWN): 036 を実行し直すと、カードの同期と RPC が 036 の内容に戻る
-- (book_dates 列と card_released_date / card_visible / cards_by_ids は残っても害はない)
-- ---------------------------------------------------------------------
