-- =====================================================================
-- 034_vector_search_rpc.sql
-- 6観点 1536 ベクトル (032/033) を HNSW で引く検索用の RPC 関数。
-- 今の app/services/vector_search.py (npz・全件 cosine) の代わりに、ranobeNext から呼ぶ。
--
--   search_semantic   自然言語検索: 観点ごとのクエリベクトルで ANN → 候補を厳密に再計算 → Σ w·cos
--   similar_series    似ているシリーズ: 観点ごとに ANN → 候補の中での順位 p → Σ (p + p²)
--   recommend_series  複数作品の重心に近い作品 (similar と同じ合成)
--   recommend_for_me  ログインユーザーのお気に入り + 読了 (各50件) から recommend_series
--
-- 全件ではなく「観点ごとの上位 p_k 件の和集合」を候補にするので、今の実装と順位が少しずれる。
-- p_k の既定値 (自然言語 100 / 似ている・おすすめ 50) は、Micro インスタンス (メモリ 1GB) で
-- 候補のベクトルをディスクから読む量を抑えるための値。300 にすると数秒〜タイムアウトになった。
-- ずれの大きさは scripts.compare_search_parity で確かめる (受け入れ基準は requirements.md §8-4)。
--
-- 推薦理由・共通点のラベル (ui-design.md §5) は ranobeNext の実装に合わせて後の migration で足す。
-- 実行: SQL Editor でまるごと実行してよい (CREATE INDEX を含まないのでトランザクションで問題ない)。
-- plpgsql の関数は RETURNS TABLE の列名 (series_id, score) が列名と衝突しないよう #variable_conflict use_column を付けている。
-- hnsw.ef_search / hnsw.iterative_scan は、関数の先頭で set_config(..., true) で指定する (SET 句は権限エラーになる)。
-- =====================================================================

-- ---------------------------------------------------------------------
-- ジャンル条件 (genre_tags ∪ sub_genre_tags に対して判定。今の vector_search と同じ)
-- p_genres が NULL / 空なら絞り込まない。p_mode = 'and' なら全部持つ、それ以外はどれかを持つ
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION series_genre_match(p_genre_tags text[], p_sub_genre_tags text[],
                                              p_genres text[], p_mode text)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT CASE
        WHEN p_genres IS NULL OR cardinality(p_genres) = 0 THEN true
        WHEN p_mode = 'and' THEN (coalesce(p_genre_tags, '{}') || coalesce(p_sub_genre_tags, '{}')) @> p_genres
        ELSE (coalesce(p_genre_tags, '{}') || coalesce(p_sub_genre_tags, '{}')) && p_genres
    END
$$;


-- ---------------------------------------------------------------------
-- 自然言語検索
-- 観点ごとのサブクエリを埋め込んだベクトル (NULL = その観点は使わない) と重みを受け取る。
--   分割できたとき: 中身のある観点だけベクトルを渡し、重みは 1
--   分割に失敗したとき: 6観点すべてに同じクエリベクトルを渡し、重みに説明文との近さの softmax を渡す
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION search_semantic(
    q_plot vector(1536) DEFAULT NULL, q_cast vector(1536) DEFAULT NULL,
    q_theme vector(1536) DEFAULT NULL, q_mood vector(1536) DEFAULT NULL,
    q_reader vector(1536) DEFAULT NULL, q_review vector(1536) DEFAULT NULL,
    w_plot real DEFAULT 1, w_cast real DEFAULT 1, w_theme real DEFAULT 1,
    w_mood real DEFAULT 1, w_reader real DEFAULT 1, w_review real DEFAULT 1,
    p_genres text[] DEFAULT NULL, p_genre_mode text DEFAULT 'or',
    p_k int DEFAULT 100, p_limit int DEFAULT 240)
