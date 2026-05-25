"""
DBから特典付き・非ライトノベルのシリーズ/巻を削除するスクリプト

対象:
- volume_title または series.title が _EXCLUDE_TITLE_PATTERNS にマッチするもの
  例: 特装版・限定版・ドラマCD付き・画集・ファンブック・コミカライズ等

挙動:
- volumes をマッチで削除（series_id は CASCADE しないので残る）
- volumes が0件になった series も削除
- series.title 自体が除外パターンにマッチする場合は series ごと削除（CASCADE で volumes も消える）

使い方:
  python -m scripts.cleanup_special_editions --dry-run
  python -m scripts.cleanup_special_editions
"""
import sys
import os
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.gemini import _is_excluded_title
from app.services.supabase_client import get_supabase


def main(dry_run: bool):
    supabase = get_supabase()

    # 1. 除外パターンに一致する series（タイトル自体がアウト）
    all_series = supabase.table("series").select("id, title, label").execute().data or []
    excluded_series = [s for s in all_series if _is_excluded_title(s["title"])]

    print(f"[除外シリーズ] {len(excluded_series)}件（series ごと削除）")
    for s in excluded_series[:30]:
        print(f"  - 「{s['title']}」 [{s.get('label')}]")
    if len(excluded_series) > 30:
        print(f"  ... 他 {len(excluded_series) - 30}件")

    # 2. 除外パターンに一致する volumes（特典付きが主）
    all_volumes = supabase.table("volumes").select("id, series_id, volume_title").execute().data or []
    excluded_volumes = [v for v in all_volumes if _is_excluded_title(v["volume_title"])]

    print(f"\n[除外巻] {len(excluded_volumes)}件（volumes のみ削除）")
    for v in excluded_volumes[:30]:
        print(f"  - 「{v['volume_title']}」")
    if len(excluded_volumes) > 30:
        print(f"  ... 他 {len(excluded_volumes) - 30}件")

    if dry_run:
        print("\n[DRY RUN] 削除はスキップ")
        return

    # 削除実行
    print("\n[削除実行中...]")
    if excluded_series:
        sids = [s["id"] for s in excluded_series]
        supabase.table("series").delete().in_("id", sids).execute()
        print(f"  → series {len(sids)}件 削除")

    if excluded_volumes:
        vids = [v["id"] for v in excluded_volumes]
        supabase.table("volumes").delete().in_("id", vids).execute()
        print(f"  → volumes {len(vids)}件 削除")

    # 3. 巻が0件になった orphan series を掃除
    print("\n[orphan series チェック]")
    remaining_series = supabase.table("series").select("id, title").execute().data or []
    remaining_vols = supabase.table("volumes").select("series_id").execute().data or []
    sids_with_vols = {v["series_id"] for v in remaining_vols}
    orphans = [s for s in remaining_series if s["id"] not in sids_with_vols]
    if orphans:
        oids = [s["id"] for s in orphans]
        supabase.table("series").delete().in_("id", oids).execute()
        print(f"  → 巻ゼロのシリーズ {len(oids)}件 削除")
    else:
        print("  → なし")

    print("\n[完了]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="特典付き・非ライトノベルの削除")
    parser.add_argument("--dry-run", action="store_true", help="削除対象を表示するだけ")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
