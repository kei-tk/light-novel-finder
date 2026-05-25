"""
DBに保存済みのシリーズにあらすじ・タグを付与する

シリーズ単位で1回だけエンリッチする（巻ごとには呼ばない）。

使い方:
  python -m scripts.enrich_books              # 未エンリッチのシリーズのみ
  python -m scripts.enrich_books --all        # 全シリーズを再エンリッチ（上書き）
  python -m scripts.enrich_books --limit 10   # 件数制限
"""
import sys
import os
import time
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.gemini import enrich_series
from app.services.supabase_client import get_supabase

GEMINI_INTERVAL_SEC = 1  # Tier 2 想定（60 RPM）


def fetch_targets(supabase, redo_all: bool, limit: int | None):
    q = supabase.table("series").select("id, title, author, synopsis, atmosphere_tags")
    if not redo_all:
        q = q.or_("synopsis.is.null,atmosphere_tags.is.null")
    if limit:
        q = q.limit(limit)
    return q.execute().data


def fetch_tag_dict(supabase) -> dict[str, list[dict]]:
    """tags マスタテーブルから優先タグ辞書を取得する。"""
    rows = supabase.table("tags").select("name, description, tag_type").execute().data or []
    out: dict[str, list[dict]] = {"atmosphere": [], "genre": [], "sub_genre": []}
    for r in rows:
        t = r.get("tag_type")
        if t in out:
            out[t].append({"name": r.get("name"), "description": r.get("description") or ""})
    return out


def enrich_one(supabase, series_row: dict, tag_dict: dict[str, list[dict]]) -> bool:
    title = series_row["title"]
    author = series_row.get("author")
    enriched = enrich_series(title, author, tag_dict=tag_dict)
    if not enriched:
        return False

    update = {}
    for key in ("synopsis", "atmosphere_tags", "genre_tags", "sub_genre_tags",
                "target_audience"):
        if enriched.get(key):
            update[key] = enriched[key]
    if not author and enriched.get("author"):
        update["author"] = enriched["author"]

    if update:
        supabase.table("series").update(update).eq("id", series_row["id"]).execute()
    return True


def main(redo_all: bool = False, limit: int | None = None):
    supabase = get_supabase()
    tag_dict = fetch_tag_dict(supabase)
    total_tags = sum(len(v) for v in tag_dict.values())
    print(f"[タグ辞書] {total_tags}件 "
          f"(atmosphere {len(tag_dict['atmosphere'])} / genre {len(tag_dict['genre'])} / "
          f"sub_genre {len(tag_dict['sub_genre'])})")

    targets = fetch_targets(supabase, redo_all, limit)
    print(f"[対象] {len(targets)}シリーズをエンリッチ（{'全シリーズ上書き' if redo_all else '未エンリッチのみ'}）\n")

    success = 0
    failed = 0

    for i, row in enumerate(targets):
        print(f"[{i+1}/{len(targets)}] 「{row['title']}」")
        if i > 0:
            time.sleep(GEMINI_INTERVAL_SEC)
        try:
            if enrich_one(supabase, row, tag_dict):
                success += 1
                print(f"  → 完了")
            else:
                failed += 1
                print(f"  → スキップ（Gemini応答なし）")
        except Exception as e:
            failed += 1
            print(f"  → エラー: {e}")

    print(f"\n[完了] 成功 {success}件 / 失敗 {failed}件")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="既存シリーズのエンリッチ")
    parser.add_argument("--all", action="store_true", help="全シリーズを再エンリッチ")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    main(redo_all=args.all, limit=args.limit)
