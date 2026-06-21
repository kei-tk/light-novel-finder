"""
手動で確定したタグマージマップを実行する一発スクリプト。

実行内容:
  1. MERGES に基づき openbd_test_series.atmosphere_tags / genre_tags /
     sub_genre_tags の配列要素を置換 (重複は除去)
  2. tags テーブルから「マージ元」の行を削除
  3. 連鎖 (A→B, B→C) は自動解決して A→C にする

確認・適用:
  python -m scripts.merge_manual_tags --dry-run    # 計画のみ
  python -m scripts.merge_manual_tags --apply      # 実行 (確認プロンプトあり)
"""
import sys
import os
import argparse
from collections import defaultdict
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase


# 同じ tag_type 内でのみ統合する。tag_type が違うと意味的にも違うので除外する。
# 形式: {from_name: to_name}
MERGES: dict[str, str] = {
    # ===== A. 完全な表記揺れ =====
    "師弟関係": "師弟",
    "パーティー冒険": "パーティ冒険",
    "平行世界": "パラレルワールド",
    "童話風": "童話モチーフ",

    # ===== B. 公式タグとの被り (ギャグは除外、隠れ実力者は残す) =====
    "心温まる": "ほのぼの",          # atmosphere
    "ミステリ": "ミステリー",         # genre
    # 「隠れ実力者」は当初「実力隠し」へ統合予定だったが、tags に「実力隠し」が
    # 存在しないため独立タグとして残す (2026-06-08 ユーザー判断)

    # ===== C. AI 同士の強い被り (異世界に寄せ) =====
    "異世界ファンタジー": "異世界",
    "オムニバス": "短編集",
    "婚約": "婚約者",
    "王侯貴族": "貴族",
    "強くてニューゲーム": "2周目",   # 後段で 2周目 → タイムスリップ と連鎖
    "不死身": "不老不死",

    # ===== D 時間ループ系 (タイムスリップ以外を統合) =====
    "やり直し":  "タイムスリップ",
    "タイムリープ": "タイムスリップ",
    "2周目":    "タイムスリップ",
    "若返り":    "タイムスリップ",

    # ===== D ファンタジー派生 (現代ファンタジー・ダークファンタジー以外を統合) =====
    # 統合先は usage 最大の「現代ファンタジー」
    "和風ファンタジー":     "現代ファンタジー",
    "歴史ファンタジー":     "現代ファンタジー",
    "王道ファンタジー":     "現代ファンタジー",
    "ハイファンタジー":     "現代ファンタジー",
    "ヒロイックファンタジー": "現代ファンタジー",
    "海洋ファンタジー":     "現代ファンタジー",
    "中華ファンタジー":     "現代ファンタジー",
    "東洋ファンタジー":     "現代ファンタジー",
    "アジアンファンタジー":  "現代ファンタジー",
    "アラビアンファンタジー": "現代ファンタジー",
    "ビブリオファンタジー":  "現代ファンタジー",
}


_COL_BY_TYPE = {
    "atmosphere": "atmosphere_tags",
    "genre":      "genre_tags",
    "sub_genre":  "sub_genre_tags",
}


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


def resolve_chain(merges: dict[str, str]) -> dict[str, str]:
    """A→B, B→C を A→C に解決。循環があれば検出して元のまま返す。"""
    out: dict[str, str] = {}
    for src in merges:
        visited = {src}
        cur = merges[src]
        while cur in merges and cur not in visited:
            visited.add(cur)
            cur = merges[cur]
        out[src] = cur
    return out


