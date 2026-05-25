"""
GA文庫/GAノベルとして収集したデータを削除するスクリプト

series.label が "GA文庫" または "GAノベル" のシリーズを削除する。
volumes は series の外部キー ON DELETE CASCADE により自動削除される。
tweets は volume_id が ON DELETE SET NULL なのでツイート自体は残る。

使い方:
  python -m scripts.delete_ga_data --dry-run    # 削除対象を表示するだけ
  python -m scripts.delete_ga_data              # 実際に削除
  python -m scripts.delete_ga_data --label GA文庫   # ラベル指定
"""
import sys
import os
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase

DEFAULT_LABELS = ["GA文庫", "GAノベル", "GAコミック", "GAコミックス", "GAコミカライズ"]


def main(labels: list[str], dry_run: bool):
    supabase = get_supabase()

    series_rows = supabase.table("series").select("id, title, label") \
        .in_("label", labels).execute().data or []

    print(f"[対象ラベル] {labels}")
    print(f"[対象シリーズ] {len(series_rows)}件\n")

    if not series_rows:
        print("[終了] 削除対象なし")
        return

    series_ids = [s["id"] for s in series_rows]

    vols = supabase.table("volumes").select("id", count="exact") \
        .in_("series_id", series_ids).execute()
    volume_count = vols.count if vols.count is not None else len(vols.data or [])

    print(f"[連動削除される巻] {volume_count}件\n")

    for s in series_rows[:30]:
        print(f"  - 「{s['title']}」 [{s.get('label')}]")
    if len(series_rows) > 30:
        print(f"  ... 他 {len(series_rows) - 30}件")

    if dry_run:
        print("\n[DRY RUN] 削除はスキップ")
        return

    print("\n[削除実行中...]")
    # series を削除すれば volumes は CASCADE で消える
    supabase.table("series").delete().in_("id", series_ids).execute()

    print(f"[完了] series {len(series_ids)}件 / volumes {volume_count}件 削除")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GA文庫/GAノベル データ削除")
    parser.add_argument("--dry-run", action="store_true", help="削除対象を表示するだけ")
    parser.add_argument("--label", action="append", default=None,
                        help="対象ラベル（複数指定可）。未指定なら GA文庫 と GAノベル")
    args = parser.parse_args()
    main(labels=args.label or DEFAULT_LABELS, dry_run=args.dry_run)
