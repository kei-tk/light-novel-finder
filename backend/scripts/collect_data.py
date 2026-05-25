"""
ツイートからデータ収集スクリプト（series + volumes 構成対応版）

使い方:
  python -m scripts.collect_data          # トレンドモード（500いいね以上のみ）
  python -m scripts.collect_data --all    # 全件モード（いいね数制限なし）
"""
import sys
import os
import time
import argparse
import re
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.services.twitter import fetch_tweets
from app.services.gemini import extract_book_from_tweet, enrich_series, parse_volume_title
from app.services.supabase_client import get_supabase

GEMINI_INTERVAL_SEC = 1  # Tier 2 想定（60 RPM）


def normalize_date(s):
    if not s:
        return None
    s = str(s).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return f"{s}-01"
    return None


def get_confirmed_volume_titles(supabase) -> set[str]:
    """発売日と著者がそろっている確定済みの巻タイトル一覧"""
    rows = supabase.table("volumes").select("volume_title, published_date, series:series_id(author)") \
        .not_.is_("published_date", "null").execute()
    titles = set()
    for r in rows.data:
        ser = r.get("series")
        if ser and ser.get("author"):
            titles.add(r["volume_title"])
    return titles


def get_latest_tweet_id(supabase) -> str | None:
    result = supabase.table("tweets").select("tweet_id") \
        .order("tweeted_at", desc=True).limit(1).execute()
    return result.data[0]["tweet_id"] if result.data else None


def save_tweet(supabase, tweet: dict, volume_id: str, tweet_type: str):
    existing = supabase.table("tweets").select("id").eq("tweet_id", tweet["tweet_id"]).execute()
    if existing.data:
        return
    supabase.table("tweets").insert({
        "tweet_id": tweet["tweet_id"],
        "content": tweet["content"],
        "tweet_type": tweet_type,
        "tweeted_at": tweet["tweeted_at"],
        "volume_id": volume_id,
        "like_count": tweet.get("like_count", 0),
        "retweet_count": tweet.get("retweet_count", 0),
    }).execute()


def upsert_series_and_volume(supabase, volume_title: str, author: str | None,
                              published_date: str | None, cover: str | None,
                              like_count: int) -> tuple[str, str, str, bool, bool]:
    """
    シリーズと巻を upsert。
    Returns (series_id, volume_id, series_title, is_new_series, is_new_volume)
    """
    series_title, volume_number = parse_volume_title(volume_title)

    # series upsert
    existing_s = supabase.table("series").select("id, author, max_tweet_likes") \
        .eq("title", series_title).execute()
    if existing_s.data:
        sid = existing_s.data[0]["id"]
        is_new_series = False
        s_update = {}
        if author and not existing_s.data[0].get("author"):
            s_update["author"] = author
        current_max = existing_s.data[0].get("max_tweet_likes") or 0
        if like_count > current_max:
            s_update["max_tweet_likes"] = like_count
            s_update["is_trending"] = like_count >= settings.like_threshold
        if s_update:
            supabase.table("series").update(s_update).eq("id", sid).execute()
    else:
        is_new_series = True
        ins = supabase.table("series").insert({
            "title": series_title,
            "author": author,
            "label": settings.label_name,
            "cover_image_url": cover if volume_number == 1 else None,
            "max_tweet_likes": like_count,
            "is_trending": like_count >= settings.like_threshold,
        }).execute()
        sid = ins.data[0]["id"]

    # volume upsert
    existing_v = supabase.table("volumes").select("id, max_tweet_likes, published_date, cover_image_url") \
        .eq("series_id", sid).eq("volume_title", volume_title).execute()
    if existing_v.data:
        vid = existing_v.data[0]["id"]
        is_new_volume = False
        v_update = {}
        if published_date and not existing_v.data[0].get("published_date"):
            v_update["published_date"] = published_date
        if cover and not existing_v.data[0].get("cover_image_url"):
            v_update["cover_image_url"] = cover
        cur_max = existing_v.data[0].get("max_tweet_likes") or 0
        if like_count > cur_max:
            v_update["max_tweet_likes"] = like_count
        if v_update:
            supabase.table("volumes").update(v_update).eq("id", vid).execute()
    else:
        is_new_volume = True
        ins = supabase.table("volumes").insert({
            "series_id": sid,
            "volume_title": volume_title,
            "volume_number": volume_number,
            "published_date": published_date,
            "cover_image_url": cover,
            "max_tweet_likes": like_count,
        }).execute()
        vid = ins.data[0]["id"]

    return sid, vid, series_title, is_new_series, is_new_volume