RETURNS TABLE (series_id uuid, score real)
LANGUAGE plpgsql STABLE SECURITY INVOKER
SET search_path = public, extensions
AS $$
#variable_conflict use_column
BEGIN
    -- 拡張機能の設定は関数定義の SET 句では指定できない (Supabase の postgres はスーパーユーザーではない) ので、
    -- 実行時にこのトランザクションの中だけ変える
    PERFORM set_config('hnsw.ef_search', greatest(p_k, 40)::text, true);
    PERFORM set_config('hnsw.iterative_scan', 'relaxed_order', true);

    RETURN QUERY
    WITH cand AS (
        (SELECT s.id FROM openbd_test_series s
          WHERE q_plot IS NOT NULL AND s.plot_embedding_1536 IS NOT NULL
            AND series_genre_match(s.genre_tags, s.sub_genre_tags, p_genres, p_genre_mode)
          ORDER BY s.plot_embedding_1536 <=> q_plot LIMIT p_k)
        UNION
        (SELECT s.id FROM openbd_test_series s
          WHERE q_cast IS NOT NULL AND s.cast_embedding_1536 IS NOT NULL
            AND series_genre_match(s.genre_tags, s.sub_genre_tags, p_genres, p_genre_mode)
          ORDER BY s.cast_embedding_1536 <=> q_cast LIMIT p_k)
        UNION
        (SELECT s.id FROM openbd_test_series s
          WHERE q_theme IS NOT NULL AND s.theme_embedding_1536 IS NOT NULL
            AND series_genre_match(s.genre_tags, s.sub_genre_tags, p_genres, p_genre_mode)
          ORDER BY s.theme_embedding_1536 <=> q_theme LIMIT p_k)
        UNION
        (SELECT s.id FROM openbd_test_series s
          WHERE q_mood IS NOT NULL AND s.mood_embedding_1536 IS NOT NULL
            AND series_genre_match(s.genre_tags, s.sub_genre_tags, p_genres, p_genre_mode)
          ORDER BY s.mood_embedding_1536 <=> q_mood LIMIT p_k)
        UNION
        (SELECT s.id FROM openbd_test_series s
          WHERE q_reader IS NOT NULL AND s.reader_embedding_1536 IS NOT NULL
            AND series_genre_match(s.genre_tags, s.sub_genre_tags, p_genres, p_genre_mode)
          ORDER BY s.reader_embedding_1536 <=> q_reader LIMIT p_k)
        UNION
        (SELECT s.id FROM openbd_test_series s
          WHERE q_review IS NOT NULL AND s.review_aspect_embedding_1536 IS NOT NULL
            AND series_genre_match(s.genre_tags, s.sub_genre_tags, p_genres, p_genre_mode)
          ORDER BY s.review_aspect_embedding_1536 <=> q_review LIMIT p_k)
    )
    SELECT s.id,
           (  coalesce(w_plot   * (1 - (s.plot_embedding_1536          <=> q_plot)),   0)
            + coalesce(w_cast   * (1 - (s.cast_embedding_1536          <=> q_cast)),   0)
            + coalesce(w_theme  * (1 - (s.theme_embedding_1536         <=> q_theme)),  0)
            + coalesce(w_mood   * (1 - (s.mood_embedding_1536          <=> q_mood)),   0)
            + coalesce(w_reader * (1 - (s.reader_embedding_1536        <=> q_reader)), 0)
            + coalesce(w_review * (1 - (s.review_aspect_embedding_1536 <=> q_review)), 0))::real AS score
      FROM cand JOIN openbd_test_series s ON s.id = cand.id
     ORDER BY score DESC
     LIMIT p_limit;
END;
$$;


-- ---------------------------------------------------------------------
-- 候補の集合に対する「似ている度合い」の合成 (similar / recommend で共通)
-- 観点ごとに cos を出し、候補の中での順位 (0〜1) p にして Σ (p + p²)。
-- 今の実装は全件の中での順位だが、ANN では全件を見ないので候補の中で代用する。
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION _rank_candidates(
    p_cand uuid[],
    v_plot vector(1536), v_cast vector(1536), v_theme vector(1536),
    v_mood vector(1536), v_reader vector(1536), v_review vector(1536),
    p_limit int)
