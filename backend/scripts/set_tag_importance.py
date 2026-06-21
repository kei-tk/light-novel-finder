"""
tags.importance を一括 / 個別に変更する CLI。

importance の意味:
  1 = 低 (デフォルト。NULL も 1 として扱う)
  2 = 中
  3 = 高 (Gemini が「適切か」を最も慎重に判定する)

使い方:
  # 一覧表示 (確認用)
  python -m scripts.set_tag_importance --list
  python -m scripts.set_tag_importance --list --tag-type sub_genre --subcategory character
  python -m scripts.set_tag_importance --list --name 共依存 --name 百合

  # 個別指定 (NAME=IMP の形式。1 タグ = 1 オプション、複数回 OK)
  python -m scripts.set_tag_importance --set 共依存=3 --set 百合=3 --set BL=3 --apply

  # 条件で一括 (subcategory / tag_type / name 部分一致 で絞り込み)
  python -m scripts.set_tag_importance --bulk --tag-type sub_genre \
      --subcategory character --importance 2 --apply

  # TSV ファイルから一括 (1 行 = "<name>\t<importance>"。# 行はコメント)
  python -m scripts.set_tag_importance --from-file imp.tsv --apply

デフォルトは dry-run。--apply を付けたとき初めて DB を更新する。
"""
import sys
import os
import argparse
from collections import Counter
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase


VALID_IMPORTANCE = (1, 2, 3)


def _paginate(query_factory, page_size: int = 1000) -> list[dict]:
    rows = []
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


def fetch_tags(supabase, tag_type: str | None,
               subcategory: str | None,
               names: list[str] | None,
               name_contains: str | None) -> list[dict]:
    """フィルタを掛けて tags を取得。"""
    def factory():
        q = supabase.table("tags").select(
            "id, name, tag_type, subcategory, importance, usage_count, is_ai_generated"
        )
        if tag_type:
            q = q.eq("tag_type", tag_type)
        if subcategory:
            q = q.eq("subcategory", subcategory)
        if names:
            q = q.in_("name", names)
        if name_contains:
            q = q.ilike("name", f"%{name_contains}%")
        return q
    return _paginate(factory)


def parse_set_args(items: list[str]) -> dict[str, int]:
    """['共依存=3', '百合=3'] → {'共依存': 3, '百合': 3}"""
    out: dict[str, int] = {}
    for s in items:
        if "=" not in s:
            raise SystemExit(f"--set の形式は NAME=IMPORTANCE: {s!r}")
        name, _, val = s.partition("=")
        name = name.strip()
        try:
            imp = int(val)
        except ValueError:
            raise SystemExit(f"--set の importance は整数で: {s!r}")
        if imp not in VALID_IMPORTANCE:
            raise SystemExit(f"importance は 1/2/3: {s!r}")
        if not name:
            raise SystemExit(f"--set の name が空: {s!r}")
        out[name] = imp
    return out


def parse_tsv(path: str) -> dict[str, int]:
    """TSV: "<name>\\t<importance>"  (# 行はコメント)"""
    out: dict[str, int] = {}
    with open(path, encoding="utf-8") as f:
        for i, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                # スペース区切りも許容
                parts = line.split()
            if len(parts) < 2:
                print(f"  ! {path}:{i} 形式不正 (skip): {line!r}")
                continue
            name = parts[0].strip()
            try:
                imp = int(parts[1])
            except ValueError:
                print(f"  ! {path}:{i} importance が整数でない (skip)")
                continue
            if imp not in VALID_IMPORTANCE:
                print(f"  ! {path}:{i} importance は 1/2/3 (skip)")
                continue
            out[name] = imp
    return out


def list_mode(supabase, tag_type, subcategory, names, name_contains):
    rows = fetch_tags(supabase, tag_type, subcategory, names, name_contains)
    if not rows:
        print("該当タグなし")
        return
    # importance を NULL=1 扱いに正規化して集計
    by_imp = Counter((r.get("importance") or 1) for r in rows)
    by_kind = Counter(("AI" if r.get("is_ai_generated") else "公式") for r in rows)
    print(f"[一覧] {len(rows)} 件  importance 内訳={dict(by_imp)}  kind={dict(by_kind)}\n")
    # 重要度降順 → usage 降順
    rows.sort(key=lambda r: (-(r.get("importance") or 1), -(r.get("usage_count") or 0)))
    for r in rows:
        kind = "AI" if r.get("is_ai_generated") else "公式"
        imp = r.get("importance")
        imp_str = f"[★{imp}]" if imp else "[★-]"
        sub = r.get("subcategory") or "-"
        print(f"  {imp_str} usage={r.get('usage_count') or 0:>4}  "
              f"{r.get('tag_type'):<10} {sub:<12} {kind}  {r['name']}")


