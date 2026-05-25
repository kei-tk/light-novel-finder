"""
紐付け書籍が 0冊の孤立 series 行を一括削除する。

gemini_detect_series を再実行した時などに、書籍が新しい series_id に
再割り当てされて旧 series 行が空のまま残るケースを掃除する。

使い方:
  python -m scripts.cleanup_orphan_series --dry-run    # 件数確認
  python -m scripts.cleanup_orphan_series              # 削除実行
  python -m scripts.cleanup_orphan_series --limit 10   # サンプル表示
"""
import sys
import os
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase


def fetch_orphan_series(supabase, page_size: int = 1000) -> list[dict]:
    """書籍が0冊の series 行を全件取得"""
    print("[全 series 取得]")
    series_rows: list[dict] = []
    offset = 0
    while True:
        rs = supabase.table("openbd_test_series").select(
            "id, series_title, author, volume_count, parent_series_id, is_series"
        ).range(offset, offset + page_size - 1).execute()
        if not rs.data:
            break
        series_rows.extend(rs.data)
        if len(rs.data) < page_size:
            break
        offset += page_size
    print(f"  → {len(series_rows):,}件")

    # 全 books の series_id を集合化
    print("[全 books series_id 取得]")
    linked_ids: set[str] = set()
    offset = 0
    while True:
        rs = supabase.table("openbd_test_books").select(
            "series_id"
        ).not_.is_("series_id", "null").range(offset, offset + page_size - 1).execute()
        if not rs.data:
            break
        for r in rs.data:
            if r.get("series_id"):
                linked_ids.add(r["series_id"])
        if len(rs.data) < page_size:
            break
        offset += page_size
    print(f"  → {len(linked_ids):,}個の series_id が参照されている")

    # series テーブル側で参照されていない id を抽出
    orphans = [s for s in series_rows if s["id"] not in linked_ids]
    print(f"\n  → 孤立 series 行 {len(orphans):,}件 / 全 {len(series_rows):,}件")
    return orphans


def main(args):
    supabase = get_supabase()
    orphans = fetch_orphan_series(supabase)
    if not orphans:
        print("孤立 series なし。")
        return

    # サンプル表示
    print(f"\n[sample {min(args.limit, len(orphans))}件]")
    for r in orphans[:args.limit]:
        title = (r.get("series_title") or "")[:60]
        author = r.get("author") or "?"
        vc = r.get("volume_count")
        print(f"  [{r['id']}] {title!r}")
        print(f"    著={author} volume_count={vc}")

    if args.dry_run:
        print("\n[DRY RUN] 削除はしません")
        return

    print()
    confirm = input(f"本当に {len(orphans)}件削除しますか? [y/N]: ").strip().lower()
    if confirm not in ("y", "yes"):
        print("中断しました")
        return

    print(f"\n[削除中...]")
    deleted = 0
    failed = 0
    batch_size = 100
    for i in range(0, len(orphans), batch_size):
        batch = orphans[i:i + batch_size]
        ids = [r["id"] for r in batch]
        try:
            supabase.table("openbd_test_series").delete().in_("id", ids).execute()
            deleted += len(batch)
            print(f"  batch {i // batch_size + 1}: +{len(batch)} (累計 {deleted})")
        except Exception as e:
            failed += len(batch)
            print(f"  ! batch {i // batch_size + 1} 失敗: {e}")

    print(f"\n[完了] 削除 {deleted:,}件 / 失敗 {failed:,}件")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="孤立 series 行 (書籍 0冊) を削除")
    parser.add_argument("--dry-run", action="store_true", help="件数確認のみ")
    parser.add_argument("--limit", type=int, default=20, help="サンプル表示件数")
    args = parser.parse_args()
    main(args)
