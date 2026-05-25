"""
保存済みツイートの再分析スクリプト
tweet_type="other" または book_id が紐付いていないツイートを対象に再分析する
使い方: python -m scripts.reanalyze_tweets
"""
import sys
import os
import time
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings

# Gemini無料枠: 1分15リクエストまで → 1リクエストあたり4秒以上あける
GEMINI_INTERVAL_SEC = 1  # Tier 2 想定（60 RPM）
from app.services.twitter import fetch_replies_for_book
from app.services.gemini import analyze_book_tweet, analyze_reviews
from app.services.supabase_client import get_supabase


def get_confirmed_titles(supabase) -> set[str]:
    result = supabase.table("books").select("title") \
        .not_.is_("title", "null") \
        .not_.is_("author", "null") \
        .not_.is_("published_date", "null").execute()
    return {row["title"] for row in result.data}


def reanalyze():
    supabase = get_supabase()

    # book_id が未設定のツイートをすべて取得（other / 未紐付けのannouncement）
    result = supabase.table("tweets").select("*").is_("book_id", "null").execute()
    targets = result.data
    print(f"[再分析対象] {len(targets)}件のツイート")

    books_saved = 0
    book_updated = 0

    for i, tweet in enumerate(targets):
        print(f"\n[{i+1}/{len(targets)}] 分析中...")
        time.sleep(GEMINI_INTERVAL_SEC)
        analysis = analyze_book_tweet(tweet["content"])

        # 書籍発売に無関係 → tweet_type を other に更新して次へ
        if not analysis.get("is_book_related") or not analysis.get("title"):
            supabase.table("tweets").update({"tweet_type": "other"}) \
                .eq("id", tweet["id"]).execute()
            continue

        title = analysis["title"]
        author = analysis.get("author")
        published_date = analysis.get("published_date")
        print(f"  [書籍発見] {title} / {author} / {published_date}")

        # 既存書籍チェック
        existing_book = supabase.table("books").select("id, author, published_date") \
            .eq("title", title).execute()

        if existing_book.data:
            book_id = existing_book.data[0]["id"]
            db_book = existing_book.data[0]

            # 不足情報を補完
            update_data = {}
            if not db_book.get("author") and author:
                update_data["author"] = author
            if not db_book.get("published_date") and published_date:
                update_data["published_date"] = published_date
            if update_data:
                supabase.table("books").update(update_data).eq("id", book_id).execute()
                print(f"    → 情報補完: {update_data}")
                book_updated += 1
        else:
            # 新規書籍として保存
            book_result = supabase.table("books").insert({
                "title": title,
                "author": author,
                "published_date": published_date,
                "description": analysis.get("description"),
                "atmosphere_tags": analysis.get("atmosphere_tags", []),
                "genre_tags": analysis.get("genre_tags", []),
                "label": settings.label_name,
            }).execute()
            book_id = book_result.data[0]["id"]
            books_saved += 1
            print(f"    → 新規保存")

        # ツイートに book_id を紐付けて announcement に更新（いいね数も保存）
        supabase.table("tweets").update({
            "book_id": book_id,
            "tweet_type": "announcement",
            "like_count": tweet.get("like_count", 0),
            "retweet_count": tweet.get("retweet_count", 0),
        }).eq("id", tweet["id"]).execute()

        # 確定済みでなければ感想ツイートを収集
        confirmed_titles = get_confirmed_titles(supabase)
        if title not in confirmed_titles:
            review_tweets = fetch_replies_for_book(title)
            review_texts = [r["content"] for r in review_tweets]
            review_analysis = analyze_reviews(title, review_texts)

            supabase.table("book_analyses").upsert({
                "book_id": book_id,
                "atmosphere_keywords": review_analysis.get("keywords", []),
                "themes": analysis.get("themes", []),
                "gemini_summary": review_analysis.get("summary", ""),
            }, on_conflict="book_id").execute()

            for review in review_tweets:
                existing_r = supabase.table("tweets").select("id") \
                    .eq("tweet_id", review["tweet_id"]).execute()
                if not existing_r.data:
                    supabase.table("tweets").insert({
                        "tweet_id": review["tweet_id"],
                        "content": review["content"],
                        "tweet_type": "review",
                        "tweeted_at": review.get("tweeted_at"),
                        "book_id": book_id,
                        "like_count": review.get("like_count", 0),
                        "retweet_count": review.get("retweet_count", 0),
                    }).execute()

    print(f"\n[完了] 新規書籍 {books_saved}件 / 情報補完 {book_updated}件")


if __name__ == "__main__":
    reanalyze()
