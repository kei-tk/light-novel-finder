-- =====================================================================
-- 035_series_vectors.sql
-- 「似ているシリーズ」と「あなたへのおすすめ」をその場で計算するための、ベクトル専用の細いテーブル。
--
-- 034 の RPC は openbd_test_series (1.7GB。旧 3072 列・6観点・あらすじ等が同居) を読むため、
-- Micro インスタンス (共有バッファ 256MB) では必要なベクトルがメモリに残らず、数秒〜タイムアウトになった。
-- 使う列 (ジャンル + theme / mood / reader の 1536 ベクトル) だけを別テーブルに分けると約151MB になり、
-- 共有バッファに収まる。結果は保存せず、呼ばれるたびに計算する。
--
-- 似ているシリーズのルール (2026-10-15 に決定):
--   候補: genre_tags が2つ以上同じ作品 (基準作品のジャンルが1つなら1つ同じでよい。ジャンルが無ければ全件)
--   並び: theme / mood / reader の cos を「候補の中での順位」p にし、Σ (p + p²) の大きい順
--   (全件の中での順位で付けた場合との一致率は手元の試算で平均 92%)
--
-- series_vectors は openbd_test_series のトリガーで自動的に同期する。パイプラインは今までどおり
-- openbd_test_series に書くだけでよい。
--
-- 実行: SQL Editor でまるごと実行する。初回の INSERT で約151MB を書くので数分かかることがある。
-- =====================================================================

SET LOCAL statement_timeout = '15min';

