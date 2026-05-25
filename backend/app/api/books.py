from fastapi import APIRouter, HTTPException, Query
from app.services.supabase_client import get_supabase

router = APIRouter(prefix="/series", tags=["series"])


@router.get("/")
def list_series(
    genre: str = Query(None, description="ジャンルタグ"),
    atmosphere: str = Query(None, description="雰囲気タグ"),
    sub_genre: str = Query(None, description="サブジャンルタグ"),
    trending_only: bool = Query(False, description="トレンドのみ"),
    limit: int = Query(20, le=100),
    offset: int = Query(0, ge=0),
):
    """シリーズ一覧（タグ・トレンドフィルタ付き）"""
    supabase = get_supabase()
    query = supabase.table("series").select("*") \
        .order("max_tweet_likes", desc=True) \
        .range(offset, offset + limit - 1)

    if genre:
        query = query.contains("genre_tags", [genre])
    if atmosphere:
        query = query.contains("atmosphere_tags", [atmosphere])
    if sub_genre:
        query = query.contains("sub_genre_tags", [sub_genre])
    if trending_only:
        query = query.eq("is_trending", True)

    result = query.execute()
    return {"series": result.data, "total": len(result.data)}


@router.get("/trending")
def trending_series(limit: int = Query(20, le=100)):
    """トレンドシリーズ一覧（合計いいね数順）"""
    supabase = get_supabase()
    result = supabase.table("series_engagement").select("*") \
        .eq("is_trending", True) \
        .order("total_likes", desc=True) \
        .limit(limit).execute()
    return {"series": result.data}


@router.get("/ranking")
def series_ranking(limit: int = Query(20, le=100)):
    """全シリーズをいいね数順でランキング表示"""
    supabase = get_supabase()
    result = supabase.table("series_engagement").select("*") \
        .order("total_likes", desc=True) \
        .limit(limit).execute()
    return {"ranking": result.data}


@router.get("/tags")
def list_tags():
    """利用可能なタグ一覧（tagsマスタ + シリーズに付いているタグの和集合）"""
    supabase = get_supabase()

    master = supabase.table("tags").select("name, description, tag_type").execute().data or []
    by_type: dict[str, list[dict]] = {"atmosphere": [], "genre": [], "sub_genre": []}
    seen: dict[str, set[str]] = {k: set() for k in by_type}
    for r in master:
        t = r.get("tag_type")
        if t in by_type:
            name = r.get("name")
            if name and name not in seen[t]:
                seen[t].add(name)
                by_type[t].append({"name": name, "description": r.get("description") or ""})

    rows = supabase.table("series").select(
        "atmosphere_tags, genre_tags, sub_genre_tags"
    ).execute().data or []
    extras = {"atmosphere": set(), "genre": set(), "sub_genre": set()}
    for r in rows:
        for tag in r.get("atmosphere_tags") or []:
            if tag and tag not in seen["atmosphere"]:
                extras["atmosphere"].add(tag)
        for tag in r.get("genre_tags") or []:
            if tag and tag not in seen["genre"]:
                extras["genre"].add(tag)
        for tag in r.get("sub_genre_tags") or []:
            if tag and tag not in seen["sub_genre"]:
                extras["sub_genre"].add(tag)
    for k, names in extras.items():
        for n in sorted(names):
            by_type[k].append({"name": n, "description": ""})

    return {
        "atmosphere_tags": by_type["atmosphere"],
        "genre_tags":      by_type["genre"],
        "sub_genre_tags":  by_type["sub_genre"],
    }


@router.get("/{series_id}")
def get_series(series_id: str):
    """シリーズ詳細（巻一覧・関連ツイート付き）"""
    supabase = get_supabase()
    result = supabase.table("series").select("*").eq("id", series_id).single().execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="シリーズが見つかりません")

    volumes = supabase.table("volumes").select("*") \
        .eq("series_id", series_id) \
        .order("volume_number", desc=False, nullsfirst=False) \
        .order("published_date", desc=False).execute()

    volume_ids = [v["id"] for v in volumes.data]
    tweets = []
    if volume_ids:
        tweets_res = supabase.table("tweets").select(
            "content, tweet_type, like_count, tweeted_at, volume_id"
        ).in_("volume_id", volume_ids).order("like_count", desc=True).limit(10).execute()
        tweets = tweets_res.data

    engagement = supabase.table("series_engagement").select("*") \
        .eq("series_id", series_id).execute()

    return {
        **result.data,
        "volumes": volumes.data,
        "tweets": tweets,
        "engagement": engagement.data[0] if engagement.data else {},
    }
