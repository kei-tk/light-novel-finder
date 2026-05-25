-- =====================================================================
-- 020_book_attached_to.sql
-- 特装版・限定版を書籍レベルで本編書籍に紐付ける attached_to_isbn 列を追加。
-- これまで openbd_test_series に is_special_edition=true で別 series 行を
-- 作っていたが、今後は本編書籍 (ISBN) に直接ぶら下げる。
--
-- 例:
--   通常版「魔女の旅々10」 isbn=A          attached_to_isbn=NULL  series_id=メイン
--   特装版「魔女の旅々10 設定資料集付き特装版」 isbn=B attached_to_isbn=A series_id=NULL
-- =====================================================================

ALTER TABLE openbd_test_books
    ADD COLUMN IF NOT EXISTS attached_to_isbn TEXT
        REFERENCES openbd_test_books(isbn) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_openbd_test_books_attached_to
    ON openbd_test_books(attached_to_isbn);