-- 1. ベクトル専用テーブル ------------------------------------------------
CREATE TABLE IF NOT EXISTS series_vectors (
    series_id  uuid PRIMARY KEY REFERENCES openbd_test_series(id) ON DELETE CASCADE,
    author     text,
    genre_tags text[] NOT NULL DEFAULT '{}',
    theme      vector(1536) NOT NULL,
    mood       vector(1536) NOT NULL,
    reader     vector(1536) NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ジャンルで候補を絞るときに使う
CREATE INDEX IF NOT EXISTS idx_series_vectors_genre ON series_vectors USING gin (genre_tags);

-- 公開データなので読むのは誰でも可。書き込みはトリガー (と service_role) だけ
ALTER TABLE series_vectors ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "series_vectors read" ON series_vectors;
CREATE POLICY "series_vectors read" ON series_vectors FOR SELECT TO anon, authenticated USING (true);
REVOKE ALL ON series_vectors FROM anon, authenticated;
GRANT SELECT ON series_vectors TO anon, authenticated;


-- 2. openbd_test_series からの同期 ---------------------------------------
-- 親シリーズで、3観点のベクトルがそろっている作品だけを持つ。そろわなくなったら消す
CREATE OR REPLACE FUNCTION sync_series_vectors() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, extensions
AS $$
BEGIN
    IF NEW.parent_series_id IS NULL
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

-- 関係する列が変わったときだけ動かす (エンリッチなどほかの列の更新では動かない)
DROP TRIGGER IF EXISTS trg_sync_series_vectors ON openbd_test_series;
CREATE TRIGGER trg_sync_series_vectors
    AFTER INSERT OR UPDATE OF parent_series_id, author, genre_tags,
                              theme_embedding_1536, mood_embedding_1536, reader_embedding_1536
    ON openbd_test_series
    FOR EACH ROW EXECUTE FUNCTION sync_series_vectors();

-- 初回の取り込み
INSERT INTO series_vectors (series_id, author, genre_tags, theme, mood, reader)
SELECT s.id, s.author, coalesce(s.genre_tags, '{}'),
       s.theme_embedding_1536, s.mood_embedding_1536, s.reader_embedding_1536
  FROM openbd_test_series s
 WHERE s.parent_series_id IS NULL
   AND s.theme_embedding_1536 IS NOT NULL
   AND s.mood_embedding_1536 IS NOT NULL
   AND s.reader_embedding_1536 IS NOT NULL
ON CONFLICT (series_id) DO NOTHING;


-- 3. RPC (034 の similar_series / recommend_series / recommend_for_me を置き換える) --------
DROP FUNCTION IF EXISTS recommend_for_me(int);
DROP FUNCTION IF EXISTS recommend_series(uuid[], int, int);
DROP FUNCTION IF EXISTS similar_series(uuid, int, boolean, int);
DROP FUNCTION IF EXISTS _rank_candidates(uuid[], vector, vector, vector, vector, vector, vector, int);

-- 似ているシリーズ
CREATE OR REPLACE FUNCTION similar_series(p_series_id uuid, p_limit int DEFAULT 12)
RETURNS TABLE (series_id uuid, score real)
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = public, extensions
AS $$
    WITH b AS (
        SELECT genre_tags AS g, theme, mood, reader FROM series_vectors WHERE series_id = p_series_id
    ), cand AS (
        SELECT v.series_id,
               1 - (v.theme  <=> b.theme)  AS c_theme,
               1 - (v.mood   <=> b.mood)   AS c_mood,
               1 - (v.reader <=> b.reader) AS c_reader
          FROM series_vectors v, b
         WHERE v.series_id <> p_series_id
           -- && で GIN インデックスを使って先に絞り、そのあと共通ジャンルの数を数える
           AND (cardinality(b.g) = 0 OR v.genre_tags && b.g)
           AND (cardinality(b.g) = 0
                OR (SELECT count(*) FROM unnest(v.genre_tags) t WHERE t = ANY (b.g)) >= least(2, cardinality(b.g)))
    ), ranked AS (
        SELECT series_id,
               percent_rank() OVER (ORDER BY c_theme)  AS p_theme,
               percent_rank() OVER (ORDER BY c_mood)   AS p_mood,
               percent_rank() OVER (ORDER BY c_reader) AS p_reader
          FROM cand
    )
    SELECT series_id,
           (p_theme + p_theme ^ 2 + p_mood + p_mood ^ 2 + p_reader + p_reader ^ 2)::real AS score
      FROM ranked
     ORDER BY score DESC, series_id
     LIMIT p_limit
$$;

-- 複数作品に近い作品: 観点ごとに元にした作品の平均ベクトルを作り、全件の中での順位で並べる
-- (正規化済みベクトルの cos の平均は、平均ベクトルとの内積に等しい。ジャンルでは絞らない)
CREATE OR REPLACE FUNCTION recommend_series(p_series_ids uuid[], p_limit int DEFAULT 12)
RETURNS TABLE (series_id uuid, score real)
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = public, extensions
AS $$
    WITH c AS (
        SELECT avg(theme) AS theme, avg(mood) AS mood, avg(reader) AS reader
          FROM series_vectors WHERE series_id = ANY (p_series_ids)
    ), cand AS (
        SELECT v.series_id,
               (v.theme  <#> c.theme)  * -1 AS c_theme,
               (v.mood   <#> c.mood)   * -1 AS c_mood,
               (v.reader <#> c.reader) * -1 AS c_reader
          FROM series_vectors v, c
         WHERE c.theme IS NOT NULL AND NOT (v.series_id = ANY (p_series_ids))
    ), ranked AS (
        SELECT series_id,
               percent_rank() OVER (ORDER BY c_theme)  AS p_theme,
               percent_rank() OVER (ORDER BY c_mood)   AS p_mood,
               percent_rank() OVER (ORDER BY c_reader) AS p_reader
          FROM cand
    )
    SELECT series_id,
           (p_theme + p_theme ^ 2 + p_mood + p_mood ^ 2 + p_reader + p_reader ^ 2)::real AS score
      FROM ranked
     ORDER BY score DESC, series_id
     LIMIT p_limit
$$;

-- ログインユーザー向け (RLS で本人のお気に入り・読了しか読めない)
CREATE OR REPLACE FUNCTION recommend_for_me(p_limit int DEFAULT 12)
RETURNS TABLE (series_id uuid, score real)
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = public, extensions
AS $$
    SELECT * FROM recommend_series(
        ARRAY(
            SELECT DISTINCT x.series_id FROM (
                (SELECT f.series_id FROM favorite_series f
                  WHERE f.user_id = auth.uid() ORDER BY f.created_at DESC LIMIT 50)
                UNION ALL
                (SELECT r.series_id FROM read_series r
                  WHERE r.user_id = auth.uid() LIMIT 50)
            ) x
        ),
        p_limit)
$$;

REVOKE ALL ON FUNCTION sync_series_vectors() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION similar_series(uuid, int) FROM PUBLIC;
REVOKE ALL ON FUNCTION recommend_series(uuid[], int) FROM PUBLIC;
REVOKE ALL ON FUNCTION recommend_for_me(int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION similar_series(uuid, int) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION recommend_series(uuid[], int) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION recommend_for_me(int) TO authenticated, service_role;

NOTIFY pgrst, 'reload schema';

-- 確認 (実行後に別に流す): 件数が 8,204 前後で、テーブルの大きさが 150MB 前後であること
-- SELECT count(*), pg_size_pretty(pg_total_relation_size('series_vectors')) FROM series_vectors;

-- ---------------------------------------------------------------------
-- 元に戻すとき (DOWN)
-- ---------------------------------------------------------------------
-- DROP TRIGGER IF EXISTS trg_sync_series_vectors ON openbd_test_series;
-- DROP FUNCTION IF EXISTS recommend_for_me(int);
-- DROP FUNCTION IF EXISTS recommend_series(uuid[], int);
-- DROP FUNCTION IF EXISTS similar_series(uuid, int);
-- DROP FUNCTION IF EXISTS sync_series_vectors();
-- DROP TABLE IF EXISTS series_vectors;
-- (034 の関数に戻すなら、このあと 034 を実行し直す)
