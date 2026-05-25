import tweepy
from datetime import datetime, timedelta
from app.config import settings

MAX_QUERY_LEN = 500


def get_client() -> tweepy.Client:
    return tweepy.Client(bearer_token=settings.x_bearer_token, wait_on_rate_limit=True)


def build_query(all_mode: bool = False, confirmed_titles: set[str] = None) -> str:
    """
    検索クエリを構築する。
    - all_mode=True: いいね数フィルタなし
    - confirmed_titles: 確定済みタイトルを -"タイトル" で除外
    ※ min_faves は X API Pro 以上の有料機能なので使わず、いいね数は常にローカルフィルタ。
    """
    base = f"from:{settings.target_x_account} -is:retweet"
    query = base
    excluded_count = 0
    confirmed_titles = confirmed_titles or set()

    for title in confirmed_titles:
        part = f' -"{title}"'
        if len(query) + len(part) > MAX_QUERY_LEN:
            break
        query += part
        excluded_count += 1

    total = len(confirmed_titles)
    if total > 0:
        print(f"[X API] クエリ除外: {excluded_count}/{total}件（残り{total - excluded_count}件はローカルフィルタ）")

    return query


def fetch_tweets(
    all_mode: bool = False,
    since_id: str = None,
    confirmed_titles: set[str] = None,
    from_months: int = None,
    to_months: int = 0,
) -> list[dict]:
    """
    公式アカウントのツイートを取得する。

    初回（since_id なし）:
      - search_all_tweets で全期間取得
      - min_faves 非対応のためローカルでいいね数フィルタ

    2回目以降（since_id あり）:
      - search_recent_tweets で差分取得
      - min_faves 対応のためAPI側でいいね数フィルタ
    """
    client = get_client()
    confirmed_titles = confirmed_titles or set()

    common_params = dict(
        tweet_fields=["created_at", "text", "attachments", "public_metrics"],
        expansions=["attachments.media_keys"],
        media_fields=["url", "preview_image_url"],
        max_results=100,
    )

    # 期間設定
    _from = from_months if from_months is not None else settings.data_fetch_months
    start_time = datetime.utcnow() - timedelta(days=30 * _from)
    end_time = datetime.utcnow() - timedelta(days=30 * to_months) if to_months > 0 else None
    period_label = f"{_from}ヶ月前〜{f'{to_months}ヶ月前' if to_months > 0 else '現在'}"

    query = build_query(all_mode=all_mode, confirmed_titles=confirmed_titles)
    filter_label = "いいね数フィルタなし" if all_mode else f"{settings.like_threshold}いいね以上（ローカル）"

    if since_id:
        # 2回目以降: since_id で差分取得（--all でも重複取得しない）
        print(f"[X API] 差分取得モード（{filter_label}・重複なし）")
        paginator = tweepy.Paginator(
            client.search_recent_tweets,
            query=query,
            since_id=since_id,
            limit=10,
            **common_params,
        )
    else:
        # 初回: 指定期間取得（search_all_tweets・ローカルフィルタ）
        print(f"[X API] 初回取得モード（{period_label}・{filter_label}）")
        kwargs = dict(query=query, start_time=start_time, limit=50, **common_params)
        if end_time:
            kwargs["end_time"] = end_time
        paginator = tweepy.Paginator(client.search_all_tweets, **kwargs)

    print(f"[X API] クエリ: {query[:100]}{'...' if len(query) > 100 else ''}")

    raw_tweets = []
    skipped_title = 0
    skipped_likes = 0

    for response in paginator:
        if not response.data:
            continue

        media_map = {}
        if response.includes and "media" in response.includes:
            for media in response.includes["media"]:
                url = media.get("url") or media.get("preview_image_url", "")
                media_map[media["media_key"]] = url

        for tweet in response.data:
            # クエリ長超過分の確定済みタイトルをローカルで除外
            if any(title in tweet.text for title in confirmed_titles):
                skipped_title += 1
                continue

            metrics = tweet.public_metrics or {}
            like_count = metrics.get("like_count", 0)

            # いいね数フィルタ（min_faves が使えないため常にローカル）
            if not all_mode and like_count < settings.like_threshold:
                skipped_likes += 1
                continue

            cover_image_url = None
            if tweet.attachments and tweet.attachments.get("media_keys"):
                first_key = tweet.attachments["media_keys"][0]
                cover_image_url = media_map.get(first_key)

            raw_tweets.append({
                "tweet_id": str(tweet.id),
                "content": tweet.text,
                "tweeted_at": tweet.created_at.isoformat() if tweet.created_at else None,
                "cover_image_url": cover_image_url,
                "like_count": like_count,
                "retweet_count": metrics.get("retweet_count", 0),
            })

    if skipped_title:
        print(f"[X API] 確定済みタイトルで {skipped_title}件 除外")
    if skipped_likes:
        print(f"[X API] {settings.like_threshold}いいね未満で {skipped_likes}件 除外（ローカルフィルタ）")

    return raw_tweets
