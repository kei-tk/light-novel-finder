"""
volumes.volume_number を Gemini 2.5 Flash で調べ直すスクリプト

既存の volumes テーブルの volume_title から、シリーズ名と巻数を再推測する。
シリーズ名のズレ（誤って巻数がシリーズ名に残っている等）も検出して修正する。

使い方:
  python -m scripts.recheck_volume_numbers              # 全 volume
  python -m scripts.recheck_volume_numbers --null-only  # volume_number が NULL のものだけ
  python -m scripts.recheck_volume_numbers --limit 50   # 件数制限
  python -m scripts.recheck_volume_numbers --dry-run    # 更新内容だけ表示、DBは触らない
  python -m scripts.recheck_volume_numbers --batch 50   # 1回のGemini呼び出しで処理する件数
"""
import sys
import os
import time
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.gemini import parse_volume_titles
from app.services.supabase_client import get_supabase

GEMINI_INTERVAL_SEC = 1  # Tier 2 想定（60 RPM）


def fetch_targets(supabase, null_only: bool, limit: int | None):
    q = supabase.table("volumes").select(
        "id, series_id, volume_title, volume_number, "
        "series:series_id(id, title)"
    )
    if null_only:
        q = q.is_("volume_number", "null")
    if limit:
        q = q.limit(limit)
    return q.execute().data or []


def find_or_create_series(supabase, series_title: str, src_series_id: str) -> str:
    """
    新しいシリーズ名に対応する series.id を返す。
    存在しなければ、元シリーズの author/label/cover などを引き継いで新規作成する。
    """
    existing = supabase.table("series").select("id").eq("title", series_title).execute()
    if existing.data:
        return existing.data[0]["id"]

    # 元シリーズの情報をコピーして新規作成
    src = supabase.table("series").select(
        "author, label, cover_image_url, max_tweet_likes, is_trending"
    ).eq("id", src_series_id).execute()
    src_row = src.data[0] if src.data else {}

    ins = supabase.table("series").insert({
        "title": series_title,
        "author": src_row.get("author"),
        "label": src_row.get("label"),
        "cover_image_url": src_row.get("cover_image_url"),
        "max_tweet_likes": 0,
        "is_trending": False,
    }).execute()
    return ins.data[0]["id"]


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def main(null_only: bool, limit: int | None, dry_run: bool, batch: int):
    supabase = get_supabase()
    targets = fetch_targets(supabase, null_only, limit)
    print(f"[対象] {len(targets)}件 の volumes を再確認")
    print(f"  モード: {'volume_numberがNULLのみ' if null_only else '全件'}"
          f"{' / DRY RUN（更新なし）' if dry_run else ''}")
    print(f"  バッチサイズ: {batch}件/Gemini呼び出し\n")

    updated_num = 0
    updated_series = 0
    unchanged = 0

    for batch_idx, batch_rows in enumerate(chunked(targets, batch)):
        if batch_idx > 0:
            time.sleep(GEMINI_INTERVAL_SEC)

        titles = [r["volume_title"] for r in batch_rows]
        print(f"[batch {batch_idx+1}] {len(titles)}件 を Gemini で分解中...")
        parsed = parse_volume_titles(titles)

        for vol in batch_rows:
            vt = vol["volume_title"]
            old_num = vol.get("volume_number")
            old_series_id = vol.get("series_id")
            old_series_title = (vol.get("series") or {}).get("title", "")

            new_series_title, new_num = parsed.get(vt, (vt, None))

            num_changed = (new_num != old_num)
            series_changed = (new_series_title and new_series_title != old_series_title)

            if not num_changed and not series_changed:
                unchanged += 1
                continue

            label = []
            if num_changed:
                label.append(f"巻数 {old_num} → {new_num}")
            if series_changed:
                label.append(f"シリーズ「{old_series_title}」→「{new_series_title}」")
            print(f"  「{vt}」: {' / '.join(label)}")

            if dry_run:
                if num_changed:
                    updated_num += 1
                if series_changed:
                    updated_series += 1
                continue

            update = {}
            if num_changed:
                update["volume_number"] = new_num

            if series_changed:
                # シリーズ名が変わる場合: 該当シリーズの id を取得（なければ新規作成）して付け替え
                new_sid = find_or_create_series(supabase, new_series_title, old_series_id)
                if new_sid != old_series_id:
                    update["series_id"] = new_sid

            if update:
                try:
                    supabase.table("volumes").update(update).eq("id", vol["id"]).execute()
                except Exception as e:
                    # UNIQUE(series_id, volume_title) 違反等は無視してログ
                    print(f"    ! 更新失敗: {e}")
                    continue
                if num_changed:
                    updated_num += 1
                if series_changed:
                    updated_series += 1

    print(f"\n[完了] 巻数更新 {updated_num} / シリーズ付け替え {updated_series} / 変更なし {unchanged}")
    if not dry_run:
        # 巻が0になった series（シリーズ付け替えで取り残された）をクリーンアップ
        orphan = supabase.rpc("noop").execute() if False else None  # placeholder
        print("\n（注）シリーズ付け替えで巻が0件になった旧シリーズが残る可能性があります。")
        print("    確認用 SQL: SELECT s.* FROM series s LEFT JOIN volumes v ON v.series_id=s.id WHERE v.id IS NULL;")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="volumes の巻数（とシリーズ名）を Gemini 2.5 Flash で調べ直す")
    parser.add_argument("--null-only", action="store_true", help="volume_number が NULL の行だけ対象")
    parser.add_argument("--limit", type=int, default=None, help="処理件数上限")
    parser.add_argument("--dry-run", action="store_true", help="DBは更新せず差分だけ表示")
    parser.add_argument("--batch", type=int, default=30, help="1回のGemini呼び出しに含める件数")
    args = parser.parse_args()
    main(null_only=args.null_only, limit=args.limit, dry_run=args.dry_run, batch=args.batch)