RETURNS TABLE (series_id uuid, score real)
LANGUAGE sql STABLE
SET search_path = public, extensions
AS $$
    WITH sims AS (
        SELECT s.id,
               1 - (s.plot_embedding_1536          <=> v_plot)   AS c_plot,
               1 - (s.cast_embedding_1536          <=> v_cast)   AS c_cast,
               1 - (s.theme_embedding_1536         <=> v_theme)  AS c_theme,
               1 - (s.mood_embedding_1536          <=> v_mood)   AS c_mood,
               1 - (s.reader_embedding_1536        <=> v_reader) AS c_reader,
               1 - (s.review_aspect_embedding_1536 <=> v_review) AS c_review
          FROM openbd_test_series s
         WHERE s.id = ANY (p_cand)
    ), ranked AS (
        SELECT id,
               percent_rank() OVER (ORDER BY c_plot   NULLS FIRST) AS p_plot,
               percent_rank() OVER (ORDER BY c_cast   NULLS FIRST) AS p_cast,
               percent_rank() OVER (ORDER BY c_theme  NULLS FIRST) AS p_theme,
               percent_rank() OVER (ORDER BY c_mood   NULLS FIRST) AS p_mood,
               percent_rank() OVER (ORDER BY c_reader NULLS FIRST) AS p_reader,
               percent_rank() OVER (ORDER BY c_review NULLS FIRST) AS p_review
          FROM sims
    )
    SELECT id,
           (  p_plot + p_plot ^ 2 + p_cast + p_cast ^ 2 + p_theme + p_theme ^ 2
            + p_mood + p_mood ^ 2 + p_reader + p_reader ^ 2 + p_review + p_review ^ 2)::real AS score
      FROM ranked
     ORDER BY score DESC
     LIMIT p_limit;
$$;


-- ---------------------------------------------------------------------
-- 似ているシリーズ
-- p_auto_genre = true なら、基準作品のジャンル (genre_tags) のうち一番珍しいものを持つ作品だけに絞る
-- (珍しさは tags.usage_count。scripts.recount_tag_usage が更新する。今の vector_search は
--  ベクトルのある作品だけで数えていたが、行を数え直すと遅いので集計済みの値で代用する)
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION similar_series(p_series_id uuid, p_limit int DEFAULT 12,
                                          p_auto_genre boolean DEFAULT true, p_k int DEFAULT 50)
RETURNS TABLE (series_id uuid, score real)
LANGUAGE plpgsql STABLE SECURITY INVOKER
SET search_path = public, extensions
AS $$
#variable_conflict use_column
DECLARE
    b record;
    v_required text[];
    v_cand uuid[];
