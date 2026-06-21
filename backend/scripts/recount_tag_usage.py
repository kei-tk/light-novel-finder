"""
tags.usage_count を openbd_test_series の TEXT[] から再集計する。

これまで enrich 中の +1 ロジックには以下のズレがあった:
  - マスタタグ (is_ai_generated=false) はカウント対象外だった
  - 上書き再エンリッチ (--all) 時に古いタグを decrement していない
  - スクリプト外で TEXT[] を編集した場合はカウントが追従しない

このスクリプトは openbd_test_series 全件の atmosphere_tags / genre_tags /
sub_genre_tags を走査し、tags テーブルの usage_count を「実態と一致する値」に
直す。マスタタグも AI 生成タグも対象。

使い方:
  python -m scripts.recount_tag_usage --dry-run     # 差分のみ表示
  python -m scripts.recount_tag_usage               # 差分があるものだけ UPDATE
  python -m scripts.recount_tag_usage --verbose     # 全件表示
"""
import sys
import os
import argparse
from collections import Counter
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase


_TAG_TYPE_TO_COLUMN = {
    "atmosphere": "atmosphere_tags",
    "genre":      "genre_tags",
    "sub_genre":  "sub_genre_tags",
}


def _paginate_select(query_factory, page_size: int = 1000) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        q = query_factory()
        rs = q.range(offset, offset + page_size - 1).execute()
        batch = rs.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def count_tags_in_series(supabase) -> Counter:
    """openbd_test_series を全件走査し (tag_type, name) → 利用シリーズ数 を返す。"""
    print("[openbd_test_series 全件取得]")
    rows = _paginate_select(
        lambda: supabase.table("openbd_test_series").select(
            "id, atmosphere_tags, genre_tags, sub_genre_tags"
        )
    )
    print(f"  → {len(rows):,}シリーズ")

    counter: Counter = Counter()
    for r in rows:
        for tag_type, col in _TAG_TYPE_TO_COLUMN.items():
            for name in (r.get(col) or []):
                name = (name or "").strip()
                if not name:
                    continue
                counter[(tag_type, name)] += 1
    return counter


def fetch_all_tags(supabase) -> list[dict]:
    print("[tags マスタ全件取得]")
    rows = _paginate_select(
        lambda: supabase.table("tags").select(
            "id, name, tag_type, usage_count, is_ai_generated"
        )
    )
    print(f"  → {len(rows):,}タグ")
    return rows


def main(dry_run: bool, verbose: bool):
    supabase = get_supabase()

    actual = count_tags_in_series(supabase)
    tags = fetch_all_tags(supabase)

    # tags 側にあるが series で使われていないタグも 0 にする
    diffs: list[tuple[dict, int, int]] = []  # (row, current, actual)
    seen_keys: set[tuple[str, str]] = set()
    for t in tags:
        tt = t.get("tag_type")
        name = t.get("name")
        if tt not in _TAG_TYPE_TO_COLUMN or not name:
            continue
        key = (tt, name)
        seen_keys.add(key)
        cur = t.get("usage_count") or 0
        act = actual.get(key, 0)
        if cur != act:
            diffs.append((t, cur, act))

    # series で使われているが tags マスタに無いタグ (整合性チェック用、表示のみ)
    orphans = [
        (tt, name, cnt) for (tt, name), cnt in actual.items()
        if (tt, name) not in seen_keys
    ]

    print(f"\n[差分] {len(diffs)} タグの usage_count が実態と異なります")
    if verbose or dry_run:
        for t, cur, act in sorted(diffs, key=lambda x: -abs(x[1] - x[2]))[:50]:
            kind = "AI" if t.get("is_ai_generated") else "公式"
            print(f"  [{kind} {t['tag_type']:>10}] {t['name']:<20} "
                  f"{cur:>4} → {act:>4} (差 {act - cur:+d})")
        if len(diffs) > 50:
            print(f"  ... (他 {len(diffs) - 50}件)")

    if orphans:
        print(f"\n[警告] tags マスタに登録されていないが series で使われているタグ: {len(orphans)}件")
        for tt, name, cnt in sorted(orphans, key=lambda x: -x[2])[:20]:
            print(f"  [{tt:>10}] {name:<20} (使用シリーズ {cnt})")
        if len(orphans) > 20:
            print(f"  ... (他 {len(orphans) - 20}件)")

    if dry_run:
        print("\n[DRY RUN] 変更なし")
        return

    if not diffs:
        print("\n変更不要")
        return

    print(f"\n[更新] {len(diffs)} タグを UPDATE します")
    confirm = input("実行しますか? [y/N]: ").strip().lower()
    if confirm != "y":
        print("中止")
        return

    updated = 0
    for t, _cur, act in diffs:
        try:
            supabase.table("tags").update({"usage_count": act}).eq("id", t["id"]).execute()
            updated += 1
        except Exception as e:
            print(f"  ! UPDATE 失敗 {t.get('name')!r}: {e}")
    print(f"[完了] {updated}/{len(diffs)} 件更新")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="tags.usage_count 再集計")
    parser.add_argument("--dry-run", action="store_true",
                        help="差分のみ表示し DB を更新しない")
    parser.add_argument("--verbose", action="store_true",
                        help="差分のあるタグを全て表示")
    args = parser.parse_args()
    main(dry_run=args.dry_run, verbose=args.verbose)