def apply_updates(supabase, rows: list[dict], target_importance: dict[str, int],
                  apply: bool):
    """rows のうち、target_importance に含まれる name について importance を更新。"""
    name_to_row = {r["name"]: r for r in rows}
    # 変更がある分だけ抽出
    plan: list[tuple[dict, int, int]] = []  # (row, current, new)
    missing: list[str] = []
    for name, imp in target_importance.items():
        r = name_to_row.get(name)
        if not r:
            missing.append(name)
            continue
        cur = r.get("importance")
        cur_norm = cur if cur in VALID_IMPORTANCE else None
        if cur_norm == imp:
            continue
        plan.append((r, cur_norm, imp))

    print(f"[更新計画] {len(plan)} 件 (DB と異なる importance のみ対象)")
    if missing:
        print(f"  ! tags に存在しない name: {len(missing)} 件 → {missing[:10]}")
    if not plan:
        print("  → 変更なし")
        return
    plan.sort(key=lambda x: (-(x[2] or 1), x[0].get("tag_type") or ""))
    for r, cur, new in plan[:60]:
        cur_disp = f"★{cur}" if cur else "★- (NULL)"
        print(f"  {r['name']:<24} ({r.get('tag_type'):<10}/"
              f"{r.get('subcategory') or '-':<12}) {cur_disp} → ★{new}")
    if len(plan) > 60:
        print(f"  ... (他 {len(plan) - 60} 件)")

    if not apply:
        print("\n(--apply 未指定: DB は更新しない)")
        return

    confirm = input(f"\n{len(plan)} 件を更新しますか? [y/N]: ").strip().lower()
    if confirm != "y":
        print("中止")
        return
    updated = 0
    for r, _cur, new in plan:
        try:
            supabase.table("tags").update({"importance": new}).eq("id", r["id"]).execute()
            updated += 1
        except Exception as e:
            print(f"  ! 更新失敗 {r['name']!r}: {e}")
    print(f"[完了] {updated}/{len(plan)} 件更新")


def bulk_mode(supabase, tag_type, subcategory, names, name_contains,
              importance: int, apply: bool):
    """フィルタに合致するタグを全て同じ importance に更新。"""
    if importance not in VALID_IMPORTANCE:
        raise SystemExit("--importance は 1/2/3")
    rows = fetch_tags(supabase, tag_type, subcategory, names, name_contains)
    if not rows:
        print("該当タグなし")
        return
    targets = {r["name"]: importance for r in rows}
    print(f"[一括対象] {len(targets)} 件 (フィルタ: tag_type={tag_type} "
          f"subcategory={subcategory} name_contains={name_contains})")
    apply_updates(supabase, rows, targets, apply)


def set_mode(supabase, tag_type, subcategory, name_contains,
             pairs: dict[str, int], apply: bool):
    if not pairs:
        raise SystemExit("--set または --from-file が必要")
    rows = fetch_tags(supabase, tag_type, subcategory,
                      list(pairs.keys()), name_contains)
    apply_updates(supabase, rows, pairs, apply)


def main():
    parser = argparse.ArgumentParser(description="tags.importance を更新")
    # モード
    parser.add_argument("--list", action="store_true",
                        help="一覧表示のみ (フィルタ併用可)")
    parser.add_argument("--bulk", action="store_true",
                        help="フィルタに合致する全タグを --importance に更新")
    parser.add_argument("--set", action="append", default=[],
                        metavar="NAME=IMP",
                        help="個別指定 (複数回 OK): 共依存=3")
    parser.add_argument("--from-file", default=None,
                        help="TSV ファイル: 1 行 \"name\\timportance\"")
    # フィルタ
    parser.add_argument("--tag-type", choices=["atmosphere", "genre", "sub_genre"],
                        default=None)
    parser.add_argument("--subcategory",
                        choices=["relationship", "character", "profession",
                                 "setting", "plot", "format"],
                        default=None)
    parser.add_argument("--name", action="append", default=[],
                        help="--list で表示する名前を絞り込み (複数回 OK)")
    parser.add_argument("--name-contains", default=None,
                        help="name に含まれる文字列で絞り込み")
    # --bulk 用
    parser.add_argument("--importance", type=int, choices=VALID_IMPORTANCE,
                        default=None, help="--bulk で全件に設定する値")
    # 適用
    parser.add_argument("--apply", action="store_true",
                        help="実際に DB を更新 (未指定なら dry-run)")
    args = parser.parse_args()

    supabase = get_supabase()
    names_filter = args.name or None

    if args.list:
        list_mode(supabase, args.tag_type, args.subcategory,
                  names_filter, args.name_contains)
        return

    if args.bulk:
        if args.importance is None:
            raise SystemExit("--bulk には --importance N が必要")
        bulk_mode(supabase, args.tag_type, args.subcategory,
                  names_filter, args.name_contains,
                  args.importance, args.apply)
        return

    # 個別指定モード
    pairs = parse_set_args(args.set)
    if args.from_file:
        pairs.update(parse_tsv(args.from_file))
    if pairs:
        set_mode(supabase, args.tag_type, args.subcategory,
                 args.name_contains, pairs, args.apply)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
