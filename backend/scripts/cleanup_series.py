"""
series テーブルのクリーンアップスクリプト

series.title 自体に巻数や副題が混ざっている場合があるため、
Gemini 2.5 Flash でタイトルを再パースし、正しいシリーズへ統合する。

例:
  series.title = "追放魔術師のその後４ 新天地で始めるスローライフ"
   ↓ Gemini で再パース
  正しいシリーズ名 = "追放魔術師のその後"
   ↓
  正しいシリーズが存在すればそこへ volumes を移動、なければリネーム。
  空になったシリーズは削除。

使い方:
  python -m scripts.cleanup_series              # 全シリーズを再確認
  python -m scripts.cleanup_series --dry-run    # 変更内容のみ表示
  python -m scripts.cleanup_series --batch 30
"""
import sys
import os
import time
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.gemini import parse_volume_titles
from app.services.supabase_client import get_supabase

GEMINI_INTERVAL_SEC = 1  # Tier 2 想定（60 RPM）


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch_all_series(supabase):
    return supabase.table("series").select("id, title, author, label, cover_image_url, "
                                            "max_tweet_likes, is_trending").execute().data or []


def find_series_id_by_title(supabase, title: str) -> str | None:
    r = supabase.table("series").select("id").eq("title", title).execute()
    return r.data[0]["id"] if r.data else None


def create_series(supabase, title: str, src: dict) -> str:
    ins = supabase.table("series").insert({
        "title": title,
        "author": src.get("author"),
        "label": src.get("label"),
        "cover_image_url": src.get("cover_image_url"),
        "max_tweet_likes": src.get("max_tweet_likes") or 0,
        "is_trending": src.get("is_trending") or False,
    }).execute()
    return ins.data[0]["id"]


def merge_volumes(supabase, src_series_id: str, dst_series_id: str) -> tuple[int, int]:
    """src の volumes を dst に移す。Returns (moved, skipped_due_to_conflict)"""
    vols = supabase.table("volumes").select("id, volume_title").eq("series_id", src_series_id).execute().data or []
    moved = 0
    skipped = 0
    for v in vols:
        # dst 側に同じ volume_title が既にあれば UNIQUE 制約違反になるのでスキップして src 側を消す
        dup = supabase.table("volumes").select("id").eq("series_id", dst_series_id) \
            .eq("volume_title", v["volume_title"]).execute()
        if dup.data:
            # 重複 → 元の volume を削除（紐付くツイートは set null される）
            supabase.table("volumes").delete().eq("id", v["id"]).execute()
            skipped += 1
            continue
        try:
            supabase.table("volumes").update({"series_id": dst_series_id}).eq("id", v["id"]).execute()
            moved += 1
        except Exception as e:
            print(f"    ! volume移動失敗 {v['id']}: {e}")
    return moved, skipped


def delete_orphan_series(supabase, series_id: str) -> bool:
    remaining = supabase.table("volumes").select("id").eq("series_id", series_id).execute().data or []
    if remaining:
        return False
    supabase.table("series").delete().eq("id", series_id).execute()
    return True


def main(dry_run: bool, batch: int, limit: int | None):
    supabase = get_supabase()
    all_series = fetch_all_series(supabase)
    if limit:
        all_series = all_series[:limit]
    print(f"[対象] {len(all_series)}シリーズを再パース"
          f"{' / DRY RUN' if dry_run else ''}\n")

    fixed = 0
    merged = 0
    deleted = 0
    unchanged = 0

    for batch_idx, batch_rows in enumerate(chunked(all_series, batch)):
        if batch_idx > 0:
            time.sleep(GEMINI_INTERVAL_SEC)
        titles = [s["title"] for s in batch_rows]
        print(f"[batch {batch_idx+1}] {len(titles)}件 Gemini に投げ中...")
        parsed = parse_volume_titles(titles)

        for s in batch_rows:
            old_title = s["title"]
            new_title, _ = parsed.get(old_title, (old_title, None))
            if not new_title or new_title == old_title:
                unchanged += 1
                continue

            print(f"\n  「{old_title}」 → 「{new_title}」")

            if dry_run:
                fixed += 1
                continue

            # 正しいシリーズの id を取得（なければ作成）
            dst_id = find_series_id_by_title(supabase, new_title)
            created = False
            if dst_id is None:
                dst_id = create_series(supabase, new_title, s)
                created = True
                print(f"    + 新規作成: 「{new_title}」")

            if dst_id == s["id"]:
                # 自分自身に統合 = リネームだけ
                supabase.table("series").update({"title": new_title}).eq("id", s["id"]).execute()
                fixed += 1
                continue

            # volumes を新シリーズに移動
            moved, skipped = merge_volumes(supabase, s["id"], dst_id)
            print(f"    → volumes 移動 {moved}件 / 重複削除 {skipped}件")

            # 元シリーズを削除
            if delete_orphan_series(supabase, s["id"]):
                deleted += 1
                print(f"    × 旧シリーズ削除: 「{old_title}」")
            else:
                print(f"    ! 旧シリーズに volumes が残っているため削除できず")

            merged += 1
            if not created:
                # 既存の正しいシリーズに統合できた
                pass

    print(f"\n[完了] 変更 {fixed + merged} / 統合 {merged} / 旧シリーズ削除 {deleted} / 変更なし {unchanged}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="series.titleに混入した巻数・副題をクリーンアップ")
    parser.add_argument("--dry-run", action="store_true", help="DBは更新せず差分のみ表示")
    parser.add_argument("--batch", type=int, default=30, help="1回のGemini呼び出し件数")
    parser.add_argument("--limit", type=int, default=None, help="処理件数上限")
    args = parser.parse_args()
    main(dry_run=args.dry_run, batch=args.batch, limit=args.limit)
