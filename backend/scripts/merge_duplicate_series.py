"""
同名+同著者で重複している openbd_test_series 行をマージする。

監査スクリプト H で検出される 26グループ / 68行のような重複を解消する。

ロジック:
  1. (NFKC正規化 series_title, NFKC正規化 author) でグループ化
  2. 各グループから「保留シリーズ」を選択:
       優先1: エンリッチ済み (popularity_score IS NOT NULL)
       優先2: 紐付け書籍数の多い方
       優先3: id が辞書順で早い方 (安定)
  3. 削除予定 series の紐付け書籍の series_id を保留 series に張り替え
  4. 削除予定 series 行を削除
  5. 保留 series の volume_count を更新

使い方:
  python -m scripts.merge_duplicate_series --dry-run
  python -m scripts.merge_duplicate_series                  # 実行
  python -m scripts.merge_duplicate_series --limit 5        # 最初の N グループのみ
"""
import sys
import os
import argparse
import unicodedata
from collections import defaultdict
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase


def _paginate(qf, ps: int = 1000) -> list[dict]:
    rows = []
    off = 0
    while True:
        rs = qf().range(off, off + ps - 1).execute()
        if not rs.data:
            break
        rows.extend(rs.data)
        if len(rs.data) < ps:
            break
        off += ps
    return rows


def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").strip().lower()


def _pick_keeper(group: list[dict], book_count_map: dict[str, int]) -> dict:
    """保留 series を選択。優先順位:
      1. エンリッチ済み (popularity_score IS NOT NULL)
      2. 紐付け書籍数が多い
      3. popularity_score が高い (Gemini の判定がより確信ありとみなす)
      4. id 辞書順 (安定したタイブレーク)
    """
    def score(s):
        enriched = 0 if s.get("popularity_score") is None else 1
        nbooks = book_count_map.get(s["id"], 0)
        pop = s.get("popularity_score") or 0
        return (enriched, nbooks, pop, s["id"])
    return max(group, key=score)


def find_duplicates(supabase):
    print("[全 series 取得]")
    series_rows = _paginate(lambda: supabase.table("openbd_test_series").select(
        "id, series_title, author, volume_count, popularity_score, parent_series_id"
    ))
    print(f"  → {len(series_rows):,}件")

    print("[全 books series_id 集計]")
    books = _paginate(lambda: supabase.table("openbd_test_books").select(
        "isbn, series_id"
    ).not_.is_("series_id", "null"))
    print(f"  → {len(books):,}件")
    book_count: dict[str, int] = defaultdict(int)
    for b in books:
        book_count[b["series_id"]] += 1

    # (title, author) でグループ化 (空タイトルは除外)
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for s in series_rows:
        title = _nfkc(s.get("series_title") or "")
        author = _nfkc(s.get("author") or "")
        if not title:
            continue
        by_key[(title, author)].append(s)

    dups = {k: v for k, v in by_key.items() if len(v) > 1}
    return dups, book_count


def merge_group(supabase, group: list[dict], book_count: dict[str, int], dry_run: bool) -> dict:
    """1グループをマージ。戻り値は統計 dict"""
    keeper = _pick_keeper(group, book_count)
    losers = [s for s in group if s["id"] != keeper["id"]]
    loser_ids = [s["id"] for s in losers]

    keeper_books = book_count.get(keeper["id"], 0)
    loser_books = sum(book_count.get(lid, 0) for lid in loser_ids)
    new_volume_count = keeper_books + loser_books

    stats = {
        "keeper_id": keeper["id"],
        "keeper_title": keeper.get("series_title"),
        "keeper_books_before": keeper_books,
        "loser_ids": loser_ids,
        "loser_books_total": loser_books,
        "new_volume_count": new_volume_count,
    }

    if dry_run:
        return stats

    # 1) loser 配下の books の series_id を keeper に張り替え
    if loser_ids:
        # parent_series_id が loser を指している series も付け替える (子シリーズ救済)
        supabase.table("openbd_test_series").update(
            {"parent_series_id": keeper["id"]}
        ).in_("parent_series_id", loser_ids).execute()

        supabase.table("openbd_test_books").update(
            {"series_id": keeper["id"]}
        ).in_("series_id", loser_ids).execute()

    # 2) keeper の volume_count 更新
    supabase.table("openbd_test_series").update(
        {"volume_count": new_volume_count}
    ).eq("id", keeper["id"]).execute()

    # 3) loser series 行を削除
    if loser_ids:
        supabase.table("openbd_test_series").delete().in_("id", loser_ids).execute()

    return stats


def main(args):
    supabase = get_supabase()
    dups, book_count = find_duplicates(supabase)

    print(f"\n重複グループ: {len(dups):,} / 合計重複 series 行: {sum(len(v) for v in dups.values()):,}")
    if not dups:
        print("重複なし。")
        return

    targets = list(dups.items())
    if args.limit:
        targets = targets[:args.limit]

    mode = "DRY-RUN" if args.dry_run else "実行"
    print(f"\n[{mode}] {len(targets):,}グループ処理\n")

    total_deleted = 0
    total_relinked = 0
    for i, ((title, author), group) in enumerate(targets, 1):
        # 各 series の本数を表示
        items_desc = " / ".join(
            f"[{s['id'][:8]}] vc={s.get('volume_count')} pop={s.get('popularity_score')} books={book_count.get(s['id'], 0)}"
            for s in group
        )
        print(f"[{i}/{len(targets)}] {title!r} 著={author!r}")
        print(f"  → {items_desc}")

        try:
            stats = merge_group(supabase, group, book_count, args.dry_run)
            print(f"  ✓ keeper={stats['keeper_id'][:8]} (books {stats['keeper_books_before']} + {stats['loser_books_total']} = {stats['new_volume_count']})")
            print(f"    削除 series: {[lid[:8] for lid in stats['loser_ids']]}")
            total_deleted += len(stats["loser_ids"])
            total_relinked += stats["loser_books_total"]
        except Exception as e:
            print(f"  ! 失敗: {e}")
        print()

    print(f"========== {mode} 完了 ==========")
    print(f"  削除 series 行数: {total_deleted}")
    print(f"  張り替え books 数: {total_relinked}")
    if args.dry_run:
        print("\n※ --dry-run 指定中。実際の変更はされていません。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="重複 series 行をマージ")
    parser.add_argument("--dry-run", action="store_true", help="変更せず計画のみ表示")
    parser.add_argument("--limit", type=int, help="先頭 N グループのみ処理")
    args = parser.parse_args()
    main(args)
