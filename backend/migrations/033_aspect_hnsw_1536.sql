-- =====================================================================
-- 033_aspect_hnsw_1536.sql
-- 032 で作った 6観点の 1536 列に HNSW インデックス (cosine) を張る。
--
-- 実行の条件: python -m scripts.migrate_aspect_1536 --verify が通っていること
--   (変換が終わる前に張ると、変換の UPDATE のたびにインデックスも更新されて遅くなる)
--
-- CONCURRENTLY は使わない。Supabase の SQL Editor は複数の文を1つのトランザクションで実行するため、
-- CREATE INDEX CONCURRENTLY が「cannot run inside a transaction block」で失敗する。
-- 対象は 8,204件と少なく、1本あたり数秒〜数十秒で終わるので、通常の CREATE INDEX で十分。
--   - 作成中は openbd_test_series への書き込み (パイプライン) だけが待たされる。読み取りは止まらない
--   - パイプラインのスクリプトを動かしていない時間に、このファイルをまるごと実行する
--   - 1つのトランザクションなので、途中で失敗したら6本とも作られない (やり直せばよい)
--
-- パラメータは 028〜030 と同じ m=16, ef_construction=64。
-- 検索時の ef_search / iterative_scan は、RPC 関数の SET 句で指定する (034 以降)。
-- =====================================================================

-- このトランザクションの中だけ有効な設定
-- - 並列ビルドを使わない: 並列だと maintenance_work_mem 分を共有メモリ (/dev/shm) に確保しようとして、
--   Supabase のインスタンスでは「could not resize shared memory segment ... No space left on device」になる
-- - 1本あたりのグラフは 8,204件 × 約6.7KB ≈ 55MB なので、128MB あればメモリ内に収まる
-- - SQL Editor の時間制限で切られないように
SET LOCAL max_parallel_maintenance_workers = 0;
SET LOCAL maintenance_work_mem = '128MB';
SET LOCAL statement_timeout = '15min';

CREATE INDEX IF NOT EXISTS idx_series_plot_embedding_1536
    ON openbd_test_series USING hnsw (plot_embedding_1536 vector_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_series_cast_embedding_1536
    ON openbd_test_series USING hnsw (cast_embedding_1536 vector_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_series_theme_embedding_1536
    ON openbd_test_series USING hnsw (theme_embedding_1536 vector_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_series_mood_embedding_1536
    ON openbd_test_series USING hnsw (mood_embedding_1536 vector_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_series_reader_embedding_1536
    ON openbd_test_series USING hnsw (reader_embedding_1536 vector_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_series_review_aspect_embedding_1536
    ON openbd_test_series USING hnsw (review_aspect_embedding_1536 vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- 確認 (実行後に別に流す): 6本そろっていて、どれも有効 (indisvalid = true) であること
-- SELECT c.relname, i.indisvalid, pg_size_pretty(pg_relation_size(c.oid)) AS size
--   FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
--  WHERE c.relname LIKE 'idx_series_%_embedding_1536'
--  ORDER BY c.relname;

-- 元に戻すとき (DOWN)
-- DROP INDEX IF EXISTS idx_series_plot_embedding_1536;
-- DROP INDEX IF EXISTS idx_series_cast_embedding_1536;
-- DROP INDEX IF EXISTS idx_series_theme_embedding_1536;
-- DROP INDEX IF EXISTS idx_series_mood_embedding_1536;
-- DROP INDEX IF EXISTS idx_series_reader_embedding_1536;
-- DROP INDEX IF EXISTS idx_series_review_aspect_embedding_1536;