BEGIN
    -- 拡張機能の設定は関数定義の SET 句では指定できない (Supabase の postgres はスーパーユーザーではない) ので、
    -- 実行時にこのトランザクションの中だけ変える
    PERFORM set_config('hnsw.ef_search', greatest(p_k, 40)::text, true);
    PERFORM set_config('hnsw.iterative_scan', 'relaxed_order', true);

    -- 必要な列だけ読む (SELECT * だと旧 3072 列まで取り出して遅くなる)
    SELECT s.genre_tags,
           s.plot_embedding_1536 AS plot, s.cast_embedding_1536 AS cast_, s.theme_embedding_1536 AS theme,
           s.mood_embedding_1536 AS mood, s.reader_embedding_1536 AS reader,
           s.review_aspect_embedding_1536 AS review
      INTO b
      FROM openbd_test_series s WHERE s.id = p_series_id;
    IF NOT FOUND OR b.plot IS NULL THEN
        RETURN;
    END IF;

    -- 一番珍しいジャンル: 集計済みの tags.usage_count を使う (行を数え直すと大きなテーブルを読むので遅い)
    IF p_auto_genre AND cardinality(b.genre_tags) > 0 THEN
        SELECT ARRAY[t.tag] INTO v_required
          FROM unnest(b.genre_tags) AS t(tag)
          LEFT JOIN tags g ON g.name = t.tag
         ORDER BY coalesce(g.usage_count, 0), t.tag
         LIMIT 1;
    END IF;

    SELECT array_agg(DISTINCT c.id) INTO v_cand FROM (
        (SELECT s.id FROM openbd_test_series s
          WHERE s.id <> p_series_id AND s.plot_embedding_1536 IS NOT NULL
            AND series_genre_match(s.genre_tags, s.sub_genre_tags, v_required, 'or')
          ORDER BY s.plot_embedding_1536 <=> b.plot LIMIT p_k)
        UNION ALL
        (SELECT s.id FROM openbd_test_series s
          WHERE s.id <> p_series_id AND s.cast_embedding_1536 IS NOT NULL
            AND series_genre_match(s.genre_tags, s.sub_genre_tags, v_required, 'or')
          ORDER BY s.cast_embedding_1536 <=> b.cast_ LIMIT p_k)
        UNION ALL
        (SELECT s.id FROM openbd_test_series s
          WHERE s.id <> p_series_id AND s.theme_embedding_1536 IS NOT NULL
            AND series_genre_match(s.genre_tags, s.sub_genre_tags, v_required, 'or')
          ORDER BY s.theme_embedding_1536 <=> b.theme LIMIT p_k)
        UNION ALL
        (SELECT s.id FROM openbd_test_series s
          WHERE s.id <> p_series_id AND s.mood_embedding_1536 IS NOT NULL
            AND series_genre_match(s.genre_tags, s.sub_genre_tags, v_required, 'or')
          ORDER BY s.mood_embedding_1536 <=> b.mood LIMIT p_k)
        UNION ALL
        (SELECT s.id FROM openbd_test_series s
          WHERE s.id <> p_series_id AND s.reader_embedding_1536 IS NOT NULL
            AND series_genre_match(s.genre_tags, s.sub_genre_tags, v_required, 'or')
          ORDER BY s.reader_embedding_1536 <=> b.reader LIMIT p_k)
        UNION ALL
        (SELECT s.id FROM openbd_test_series s
          WHERE s.id <> p_series_id AND s.review_aspect_embedding_1536 IS NOT NULL
            AND series_genre_match(s.genre_tags, s.sub_genre_tags, v_required, 'or')
          ORDER BY s.review_aspect_embedding_1536 <=> b.review LIMIT p_k)
    ) c;

    IF v_cand IS NULL THEN
        RETURN;
    END IF;
    RETURN QUERY SELECT * FROM _rank_candidates(
        v_cand, b.plot, b.cast_, b.theme, b.mood, b.reader, b.review, p_limit);
END;
$$;


-- ---------------------------------------------------------------------
-- 複数作品の重心に近い作品
-- 正規化済みベクトルの cos の平均は「平均ベクトルとの内積」に等しいので、観点ごとに avg() で重心を作る
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION recommend_series(p_series_ids uuid[], p_limit int DEFAULT 12, p_k int DEFAULT 50)
RETURNS TABLE (series_id uuid, score real)
LANGUAGE plpgsql STABLE SECURITY INVOKER
SET search_path = public, extensions
AS $$
#variable_conflict use_column
DECLARE
    c_plot vector(1536); c_cast vector(1536); c_theme vector(1536);
    c_mood vector(1536); c_reader vector(1536); c_review vector(1536);
    v_cand uuid[];
