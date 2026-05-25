"""
volumes.published_date を Gemini 3.1 Pro（Web検索）で調べ直すスクリプト

使い方:
  python -m scripts.recheck_dates                # NULL のものだけ
  python -m scripts.recheck_dates --all          # 全 volume を再確認（既存値も上書き）
  python -m scripts.recheck_dates --limit 20     # 件数制限
  python -m scripts.recheck_dates --series "○○" # 特定シリーズのみ
"""
import sys
import os
import time
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.gemini import lookup_published_date
from app.services.supabase_client import get_supabase

GEMINI_INTERVAL_SEC = 1  # Tier 2 想定（60 RPM）


def fetch_targets(supabase, redo_all: bool, limit: int | None, series_filter: str | None):
    q = supabase.table("volumes").select(
        "id, volume_title, volume_number, published_date, "
        "series:series_id(id, title)"
    )
    if not redo_all:
        q = q.is_("published_date", "null")
    if limit:
        q = q.limit(limit)
    rows = q.execute().data or []
    if series_filter:
        rows = [r for r in rows if (r.get("series") or {}).get("title") == series_filter]
    return rows


def main(redo_all: bool, limit: int | None, series_filter: str | None):
    supabase = get_supabase()
    targets = fetch_targets(supabase, redo_all, limit, series_filter)
    print(f"[対象] {len(targets)}件 を発売日再確認")
    print(f"  モード: {'全件上書き' if redo_all else '発売日NULLのみ'}"
          f"{f' / シリーズ絞り込み: 「{series_filter}」' if series_filter else ''}\n")

    updated = 0
    unchanged = 0
    not_found = 0

    for i, vol in enumerate(targets):
        ser = vol.get("series") or {}
        series_title = ser.get("title", "")
        volume_title = vol.get("volume_title", "")
        volume_number = vol.get("volume_number")
        old_date = vol.get("published_date")

        print(f"[{i+1}/{len(targets)}] 「{volume_title}」（旧: {old_date or 'NULL'}）")
        if i > 0:
            time.sleep(GEMINI_INTERVAL_SEC)

        new_date = lookup_published_date(series_title, volume_title, volume_number)
        if not new_date:
            not_found += 1
            print(f"  → 不明（変更なし）")
            continue

        if new_date == old_date:
            unchanged += 1
            print(f"  → 同じ（{new_date}）")
            continue

        supabase.table("volumes").update({"published_date": new_date}) \
            .eq("id", vol["id"]).execute()
        updated += 1
        print(f"  → 更新: {old_date or 'NULL'} → {new_date}")

    print(f"\n[完了] 更新 {updated} / 変更なし {unchanged} / 不明 {not_found}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="volumes の発売日を Gemini 3.1 Pro で調べ直す")
    parser.add_argument("--all", action="store_true", help="全 volume を再確認（既存値も上書き）")
    parser.add_argument("--limit", type=int, default=None, help="処理件数上限")
    parser.add_argument("--series", type=str, default=None, help="特定シリーズに絞る")
    args = parser.parse_args()
    main(redo_all=args.all, limit=args.limit, series_filter=args.series)
