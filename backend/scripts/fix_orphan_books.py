"""
series_id も attached_to_isbn も NULL の「孤児」書籍を救済する。

gemini_detect_series で Gemini の応答漏れ等で series_id が振られなかった本を、
1冊1series 行 (is_series=false, standalone) として保存する。

使い方:
  python -m scripts.fix_orphan_books --dry-run         # 件数確認のみ
  python -m scripts.fix_orphan_books                   # 実行
  python -m scripts.fix_orphan_books --imprint 電撃文庫
"""
import sys
import os
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase


_AUTHOR_ROLE_LABELS = frozenset({"著", "原著", "原作"})
_AUTHOR_ROLE_CODES = frozenset({"A01", "A23", "A24"})


def _author_for_book(b: dict) -> str:
    """A01 著者を優先、無ければ A23/A24 で代替。全部 NULL なら '(著者不明)'"""
    author = (b.get("author") or "").strip()
    if author:
        return author
    for c in (b.get("contributors") or []):
        if not isinstance(c, dict):
            continue
        if (c.get("role_label") in _AUTHOR_ROLE_LABELS
                or c.get("role_code") in _AUTHOR_ROLE_CODES):
            n = (c.get("name") or "").strip()
            if n:
                return n
    return "(著者不明)"


def fetch_orphan_books(supabase, imprint: str | None,
                       page_size: int = 1000) -> list[dict]:
    print("[孤児 books 取得中]")
    rows: list[dict] = []
    offset = 0
    while True:
        q = supabase.table("openbd_test_books").select(
            "isbn, title, author, illustrator, contributors, "
            "imprint, publisher, cover_image_url, description, "
            "published_date"
        ).is_("series_id", "null").is_("attached_to_isbn", "null")
        if imprint:
            q = q.eq("imprint", imprint)
        rs = q.range(offset, offset + page_size - 1).execute()
        if not rs.data:
            break
        rows.extend(rs.data)
        if len(rs.data) < page_size:
            break
        offset += page_size
    print(f"  → {len(rows):,}冊")
    return rows


def main(args):
    supabase = get_supabase()
    orphans = fetch_orphan_books(supabase, args.imprint)
    if not orphans:
        print("孤児なし、終了。")
        return

    print(f"\n[sample] 5冊表示")
    for b in orphans[:5]:
        title = (b.get("title") or "")[:60]
        author = _author_for_book(b)
        print(f"  [{b['isbn']}] {title} (著={author})")

    if args.dry_run:
        print(f"\n[DRY RUN] 救済対象 {len(orphans):,}冊。実行は --dry-run を外してください")
        return

    print(f"\n[救済処理] {len(orphans):,}冊を 1冊1series で保存中...")
    saved = 0
    failed = 0
    for i, b in enumerate(orphans, 1):
        isbn = b["isbn"]
        title = (b.get("title") or "").strip() or f"(タイトル不明 {isbn})"
        author = _author_for_book(b)
        normalized_title = f"\x03orphan-fix\x03{author}\x03{isbn}"
        record = {
            "series_title": title,
            "author": author,
            "normalized_title": normalized_title,
            "is_series": False,
            "is_special_edition": False,
            "illustrator": b.get("illustrator"),
            "contributors": b.get("contributors"),
            "publisher": b.get("publisher"),
            "imprint": b.get("imprint"),
            "cover_image_url": b.get("cover_image_url"),
            "description": b.get("description"),
            "volume_count": 1,
            "first_published_date": b.get("published_date"),
            "latest_published_date": b.get("published_date"),
        }
        try:
            rs = supabase.table("openbd_test_series").upsert(
                record, on_conflict="normalized_title,author",
                returning="representation",
            ).execute()
            if rs.data:
                sid = rs.data[0]["id"]
                supabase.table("openbd_test_books").update({
                    "series_id": sid,
                    "volume_number": None,
                    "volume_number_sub": None,
                }).eq("isbn", isbn).execute()
                saved += 1
        except Exception as e:
            failed += 1
            if failed <= 5:
                print(f"  ! 失敗 {isbn}: {e}")
        if i % 200 == 0:
            print(f"  [{i}/{len(orphans)}] 進捗: 成功 {saved} / 失敗 {failed}")

    print(f"\n[完了] 成功 {saved:,} / 失敗 {failed:,}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="series_id/attached_to_isbn が NULL の孤児書籍を救済")
    parser.add_argument("--imprint", default=None, help="レーベル絞り込み")
    parser.add_argument("--dry-run", action="store_true", help="件数確認のみ")
    args = parser.parse_args()
    main(args)