def collect(all_mode: bool = False, reset: bool = False,
            from_months: int = None, to_months: int = 0):
    supabase = get_supabase()

    confirmed_titles = get_confirmed_volume_titles(supabase)
    print(f"[確定済み巻] {len(confirmed_titles)}件 → 取得から除外")

    since_id = None if reset else get_latest_tweet_id(supabase)
    if reset:
        print(f"[リセットモード] since_id無視")

    tweets = fetch_tweets(
        all_mode=all_mode,
        since_id=since_id,
        confirmed_titles=confirmed_titles,
        from_months=from_months,
        to_months=to_months,
    )
    print(f"[取得完了] {len(tweets)}件のツイート\n")

    new_series_ids: list[tuple[str, str]] = []  # (series_id, series_title)

    for i, tweet in enumerate(tweets):
        existing = supabase.table("tweets").select("id").eq("tweet_id", tweet["tweet_id"]).execute()
        if existing.data:
            continue

        print(f"[{i+1}/{len(tweets)}] 判定中...")
        time.sleep(GEMINI_INTERVAL_SEC)
        result = extract_book_from_tweet(tweet["content"])

        if not result.get("is_book_related") or not result.get("title"):
            print(f"  → 書籍無関係: {tweet['content'][:40]}...")
            continue

        volume_title = result["title"]
        author = result.get("author")
        published_date = normalize_date(result.get("published_date"))
        like_count = tweet.get("like_count", 0)
        cover = tweet.get("cover_image_url")
        print(f"  → 書籍発見:「{volume_title}」 いいね:{like_count}")

        sid, vid, series_title, is_new_s, is_new_v = upsert_series_and_volume(
            supabase, volume_title, author, published_date, cover, like_count,
        )
        if is_new_s:
            new_series_ids.append((sid, series_title))

        save_tweet(supabase, tweet, vid, "announcement")
        print(f"  → 保存（{'新規' if is_new_v else '既存'}巻 / {'新規' if is_new_s else '既存'}シリーズ）")

    # 新規シリーズはエンリッチ
    if new_series_ids:
        print(f"\n[エンリッチ開始] {len(new_series_ids)}シリーズ")
        for i, (sid, stitle) in enumerate(new_series_ids):
            print(f"[{i+1}/{len(new_series_ids)}] 「{stitle}」")
            time.sleep(GEMINI_INTERVAL_SEC)
            row = supabase.table("series").select("author").eq("id", sid).execute().data[0]
            enriched = enrich_series(stitle, row.get("author"))
            if not enriched:
                print(f"  → スキップ")
                continue
            update = {}
            for key in ("synopsis", "atmosphere_tags", "genre_tags", "sub_genre_tags", "target_audience"):
                if enriched.get(key):
                    update[key] = enriched[key]
            if not row.get("author") and enriched.get("author"):
                update["author"] = enriched["author"]
            if update:
                supabase.table("series").update(update).eq("id", sid).execute()
            print(f"  → 完了")

    print(f"\n[完了] 新規シリーズ {len(new_series_ids)}件")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--from-months", type=int, default=None)
    parser.add_argument("--to-months", type=int, default=0)
    args = parser.parse_args()

    mode_label = "全件" if args.all else f"{settings.like_threshold}いいね以上"
    print(f"=== {mode_label}モード ===")

    collect(
        all_mode=args.all,
        reset=args.reset,
        from_months=args.from_months,
        to_months=args.to_months,
    )
