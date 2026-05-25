"""
DBの状態確認スクリプト
使い方: python -m scripts.debug_check
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase

def main():
    supabase = get_supabase()

    # ツイート数確認
    tweets = supabase.table("tweets").select("*").execute()
    print(f"\n📊 DBの状態:")
    print(f"  ツイート総数: {len(tweets.data)}件")

    # ツイートの種類別集計
    types = {}
    for t in tweets.data:
        tt = t.get("tweet_type", "unknown")
        types[tt] = types.get(tt, 0) + 1
    for k, v in types.items():
        print(f"    - {k}: {v}件")

    # 書籍数確認
    books = supabase.table("books").select("*").execute()
    print(f"  書籍数: {len(books.data)}件")

    # ツイートのサンプル表示（先頭5件）
    if tweets.data:
        print(f"\n📝 保存済みツイートのサンプル（先頭5件）:")
        for t in tweets.data[:5]:
            print(f"  [{t.get('tweet_type')}] {t['content'][:80]}...")
    else:
        print("\n⚠️  ツイートが1件も保存されていません。X APIの取得に問題がある可能性があります。")

if __name__ == "__main__":
    main()
