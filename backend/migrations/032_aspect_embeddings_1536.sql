-- =====================================================================
-- 032_aspect_embeddings_1536.sql
-- 6観点の埋め込みを 3072 → 1536 次元に戻し、HNSW インデックスを張れるようにする (ranobeNext 移行)。
--
-- text-embedding-3-large は「先頭から切り詰めて L2 正規化」すると dimensions 指定と同じ結果になる。
-- そのため再埋め込みはせず、既存の 3072 列から SQL だけで 1536 を作る (pgvector 0.7+ の
-- subvector / l2_normalize。本番は 0.8.0)。
--
-- このファイルでやること (どれも既存データを壊さない):
--   1. *_embedding_1536 列を追加 (NULL 可・デフォルトなしなので、メタデータの変更だけで即時に終わる)
--   2. set_updated_at() に「app.skip_updated_at = on のときは updated_at を動かさない」分岐を追加
--      (変換の UPDATE で全件の updated_at が動くと、エンリッチの --skip-updated-since 再開が壊れるため)
--   3. id 順に p_limit 件ずつ変換する関数 migrate_aspect_1536_batch() を作る
--      (REST からしか DB に触れないので、scripts.migrate_aspect_1536 が RPC で繰り返し呼ぶ (既定 50件ずつ)。
--       1回の呼び出し = 1トランザクションを短く保つ)
--
-- 旧 3072 列はここでは消さない。本番切り替えの1週間後に別の migration で DROP + リネームする。
-- HNSW インデックスは 033 (CREATE INDEX CONCURRENTLY はトランザクション内で実行できないため別ファイル)。
--
-- 実行前に: python -m scripts.backup_aspect_3072 でバックアップを取り、照合が通っていること。
-- =====================================================================

-- 1. 1536 列 -------------------------------------------------------------
ALTER TABLE openbd_test_series
    ADD COLUMN IF NOT EXISTS plot_embedding_1536          vector(1536),
    ADD COLUMN IF NOT EXISTS cast_embedding_1536          vector(1536),
    ADD COLUMN IF NOT EXISTS theme_embedding_1536         vector(1536),
    ADD COLUMN IF NOT EXISTS mood_embedding_1536          vector(1536),
    ADD COLUMN IF NOT EXISTS reader_embedding_1536        vector(1536),
    ADD COLUMN IF NOT EXISTS review_aspect_embedding_1536 vector(1536);

-- 2. updated_at トリガーの抜け道 -----------------------------------------
-- 既存の updated_at トリガー (003 / 004 / 026 など) が共有している関数。フラグが無いときの動作は今までと同じ
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    IF current_setting('app.skip_updated_at', true) = 'on' THEN
        RETURN NEW;
    END IF;
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 3. バッチ変換関数 -------------------------------------------------------
-- p_after より大きい id から p_limit 件を変換し、(処理件数, 最後の id) を返す。
-- 処理件数が 0 になったら完了。何度呼んでも同じ値になる (冪等) ので、途中で止めても再開できる。
CREATE OR REPLACE FUNCTION migrate_aspect_1536_batch(p_after uuid DEFAULT NULL, p_limit int DEFAULT 200)
RETURNS TABLE (processed int, last_id uuid)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, extensions
AS $$
DECLARE
    v_ids uuid[];
BEGIN
    -- このトランザクションの中だけ updated_at を動かさない
    PERFORM set_config('app.skip_updated_at', 'on', true);

    SELECT array_agg(t.id ORDER BY t.id) INTO v_ids
    FROM (
        SELECT s.id
        FROM openbd_test_series s
        WHERE (p_after IS NULL OR s.id > p_after)
          AND (s.plot_embedding IS NOT NULL OR s.cast_embedding IS NOT NULL
               OR s.theme_embedding IS NOT NULL OR s.mood_embedding IS NOT NULL
               OR s.reader_embedding IS NOT NULL OR s.review_aspect_embedding IS NOT NULL)
        ORDER BY s.id
        LIMIT p_limit
    ) t;

    IF v_ids IS NULL THEN
        RETURN QUERY SELECT 0, NULL::uuid;
        RETURN;
    END IF;

    -- 行ロックは id 順の1文でまとめて取る (デッドロックを避ける)。NULL は NULL のまま
    UPDATE openbd_test_series s SET
        plot_embedding_1536          = l2_normalize(subvector(s.plot_embedding,          1, 1536)),
        cast_embedding_1536          = l2_normalize(subvector(s.cast_embedding,          1, 1536)),
        theme_embedding_1536         = l2_normalize(subvector(s.theme_embedding,         1, 1536)),
        mood_embedding_1536          = l2_normalize(subvector(s.mood_embedding,          1, 1536)),
        reader_embedding_1536        = l2_normalize(subvector(s.reader_embedding,        1, 1536)),
        review_aspect_embedding_1536 = l2_normalize(subvector(s.review_aspect_embedding, 1, 1536))
    WHERE s.id = ANY (v_ids);

    RETURN QUERY SELECT cardinality(v_ids), v_ids[cardinality(v_ids)];
END;
$$;

-- 関数の EXECUTE は既定で PUBLIC に付くので外し、service_role (パイプライン) だけに許可する
REVOKE ALL ON FUNCTION migrate_aspect_1536_batch(uuid, int) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION migrate_aspect_1536_batch(uuid, int) TO service_role;

-- PostgREST に新しい列と関数を認識させる
NOTIFY pgrst, 'reload schema';

-- ---------------------------------------------------------------------
-- 元に戻すとき (DOWN)。旧 3072 列には触れていないので、これだけで元の状態に戻る
-- ---------------------------------------------------------------------
-- DROP FUNCTION IF EXISTS migrate_aspect_1536_batch(uuid, int);
-- ALTER TABLE openbd_test_series
--     DROP COLUMN IF EXISTS plot_embedding_1536,
--     DROP COLUMN IF EXISTS cast_embedding_1536,
--     DROP COLUMN IF EXISTS theme_embedding_1536,
--     DROP COLUMN IF EXISTS mood_embedding_1536,
--     DROP COLUMN IF EXISTS reader_embedding_1536,
--     DROP COLUMN IF EXISTS review_aspect_embedding_1536;
-- (set_updated_at() はフラグが無ければ従来と同じ動作なので、戻さなくてよい)
