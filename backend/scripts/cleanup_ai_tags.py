"""
AI が自動生成したタグ (is_ai_generated=true) を一括管理・削除する。

公式タグ (手動キュレーション = is_ai_generated=false) は touch しない。

使い方:
  python -m scripts.cleanup_ai_tags --list                 # 全 AI タグを表示
  python -m scripts.cleanup_ai_tags --list --type atmosphere
  python -m scripts.cleanup_ai_tags --list --min-usage 5   # 5回以上使われた AI タグだけ表示

  python -m scripts.cleanup_ai_tags --delete-all                  # 全 AI タグ削除 (確認あり)
  python -m scripts.cleanup_ai_tags --delete-unused               # usage_count==0 のみ削除
  python -m scripts.cleanup_ai_tags --delete-below 2              # usage_count < 2 を削除
  python -m scripts.cleanup_ai_tags --delete-name タグA --delete-name タグB  # 特定タグ削除
  python -m scripts.cleanup_ai_tags --delete-all --purge          # series 行の配列からも除去
  python -m scripts.cleanup_ai_tags --delete-all --dry-run        # 削除候補確認のみ

  python -m scripts.cleanup_ai_tags --promote タグA               # AI生成 → 公式マスタへ昇格
"""
import sys
import os
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase


_TAG_TYPE_TO_COLUMN = {
    "atmosphere": "atmosphere_tags",
    "genre":      "genre_tags",
    "sub_genre":  "sub_genre_tags",
}


def fetch_ai_tags(supabase, tag_type: str | None = None,
                  min_usage: int = 0) -> list[dict]:
    q = supabase.table("tags").select(
        "id, name, tag_type, usage_count, first_seen_at"
    ).eq("is_ai_generated", True).gte("usage_count", min_usage)
    if tag_type:
        q = q.eq("tag_type", tag_type)
    rs = q.order("usage_count", desc=True).execute()
    return rs.data or []


def list_ai_tags(supabase, tag_type: str | None, min_usage: int):
    rows = fetch_ai_tags(supabase, tag_type, min_usage)
    if not rows:
        print("AI タグなし")
        return
    from collections import Counter
    by_type = Counter(r["tag_type"] for r in rows)
    print(f"[AI 生成タグ] 計 {len(rows)}件: {dict(by_type)}\n")
    type_groups: dict[str, list[dict]] = {}
    for r in rows:
        type_groups.setdefault(r["tag_type"], []).append(r)
    for tt, group in type_groups.items():
        print(f"\n--- {tt} ({len(group)}件) ---")
        for r in group:
            print(f"  [usage={r['usage_count']:>3}] {r['name']}"
                  f"  (since {r.get('first_seen_at', '?')[:10]})")


def purge_from_series(supabase, name: str, tag_type: str):
    """series 行の配列カラムから該当タグ名を除去"""
    col = _TAG_TYPE_TO_COLUMN.get(tag_type)
    if not col:
        return 0
    # PostgreSQL の配列から要素を除去するには SQL を直接叩く必要があるため、
    # まず対象行を fetch して update で書き戻す方式にする (Supabase の制約)
    affected = 0
    offset = 0
    while True:
        rs = supabase.table("openbd_test_series").select(f"id, {col}").contains(
            col, [name]).range(offset, offset + 999).execute()
        rows = rs.data or []
        if not rows:
            break
        for r in rows:
            arr = r.get(col) or []
            new_arr = [t for t in arr if t != name]
            if new_arr != arr:
                try:
                    supabase.table("openbd_test_series").update({
                        col: new_arr,
                    }).eq("id", r["id"]).execute()
                    affected += 1
                except Exception as e:
                    print(f"    ! purge 失敗 series_id={r['id']}: {e}")
        if len(rows) < 1000:
            break
        offset += 1000
    return affected