def main(apply: bool):
    sb = get_supabase()
    final = resolve_chain(MERGES)
    all_names = sorted(set(final.keys()) | set(final.values()))

    # tags マスタを取得 (tag_type / id を確定するため)
    rs = sb.table("tags").select(
        "id, name, tag_type, usage_count, is_ai_generated"
    ).in_("name", all_names).execute()
    name_to_tag: dict[str, dict] = {}
    # 同じ name が複数 tag_type にまたがる可能性は低いが、今回は MERGES が tag_type 一意の想定
    for r in rs.data or []:
        # name 重複時は usage_count 大きい方を残す
        prev = name_to_tag.get(r["name"])
        if not prev or (r.get("usage_count") or 0) > (prev.get("usage_count") or 0):
            name_to_tag[r["name"]] = r

    # ---- Plan ----
    print("\n[計画] tags 同一 tag_type 内のマージ\n")
    by_type: dict[str, dict[str, str]] = defaultdict(dict)  # tag_type -> {src: dst}
    skipped: list[tuple[str, str, str]] = []
    for src, dst in final.items():
        st = name_to_tag.get(src)
        dt = name_to_tag.get(dst)
        if not st:
            skipped.append((src, dst, "src が tags に無い"))
            continue
        if not dt:
            skipped.append((src, dst, f"dst {dst!r} が tags に無い"))
            continue
        if st["tag_type"] != dt["tag_type"]:
            skipped.append((src, dst,
                            f"tag_type 不一致 src={st['tag_type']} dst={dt['tag_type']}"))
            continue
        by_type[st["tag_type"]][src] = dst

    for t in ("atmosphere", "genre", "sub_genre"):
        mapping = by_type.get(t) or {}
        if not mapping:
            continue
        print(f"  ■ {t} ({len(mapping)} 件)")
        for src, dst in sorted(mapping.items(), key=lambda x: -(name_to_tag[x[0]].get("usage_count") or 0)):
            su = name_to_tag[src].get("usage_count") or 0
            du = name_to_tag[dst].get("usage_count") or 0
            kind = "AI" if name_to_tag[src].get("is_ai_generated") else "公式"
            print(f"    {src:<22} (usage={su:>4}, {kind}) → {dst} (usage={du})")

    if skipped:
        print(f"\n  ■ スキップ ({len(skipped)} 件)")
        for src, dst, why in skipped:
            print(f"    {src} → {dst}: {why}")

    if not apply:
        print("\n(--apply 未指定: DB は更新しません)")
        return

    confirm = input("\n上記の計画を実行しますか? [y/N]: ").strip().lower()
    if confirm != "y":
        print("中止")
        return

    # ---- 1. openbd_test_series の配列を置換 ----
    print("\n[適用 A] openbd_test_series の配列置換")
    for tag_type, mapping in by_type.items():
        col = _COL_BY_TYPE[tag_type]
        rows = _paginate(
            lambda c=col: sb.table("openbd_test_series").select(f"id, {c}")
        )
        updated = 0
        for r in rows:
            arr = r.get(col) or []
            if not arr:
                continue
            new_arr: list[str] = []
            seen: set[str] = set()
            changed = False
            for n in arr:
                mapped = mapping.get(n, n)
                if mapped != n:
                    changed = True
                if mapped in seen:
                    changed = True
                    continue
                seen.add(mapped)
                new_arr.append(mapped)
            if changed:
                try:
                    sb.table("openbd_test_series").update({col: new_arr}).eq("id", r["id"]).execute()
                    updated += 1
                except Exception as e:
                    print(f"  ! id={r['id'][:8]} 更新失敗: {e}")
        print(f"  {tag_type}: {updated} シリーズを更新")

    # ---- 2. tags 行削除 (マージ元) ----
    print("\n[適用 B] tags 行から「マージ元」を削除")
    src_ids = []
    for src in {s for mapping in by_type.values() for s in mapping}:
        t = name_to_tag.get(src)
        if t:
            src_ids.append(t["id"])
    if src_ids:
        try:
            sb.table("tags").delete().in_("id", src_ids).execute()
            print(f"  → {len(src_ids)} 行削除")
        except Exception as e:
            print(f"  ! tags 削除失敗: {e}")

    print("\n[完了]")
    print("    ※ tags.usage_count はこの操作で更新していません。")
    print("       完了後に `python -m scripts.recount_tag_usage --apply` で再集計を推奨します。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="手動マージ実行")
    parser.add_argument("--dry-run", action="store_true",
                        help="(デフォルト) 計画のみ表示")
    parser.add_argument("--apply", action="store_true",
                        help="実際に DB を更新")
    args = parser.parse_args()
    if not (args.dry_run or args.apply):
        args.dry_run = True
    main(apply=args.apply)
