-- =====================================================================
-- 040_drop_unused_vectors.sql
-- アプリが使うベクトルは theme / mood / reader の 1536 列 (series_vectors へトリガーで同期) だけになった。
-- 使わないベクトル列と HNSW が openbd_test_series に残っていると、1行の更新ごとに全 HNSW へ
-- 書き足しが起き、1件 6秒前後かかる (パイプラインの集計更新が時間切れになる)。そこで次を消す:
--
--   列 (13): embedding / review_embedding (028・029)、
--            plot / cast / theme / mood / reader / review_aspect の *_embedding (3072、031)、
--            plot / cast / review_aspect の *_embedding_1536 (032)
--            → 列と一緒に、その列の HNSW (028・029・033 の一部) も消える
--   HNSW (3): theme / mood / reader の *_embedding_1536 (033)。列は残すが、検索は series_vectors で行うので使っていない
--   関数: 消える列を読む、使わなくなった関数 (032 の変換用、034 の意味検索用)
--
-- 元のベクトルはローカルに保存済み (backups/ は gitignore):
--   3072 × 6観点 : backups/aspect_3072_20261015_094928/  (8,204件。10/15 以降の作り直しは 0件で、DB と同じ)
--   1536 × 8列   : backups/vectors_1536_<日時>/           (scripts.backup_vectors_1536 で取得・照合)
-- *_text (原文) の列は残すので、ベクトルは原文からいつでも作り直せる。
--
-- 実行: SQL Editor でまるごと実行する。DROP COLUMN は列を「消えた」印にするだけなのですぐ終わる。
--       ディスクの空きが戻るのは、表の書き直し (VACUUM FULL) をしたとき。これは別に判断する。
-- =====================================================================

SET LOCAL statement_timeout = '10min';

-- 1. 使わなくなった関数 (消える列を読むので、残すと呼んだときに失敗する)
DROP FUNCTION IF EXISTS migrate_aspect_1536_batch(uuid, int);
DROP FUNCTION IF EXISTS search_semantic(vector, vector, vector, vector, vector, vector,
                                        real, real, real, real, real, real, text[], text, int, int);
DROP FUNCTION IF EXISTS _rank_candidates(uuid[], vector, vector, vector, vector, vector, vector, int);
DROP FUNCTION IF EXISTS series_genre_match(text[], text[], text[], text);

-- 2. 使わない HNSW (列は残す 3本)
DROP INDEX IF EXISTS idx_series_theme_embedding_1536;
DROP INDEX IF EXISTS idx_series_mood_embedding_1536;
DROP INDEX IF EXISTS idx_series_reader_embedding_1536;

-- 3. 使わないベクトル列 (列の HNSW も一緒に消える)
ALTER TABLE openbd_test_series
    DROP COLUMN IF EXISTS embedding,
    DROP COLUMN IF EXISTS review_embedding,
    DROP COLUMN IF EXISTS plot_embedding,
    DROP COLUMN IF EXISTS cast_embedding,
    DROP COLUMN IF EXISTS theme_embedding,
    DROP COLUMN IF EXISTS mood_embedding,
    DROP COLUMN IF EXISTS reader_embedding,
    DROP COLUMN IF EXISTS review_aspect_embedding,
    DROP COLUMN IF EXISTS plot_embedding_1536,
    DROP COLUMN IF EXISTS cast_embedding_1536,
    DROP COLUMN IF EXISTS review_aspect_embedding_1536;

NOTIFY pgrst, 'reload schema';

-- 確認 (実行後に別に流す):
-- (1) 残っているベクトル列が3つだけであること
-- SELECT column_name FROM information_schema.columns
--  WHERE table_name = 'openbd_test_series' AND udt_name = 'vector' ORDER BY 1;
-- (2) openbd_test_series に HNSW が残っていないこと
-- SELECT indexname FROM pg_indexes WHERE tablename = 'openbd_test_series' AND indexdef ILIKE '%hnsw%';
-- (3) 消えた関数を参照する関数が残っていないこと (結果が空なら OK)
-- SELECT proname FROM pg_proc WHERE prosrc ILIKE '%plot_embedding%' OR prosrc ILIKE '%review_embedding%';

-- ---------------------------------------------------------------------
-- 元に戻すとき (DOWN)
-- ---------------------------------------------------------------------
-- 列は 028 / 029 / 031 / 032 の ADD COLUMN で作り直し、値はバックアップから書き戻す
-- (3072 は backups/aspect_3072_*、1536 は backups/vectors_1536_*。並びは各フォルダの ids.json)。
-- HNSW は 033 の CREATE INDEX をそのまま流せば作り直せる。将来ベクトル検索に使うなら、
-- 大きな表ではなく細い表 series_vectors に作る (更新が少ないので重くならない):
--   CREATE INDEX ON series_vectors USING hnsw (theme vector_cosine_ops) WITH (m = 16, ef_construction = 64);
