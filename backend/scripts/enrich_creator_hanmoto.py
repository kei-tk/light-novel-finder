"""
既存の openbd_test_books について、hanmoto.com のページから
author (A01=著) / illustrator (A12=絵) を再取得して上書きする。

旧データは「著者」「イラスト」が混ざって author 列に入っているケースがあるので、
このスクリプトで分離・正規化できる。

使い方:
  python -m scripts.enrich_creator_hanmoto --dry-run
  python -m scripts.enrich_creator_hanmoto --imprint 電撃文庫
  python -m scripts.enrich_creator_hanmoto --limit 100
  python -m scripts.enrich_creator_hanmoto --only-missing  # illustrator が NULL の行だけ
"""
import sys
import os
import time
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase
from scripts.discover_openbd import fetch_hanmoto_metadata, SLEEP_BETWEEN_HANMOTO


def fetch_target_rows(supabase, imprint: str | None, limit: int | None,
                      only_missing: bool) -> list[dict]:
    q = (supabase.table("openbd_test_books")
         .select("isbn, title, imprint, author, illustrator, contributors"))
    if imprint:
        q = q.eq("imprint", imprint)
    if only_missing:
        q = q.is_("contributors", "null")
    if limit:
        q = q.limit(limit)
    return q.execute().data or []


def _summarize_roles(contribs: list[dict]) -> str:
    if not contribs:
        return "(なし)"
    return ", ".join(f"{c['role_label']}={c['name']}" for c in contribs)


def main(imprint: str | None, limit: int | None, dry_run: bool, only_missing: bool):
    supabase = get_supabase()
    rows = fetch_target_rows(supabase, imprint, limit, only_missing)
    label = f"imprint='{imprint}'" if imprint else "全imprint"
    extra = " かつ contributors が NULL" if only_missing else ""
    print(f"[対象] {label}{extra}: {len(rows)}件")
    if not rows:
        return

    updated = 0
    no_change = 0
    fetch_failed = 0
    no_creator = 0

    for idx, row in enumerate(rows):
        isbn = row["isbn"]
        if idx > 0:
            time.sleep(SLEEP_BETWEEN_HANMOTO)
        meta = fetch_hanmoto_metadata(isbn, verbose=False)
        if not meta:
            fetch_failed += 1
            print(f"  [{idx+1}/{len(rows)}] {isbn}: hanmoto取得失敗")
            continue

        new_author = meta.get("author")
        new_illust = meta.get("illustrator")
        new_contribs = meta.get("contributors") or []
        if not new_contribs:
            no_creator += 1
            print(f"  [{idx+1}/{len(rows)}] {isbn}: hanmoto に contributor 情報なし")
            continue

        update: dict = {}
        if new_author and new_author != row.get("author"):
            update["author"] = new_author
        if new_illust and new_illust != row.get("illustrator"):
            update["illustrator"] = new_illust
        if new_contribs != (row.get("contributors") or []):
            update["contributors"] = new_contribs

        if not update:
            no_change += 1
            continue

        print(f"  [{idx+1}/{len(rows)}] {isbn} 「{(row.get('title') or '')[:40]}」: "
              f"{_summarize_roles(new_contribs)}")

        if not dry_run:
            try:
                supabase.table("openbd_test_books").update(update).eq("isbn", isbn).execute()
                updated += 1
            except Exception as e:
                print(f"    ! 更新失敗 {isbn}: {e}")

    print(f"\n[完了] 更新 {updated}件 / 変更不要 {no_change} / 取得失敗 {fetch_failed} "
          f"/ contributor無し {no_creator} / 試行 {len(rows)}件")
    if dry_run:
        print("[DRY RUN] 実際のDB更新はスキップしています")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="openbd_test_books の author(A01) / illustrator(A12) を hanmoto から再取得")
    parser.add_argument("--imprint", default=None, help="対象imprintで絞る")
    parser.add_argument("--limit", type=int, default=None, help="処理件数上限")
    parser.add_argument("--dry-run", action="store_true", help="DBを更新しない")
    parser.add_argument("--only-missing", action="store_true",
                        help="illustrator が NULL の行だけを対象にする")
    args = parser.parse_args()

    main(
        imprint=args.imprint,
        limit=args.limit,
        dry_run=args.dry_run,
        only_missing=args.only_missing,
    )