def delete_tags(supabase, candidates: list[dict], purge: bool, dry_run: bool):
    if not candidates:
        print("削除対象なし")
        return
    print(f"\n[削除対象] {len(candidates)}件")
    for r in candidates[:30]:
        print(f"  [{r['tag_type']}] usage={r['usage_count']:>3}  {r['name']}")
    if len(candidates) > 30:
        print(f"  ... 他 {len(candidates) - 30}件")

    if dry_run:
        print("\n[DRY RUN] 削除はしません")
        return

    print()
    confirm = input(f"本当に {len(candidates)}件削除しますか? [y/N]: ").strip().lower()
    if confirm not in ("y", "yes"):
        print("中断しました")
        return

    deleted = 0
    purged_rows_total = 0
    for r in candidates:
        if purge:
            purged = purge_from_series(supabase, r["name"], r["tag_type"])
            purged_rows_total += purged
            if purged:
                print(f"  purged from {purged} series rows: {r['name']}")
        try:
            supabase.table("tags").delete().eq("id", r["id"]).execute()
            deleted += 1
        except Exception as e:
            print(f"  ! tags 削除失敗 {r['name']}: {e}")

    print(f"\n[完了] tags から {deleted}件削除"
          + (f" / series 行から計 {purged_rows_total}回 purge" if purge else ""))


def promote_tag(supabase, name: str, description: str | None):
    """AI タグを公式マスタに昇格 (is_ai_generated=false にする)"""
    rs = supabase.table("tags").select("id, name, tag_type, is_ai_generated").eq(
        "name", name).eq("is_ai_generated", True).execute()
    if not rs.data:
        print(f"  ! '{name}' に該当する AI タグが見つかりません")
        return
    for r in rs.data:
        update = {"is_ai_generated": False}
        if description:
            update["description"] = description
        supabase.table("tags").update(update).eq("id", r["id"]).execute()
        print(f"  ✓ promoted: [{r['tag_type']}] {name}")


def main(args):
    supabase = get_supabase()

    if args.list:
        list_ai_tags(supabase, args.type, args.min_usage)
        return

    if args.promote:
        promote_tag(supabase, args.promote, args.description)
        return

    # 削除モード: 条件で候補抽出
    candidates: list[dict] = []
    if args.delete_name:
        rs = supabase.table("tags").select(
            "id, name, tag_type, usage_count"
        ).eq("is_ai_generated", True).in_("name", args.delete_name).execute()
        candidates = rs.data or []
    elif args.delete_unused:
        candidates = fetch_ai_tags(supabase, args.type, min_usage=0)
        candidates = [r for r in candidates if (r.get("usage_count") or 0) == 0]
    elif args.delete_below is not None:
        candidates = fetch_ai_tags(supabase, args.type, min_usage=0)
        candidates = [r for r in candidates if (r.get("usage_count") or 0) < args.delete_below]
    elif args.delete_all:
        candidates = fetch_ai_tags(supabase, args.type, min_usage=0)
    else:
        print("削除モードが指定されていません。--list / --delete-all / --delete-unused 等を使ってください。")
        return

    delete_tags(supabase, candidates, args.purge, args.dry_run)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI 生成タグの一括管理")
    # 表示
    parser.add_argument("--list", action="store_true", help="AI タグ一覧表示")
    parser.add_argument("--min-usage", type=int, default=0,
                        help="表示対象を usage_count >= N に絞る (デフォルト 0)")
    # 削除条件
    parser.add_argument("--delete-all", action="store_true", help="AI タグ全件削除")
    parser.add_argument("--delete-unused", action="store_true",
                        help="usage_count=0 の AI タグだけ削除")
    parser.add_argument("--delete-below", type=int, default=None,
                        help="usage_count < N の AI タグを削除")
    parser.add_argument("--delete-name", action="append", default=None,
                        help="特定タグ名を削除 (複数指定可)")
    # 共通
    parser.add_argument("--type", choices=["atmosphere", "genre", "sub_genre"],
                        default=None, help="タグ種別で絞る")
    parser.add_argument("--purge", action="store_true",
                        help="削除時に series 行の配列カラムからも該当タグ名を除去")
    parser.add_argument("--dry-run", action="store_true",
                        help="削除候補表示のみ (確認・実削除なし)")
    # 昇格
    parser.add_argument("--promote", default=None,
                        help="指定タグ名を公式マスタに昇格 (is_ai_generated=false)")
    parser.add_argument("--description", default=None,
                        help="--promote と併用: 昇格時に description を設定")
    args = parser.parse_args()
    main(args)