BEGIN
    -- 拡張機能の設定は関数定義の SET 句では指定できない (Supabase の postgres はスーパーユーザーではない) ので、
    -- 実行時にこのトランザクションの中だけ変える
    PERFORM set_config('hnsw.ef_search', greatest(p_k, 40)::text, true);
    PERFORM set_config('hnsw.iterative_scan', 'relaxed_order', true);

    SELECT avg(plot_embedding_1536), avg(cast_embedding_1536), avg(theme_embedding_1536),
           avg(mood_embedding_1536), avg(reader_embedding_1536), avg(review_aspect_embedding_1536)
      INTO c_plot, c_cast, c_theme, c_mood, c_reader, c_review
      FROM openbd_test_series
     WHERE id = ANY (p_series_ids) AND plot_embedding_1536 IS NOT NULL;
    IF c_plot IS NULL THEN
        RETURN;
    END IF;

    SELECT array_agg(DISTINCT c.id) INTO v_cand FROM (
        (SELECT s.id FROM openbd_test_series s
          WHERE s.plot_embedding_1536 IS NOT NULL AND NOT (s.id = ANY (p_series_ids))
          ORDER BY s.plot_embedding_1536 <=> c_plot LIMIT p_k)
        UNION ALL
        (SELECT s.id FROM openbd_test_series s
          WHERE s.cast_embedding_1536 IS NOT NULL AND NOT (s.id = ANY (p_series_ids))
          ORDER BY s.cast_embedding_1536 <=> c_cast LIMIT p_k)
        UNION ALL
        (SELECT s.id FROM openbd_test_series s
          WHERE s.theme_embedding_1536 IS NOT NULL AND NOT (s.id = ANY (p_series_ids))
          ORDER BY s.theme_embedding_1536 <=> c_theme LIMIT p_k)
        UNION ALL
        (SELECT s.id FROM openbd_test_series s
          WHERE s.mood_embedding_1536 IS NOT NULL AND NOT (s.id = ANY (p_series_ids))
          ORDER BY s.mood_embedding_1536 <=> c_mood LIMIT p_k)
        UNION ALL
        (SELECT s.id FROM openbd_test_series s
          WHERE s.reader_embedding_1536 IS NOT NULL AND NOT (s.id = ANY (p_series_ids))
          ORDER BY s.reader_embedding_1536 <=> c_reader LIMIT p_k)
        UNION ALL
        (SELECT s.id FROM openbd_test_series s
          WHERE s.review_aspect_embedding_1536 IS NOT NULL AND NOT (s.id = ANY (p_series_ids))
          ORDER BY s.review_aspect_embedding_1536 <=> c_review LIMIT p_k)
    ) c;

    IF v_cand IS NULL THEN
        RETURN;
    END IF;
    RETURN QUERY SELECT * FROM _rank_candidates(
        v_cand, c_plot, c_cast, c_theme, c_mood, c_reader, c_review, p_limit);
END;
$$;


-- ---------------------------------------------------------------------
-- ログインユーザー向けのおすすめ (今の /users/me/recommendations と同じ元データ)
-- SECURITY INVOKER なので、favorite_series / read_series の RLS で本人の行しか読めない
-- ---------------------------------------------------------------------
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


-- ---------------------------------------------------------------------
-- 権限: 関数の EXECUTE は既定で PUBLIC に付くので、いったん外してから必要なロールにだけ付ける
-- 返すのは公開データ (series の id とスコア) だけなので anon にも許可する。
-- recommend_for_me は auth.uid() が要るので authenticated だけ
-- ---------------------------------------------------------------------
REVOKE ALL ON FUNCTION series_genre_match(text[], text[], text[], text) FROM PUBLIC;
REVOKE ALL ON FUNCTION search_semantic(vector, vector, vector, vector, vector, vector,
                                       real, real, real, real, real, real, text[], text, int, int) FROM PUBLIC;
REVOKE ALL ON FUNCTION _rank_candidates(uuid[], vector, vector, vector, vector, vector, vector, int) FROM PUBLIC;
REVOKE ALL ON FUNCTION similar_series(uuid, int, boolean, int) FROM PUBLIC;
REVOKE ALL ON FUNCTION recommend_series(uuid[], int, int) FROM PUBLIC;
REVOKE ALL ON FUNCTION recommend_for_me(int) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION series_genre_match(text[], text[], text[], text) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION search_semantic(vector, vector, vector, vector, vector, vector,
                                          real, real, real, real, real, real, text[], text, int, int)
    TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION _rank_candidates(uuid[], vector, vector, vector, vector, vector, vector, int)
    TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION similar_series(uuid, int, boolean, int) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION recommend_series(uuid[], int, int) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION recommend_for_me(int) TO authenticated, service_role;

NOTIFY pgrst, 'reload schema';

-- ---------------------------------------------------------------------
-- 元に戻すとき (DOWN)
-- ---------------------------------------------------------------------
-- DROP FUNCTION IF EXISTS recommend_for_me(int);
-- DROP FUNCTION IF EXISTS recommend_series(uuid[], int, int);
-- DROP FUNCTION IF EXISTS similar_series(uuid, int, boolean, int);
-- DROP FUNCTION IF EXISTS _rank_candidates(uuid[], vector, vector, vector, vector, vector, vector, int);
-- DROP FUNCTION IF EXISTS search_semantic(vector, vector, vector, vector, vector, vector,
--                                         real, real, real, real, real, real, text[], text, int, int);
-- DROP FUNCTION IF EXISTS series_genre_match(text[], text[], text[], text);
