"""
openbd_test_books の author が NULL/空 の書籍を hanmoto.com で調べて補完する

使い方:
  python -m scripts.enrich_author_hanmoto --dry-run
  python -m scripts.enrich_author_hanmoto
  python -m scripts.enrich_author_hanmoto --limit 50
  python -m scripts.enrich_author_hanmoto --imprint 電撃文庫
"""
import sys
import os
import time
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase
from scripts.discover_openbd import fetch_hanmoto_metadata, SLEEP_BETWEEN_HANMOTO


def fetch_target_rows(supabase, imprint: str | None, limit: int | None) -> list[dict]:
    """author が NULL または空文字列の行を取得"""
    # NULL を取得
    q_null = (supabase.table("openbd_test_books")
              .select("isbn, title, imprint, author")
              .is_("author", "null"))
    if imprint:
        q_null = q_null.eq("imprint", imprint)
    if limit:
        q_null = q_null.limit(limit)
    rows_null = q_null.execute().data or []

    # 空文字列を取得
    q_empty = (supabase.table("openbd_test_books")
               .select("isbn, title, imprint, author")
               .eq("author", ""))
    if imprint:
        q_empty = q_empty.eq("imprint", imprint)
    if limit:
        q_empty = q_empty.limit(limit)
    rows_empty = q_empty.execute().data or []

    seen: set[str] = set()
    merged: list[dict] = []
    for r in rows_null + rows_empty:
        if r["isbn"] in seen:
            continue
        seen.add(r["isbn"])
        merged.append(r)
        if limit and len(merged) >= limit:
            break
    return merged


def main(imprint: str | None, limit: int | None, dry_run: bool):
    supabase = get_supabase()
    rows = fetch_target_rows(supabase, imprint, limit)
    print(f"[対象] author が NULL/空 の書籍: {len(rows)}件"
          + (f" (imprint='{imprint}')" if imprint else ""))
    if not rows:
        return

    updated = 0
    no_author = 0
    fetch_failed = 0
    for idx, row in enumerate(rows):
        isbn = row["isbn"]
        if idx > 0:
            time.sleep(SLEEP_BETWEEN_HANMOTO)
        meta = fetch_hanmoto_metadata(isbn, verbose=True)
        if not meta:
            fetch_failed += 1
            continue
        author = meta.get("author")
        if not author:
            no_author += 1
            print(f"  [{idx+1}/{len(rows)}] {isbn} hanmoto も author なし: 「{row.get('title')}」")
            continue

        print(f"  [{idx+1}/{len(rows)}] {isbn} → author='{author}'  「{row.get('title')}」")
        if not dry_run:
            try:
                supabase.table("openbd_test_books").update({"author": author}).eq("isbn", isbn).execute()
                updated += 1
            except Exception as e:
                print(f"    ! 更新失敗 {isbn}: {e}")

    print(f"\n[完了] 更新 {updated}件 / 試行 {len(rows)}件 "
          f"(取得失敗 {fetch_failed} / hanmoto側もauthorなし {no_author})")
    if dry_run:
        print("[DRY RUN] 実際のDB更新はスキップしています")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="author が NULL/空 の openbd_test_books 行を hanmoto.com で補完")
    parser.add_argument("--imprint", default=None, help="対象imprintで絞る（例: 電撃文庫）")
    parser.add_argument("--limit", type=int, default=None, help="処理件数上限")
    parser.add_argument("--dry-run", action="store_true", help="DBを更新しない")
    args = parser.parse_args()

    main(imprint=args.imprint, limit=args.limit, dry_run=args.dry_run)
