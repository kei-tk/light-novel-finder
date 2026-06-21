"""
retag_genre.py のターミナル出力ログから genre_tags を DB に復元するスクリプト。

ログ内の `✓` で始まる行を解析:
  ✓ シリーズタイトル(40文字)  ['old', ...] → ['new', ...]
                              ↑ before          ↑ after = 復元したい値

タイトルは 40 文字に切り詰められているので、DB から「タイトル前方一致 (= ログ行のタイトル)」で
シリーズを探す。1 件だけ一致したものを安全に UPDATE する。複数一致 / 0 一致は要レビューとして
報告する。

使い方:
  python -m scripts.restore_genre_from_log path/to/log.txt --dry-run
  python -m scripts.restore_genre_from_log path/to/log.txt --apply
"""
import sys
import os
import re
import ast
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase


# ログ行のパース。
#   "  ✓ XX...  ['a', 'b']  →  ['c', 'd']"
# 全角矢印 → を区切りに使う。タイトル末尾と before の間は 2スペース以上。
_LINE_RE = re.compile(
    r"^\s*✓\s+(?P<title>.+?)\s\s+(?P<before>\[.*?\])\s*→\s*(?P<after>\[.*?\])\s*$"
)


def parse_log(path: str) -> list[dict]:
    """ログから {title, before, after} を抜き出す"""
    entries = []
    bad = 0
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if "✓" not in line:
                continue
            m = _LINE_RE.match(line)
            if not m:
                bad += 1
                continue
            title = m.group("title").strip()
            try:
                before = ast.literal_eval(m.group("before"))
                after = ast.literal_eval(m.group("after"))
            except (ValueError, SyntaxError):
                bad += 1
                continue
            if not isinstance(before, list) or not isinstance(after, list):
                bad += 1
                continue
            entries.append({"title": title, "before": before, "after": after})
    print(f"[ログ解析] {len(entries)} 行ヒット (パース失敗 {bad} 件)")
    return entries


def fetch_all_titles(sb) -> list[dict]:
    """全 series の id + title を取得 (前方一致照合用、メモリにキャッシュ)"""
    rows = []
    offset = 0
    while True:
        rs = sb.table("openbd_test_series").select(
            "id, series_title, genre_tags"
        ).is_("parent_series_id", "null").eq("is_special_edition", False).range(
            offset, offset + 999
        ).execute()
        batch = rs.data or []
        rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return rows


def match_series(entries: list[dict], all_series: list[dict]) -> tuple[
    list[dict], list[dict], list[dict]
]:
    """ログの title (40 文字切り詰め) → series 1 件にマッチさせる。
    返り値: (確定マッチ, 複数候補, 見つからず)"""
    # title 前方一致索引: prefix(40) → [series, ...]
    by_prefix: dict[str, list[dict]] = {}
    for s in all_series:
        title = s.get("series_title") or ""
        prefix = title[:40]
        by_prefix.setdefault(prefix, []).append(s)

    matched = []
    ambiguous = []
    missing = []
    for e in entries:
        log_title = e["title"]
        # ログ側のタイトルは 40 文字以下にトリミングされている
        candidates = by_prefix.get(log_title, [])
        if len(candidates) == 1:
            s = candidates[0]
            matched.append({
                "id": s["id"],
                "title": s["series_title"],
                "current": s.get("genre_tags") or [],
                "log_before": e["before"],
                "after": e["after"],
            })
        elif len(candidates) >= 2:
            ambiguous.append({"log_title": log_title, "candidates": candidates,
                              "after": e["after"]})
        else:
            missing.append({"log_title": log_title, "after": e["after"]})
    return matched, ambiguous, missing


def main():
    parser = argparse.ArgumentParser(description="ログから genre_tags を復元")
    parser.add_argument("log_path", help="retag_genre.py のターミナル出力ログ")
    parser.add_argument("--dry-run", action="store_true",
                        help="(デフォルト) 計画のみ。DB は更新しない")
    parser.add_argument("--apply", action="store_true",
                        help="DB を更新する")
    parser.add_argument("--allow-current-mismatch", action="store_true",
                        help="DB の現在値とログの before が一致しなくても適用 (再 run 後等)")
    args = parser.parse_args()
    if not (args.dry_run or args.apply):
        args.dry_run = True

    if not os.path.exists(args.log_path):
        print(f"ログファイルが見つかりません: {args.log_path}")
        return

    entries = parse_log(args.log_path)
    if not entries:
        print("復元対象なし")
        return

    sb = get_supabase()
    print(f"[全シリーズ取得]")
    all_series = fetch_all_titles(sb)
    print(f"  → {len(all_series):,} 件")

    matched, ambiguous, missing = match_series(entries, all_series)
    print(f"\n[照合結果]")
    print(f"  確定マッチ:  {len(matched):,}")
    print(f"  複数候補:    {len(ambiguous):,} (要手動レビュー)")
    print(f"  見つからず:  {len(missing):,}")

    # before チェック (整合性確認用、ズレてたらフラグ)
    skip_mismatch = []
    if not args.allow_current_mismatch:
        for m in list(matched):
            if set(m["current"]) != set(m["log_before"]):
                skip_mismatch.append(m)
        if skip_mismatch:
            ids_skip = {m["id"] for m in skip_mismatch}
            matched = [m for m in matched if m["id"] not in ids_skip]
            print(f"  before ズレでスキップ: {len(skip_mismatch):,} "
                  f"(--allow-current-mismatch で強制適用可)")

    # 既に適用済 (after と一致) は無視
    unchanged = [m for m in matched if set(m["current"]) == set(m["after"])]
    matched = [m for m in matched if set(m["current"]) != set(m["after"])]
    if unchanged:
        print(f"  既に適用済 (after と一致): {len(unchanged):,} 件 → スキップ")

    print(f"\n[更新対象] {len(matched):,} 件")
    # サンプル表示
    for m in matched[:8]:
        print(f"  {m['title'][:40]:<40}  {m['current']} → {m['after']}")
    if len(matched) > 8:
        print(f"  ... (他 {len(matched) - 8} 件)")

    if ambiguous:
        print(f"\n[複数候補] {len(ambiguous):,} 件 (要レビュー)")
        for a in ambiguous[:5]:
            titles = [c["series_title"][:50] for c in a["candidates"]]
            print(f"  log='{a['log_title']}' → {titles}  指示後={a['after']}")
    if missing:
        print(f"\n[見つからず] {len(missing):,} 件")
        for x in missing[:5]:
            print(f"  log='{x['log_title']}'")

    if not args.apply:
        print("\n(--apply 未指定: DB は更新しません)")
        return

    confirm = input(f"\n{len(matched)} 件を UPDATE します。実行しますか? [y/N]: ").strip().lower()
    if confirm != "y":
        print("中止")
        return

    updated = 0
    for m in matched:
        try:
            sb.table("openbd_test_series").update(
                {"genre_tags": m["after"]}
            ).eq("id", m["id"]).execute()
            updated += 1
        except Exception as e:
            print(f"  ! 失敗 {m['title'][:40]}: {e}")
    print(f"\n[完了] {updated}/{len(matched)} 件更新")


if __name__ == "__main__":
    main()
