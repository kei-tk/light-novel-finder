"""
openbd_test_books の published_date が NULL の書籍を hanmoto.com で調べて補完する。

データソース優先順:
  1. 書店発売日 (og:books:release_date)
  2. 初版年月日 (book-dates-publish dd) ← フォールバック

使い方:
  python -m scripts.enrich_pubdate_hanmoto --dry-run        # 何が更新されるか確認
  python -m scripts.enrich_pubdate_hanmoto                  # 実際に更新
  python -m scripts.enrich_pubdate_hanmoto --limit 50       # 50件だけ試す
  python -m scripts.enrich_pubdate_hanmoto --imprint 電撃文庫
  python -m scripts.enrich_pubdate_hanmoto --fill-all       # 発売日以外の欠損(cover/desc/author)も補完
"""
import sys
import os
import time
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase
from scripts.discover_openbd import fetch_hanmoto_metadata, SLEEP_BETWEEN_HANMOTO


def fetch_target_rows(supabase, imprint: str | None, limit: int | None,
                       page_size: int = 1000) -> list[dict]:
    """published_date が NULL の行を全件取得 (pagination 対応)"""
    if limit:
        # 限定モード: 1ページだけ
        q = (supabase.table("openbd_test_books")
             .select("isbn, title, imprint, author, cover_image_url, description")
             .is_("published_date", "null"))
        if imprint:
            q = q.eq("imprint", imprint)
        return q.limit(limit).execute().data or []

    rows: list[dict] = []
    offset = 0
    while True:
        q = (supabase.table("openbd_test_books")
             .select("isbn, title, imprint, author, cover_image_url, description")
             .is_("published_date", "null"))
        if imprint:
            q = q.eq("imprint", imprint)
        rs = q.range(offset, offset + page_size - 1).execute()
        if not rs.data:
            break
        rows.extend(rs.data)
        if len(rs.data) < page_size:
            break
        offset += page_size
    return rows


def main(imprint: str | None, limit: int | None, dry_run: bool, fill_all: bool):
    supabase = get_supabase()
    rows = fetch_target_rows(supabase, imprint, limit)
    print(f"[対象] published_date IS NULL な書籍: {len(rows)}件"
          + (f" (imprint='{imprint}')" if imprint else ""))
    if not rows:
        return

    updated = 0
    no_date = 0
    fetch_failed = 0
    for idx, row in enumerate(rows):
        isbn = row["isbn"]
        if idx > 0:
            time.sleep(SLEEP_BETWEEN_HANMOTO)
        meta = fetch_hanmoto_metadata(isbn, verbose=True)
        if not meta:
            fetch_failed += 1
            continue

        update: dict = {}
        if meta["published_date"]:
            update["published_date"] = meta["published_date"]
        if fill_all:
            if not row.get("cover_image_url") and meta["cover_image_url"]:
                update["cover_image_url"] = meta["cover_image_url"]
            if not row.get("description") and meta["description"]:
                update["description"] = meta["description"]
            if not row.get("author") and meta["author"]:
                update["author"] = meta["author"]

        if not update:
            no_date += 1
            print(f"  [{idx+1}/{len(rows)}] {isbn} hanmoto も発売日なし: 「{row.get('title')}」")
            continue

        date_part = update.get("published_date", "(変更なし)")
        extra_keys = [k for k in update.keys() if k != "published_date"]
        extra_part = f" + {extra_keys}" if extra_keys else ""
        print(f"  [{idx+1}/{len(rows)}] {isbn} → {date_part}{extra_part}  「{row.get('title')}」")

        if not dry_run:
            try:
                supabase.table("openbd_test_books").update(update).eq("isbn", isbn).execute()
                updated += 1
            except Exception as e:
                print(f"    ! 更新失敗 {isbn}: {e}")

    print(f"\n[完了] 更新 {updated}件 / 試行 {len(rows)}件 "
          f"(取得失敗 {fetch_failed} / hanmoto側も日付なし {no_date})")
    if dry_run:
        print("[DRY RUN] 実際のDB更新はスキップしています")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="published_date が NULL の openbd_test_books 行を hanmoto.com で補完")
    parser.add_argument("--imprint", default=None,
                        help="対象imprintで絞る（例: 電撃文庫）。未指定で全imprint")
    parser.add_argument("--limit", type=int, default=None, help="処理件数上限（試行用）")
    parser.add_argument("--dry-run", action="store_true", help="DBを更新しない")
    parser.add_argument("--fill-all", action="store_true",
                        help="発売日以外の欠損(cover_image_url/description/author)も補完")
    args = parser.parse_args()

    main(
        imprint=args.imprint,
        limit=args.limit,
        dry_run=args.dry_run,
        fill_all=args.fill_all,
    )
