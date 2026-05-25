"""
講談社ラノベ文庫公式サイトから既刊書籍を調査して DB の volumes に追記するスクリプト

公式サイトのHTMLを直接取得し、Gemini 3.1 Pro でパースして、
**既に series テーブルに登録されているシリーズ**に該当する巻だけを volumes に upsert する。
未登録シリーズの本はスキップ（series の新規作成はここでは行わない — collect_data 側の責務）。

使い方:
  python -m scripts.discover_official                # 過去2年分
  python -m scripts.discover_official --years 5      # 過去5年分
  python -m scripts.discover_official --year 2025    # 単年
  python -m scripts.discover_official --from 2020 --to 2026
"""
import sys
import os
import time
import argparse
import re
from datetime import date
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.services.gemini import discover_books_from_official_site, parse_volume_titles
from app.services.supabase_client import get_supabase

GEMINI_INTERVAL_SEC = 1  # Tier 2 想定（60 RPM）


def normalize_date(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return f"{s}-01"
    if re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", s):
        y, m, d = s.split("/")
        return f"{y}-{int(m):02d}-{int(d):02d}"
    if re.fullmatch(r"\d{4}/\d{1,2}", s):
        y, m = s.split("/")
        return f"{y}-{int(m):02d}-01"
    return None


def upsert_series(supabase, series_title: str, author: str | None,
                  cover_image_url: str | None, create_if_missing: bool) -> tuple[str | None, bool]:
    """
    シリーズを upsert。
    create_if_missing=False（デフォルト）: 既存のみ。なければ (None, False)。
    create_if_missing=True: 存在しなければ新規作成。
    Returns: (series_id, is_new)
    """
    existing = supabase.table("series").select("id, author, cover_image_url") \
        .eq("title", series_title).execute()
    if existing.data:
        sid = existing.data[0]["id"]
        update = {}
        if author and not existing.data[0].get("author"):
            update["author"] = author
        if cover_image_url:
            update["cover_image_url"] = cover_image_url
        if update:
            supabase.table("series").update(update).eq("id", sid).execute()
        return sid, False

    if not create_if_missing:
        return None, False

    ins = supabase.table("series").insert({
        "title": series_title,
        "author": author,
        "label": settings.label_name,
        "cover_image_url": cover_image_url,
        "max_tweet_likes": 0,
        "is_trending": False,
    }).execute()
    return (ins.data[0]["id"] if ins.data else None), True


def upsert_volume(supabase, series_id: str, volume_title: str, volume_number: int | None,
                  published_date: str | None, cover_image_url: str | None) -> bool:
    """volume upsert。Returns is_new"""
    existing = supabase.table("volumes").select("id, published_date, cover_image_url") \
        .eq("series_id", series_id).eq("volume_title", volume_title).execute()
    if existing.data:
        vid = existing.data[0]["id"]
        update = {}
        if published_date:
            update["published_date"] = published_date
        if cover_image_url:
            update["cover_image_url"] = cover_image_url
        if volume_number is not None:
            update["volume_number"] = volume_number
        if update:
            supabase.table("volumes").update(update).eq("id", vid).execute()
        return False

    supabase.table("volumes").insert({
        "series_id": series_id,
        "volume_title": volume_title,
        "volume_number": volume_number,
        "published_date": published_date,
        "cover_image_url": cover_image_url,
        "max_tweet_likes": 0,
    }).execute()
    return True


def main(start_year: int, end_year: int, create_series: bool):
    supabase = get_supabase()
    print(f"[調査期間] {start_year}年 〜 {end_year}年")
    print(f"[シリーズ新規作成] {'有効（--create-series）' if create_series else '無効（既存シリーズのみ更新）'}\n")

    new_series_count = 0
    new_volume_count = 0
    skipped_no_series = 0
    total_found = 0
    years = list(range(start_year, end_year + 1))

    for i, year in enumerate(years):
        print(f"[{i+1}/{len(years)}] {year}年 を調査中...")
        if i > 0:
            time.sleep(GEMINI_INTERVAL_SEC)
        books = discover_books_from_official_site(year=year)
        print(f"  → {len(books)}件 取得")
        total_found += len(books)

        # Gemini 2.5 Flash でこの年の全タイトルをバッチ分解
        titles_to_parse = [(b.get("title") or "").strip() for b in books if (b.get("title") or "").strip()]
        if titles_to_parse:
            time.sleep(GEMINI_INTERVAL_SEC)
            parsed_map = parse_volume_titles(titles_to_parse)
            print(f"  → タイトル分解 {len(parsed_map)}件")
        else:
            parsed_map = {}

        for book in books:
            volume_title = (book.get("title") or "").strip()
            if not volume_title:
                continue
            author = book.get("author")
            published_date = normalize_date(book.get("published_date"))
            cover = book.get("cover_image_url")

            series_title, volume_number = parsed_map.get(volume_title, (volume_title, None))

            sid, is_new_series = upsert_series(
                supabase, series_title, author,
                cover if volume_number == 1 else None,
                create_if_missing=create_series,
            )
            if sid is None:
                skipped_no_series += 1
                continue
            if is_new_series:
                new_series_count += 1
                print(f"  ＋ 新規シリーズ: 「{series_title}」")

            is_new_vol = upsert_volume(supabase, sid, volume_title, volume_number,
                                        published_date, cover)
            if is_new_vol:
                new_volume_count += 1
                print(f"  ＋ 新規巻: 「{volume_title}」({published_date or '日付不明'}) [{series_title}]")

    print(f"\n[サイト調査完了] 取得 {total_found}件 / "
          f"新規シリーズ {new_series_count}件 / 新規巻 {new_volume_count}件 / "
          f"未登録シリーズによりスキップ {skipped_no_series}件")
    print("\n[全体完了]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="公式サイトから既刊書籍を年単位で調査")
    parser.add_argument("--years", type=int, default=2, help="今年から遡る年数")
    parser.add_argument("--from", dest="from_year", type=int, default=None)
    parser.add_argument("--to", dest="to_year", type=int, default=None)
    parser.add_argument("--year", type=int, default=None, help="単年指定")
    parser.add_argument("--create-series", action="store_true",
                        help="未登録シリーズも新規作成する（リセット直後の初回投入用）")
    args = parser.parse_args()

    today_year = date.today().year
    if args.year:
        start_year = end_year = args.year
    else:
        end_year = args.to_year or today_year
        start_year = args.from_year or (end_year - args.years + 1)

    main(start_year=start_year, end_year=end_year, create_series=args.create_series)
