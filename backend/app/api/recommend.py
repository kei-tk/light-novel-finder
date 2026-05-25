from fastapi import APIRouter, Body
from app.services.supabase_client import get_supabase
from app.services.gemini import generate_recommendation_tags

router = APIRouter(prefix="/recommend", tags=["recommend"])


@router.post("/")
def recommend_books(
    preference: str = Body(..., embed=True, description="読みたい雰囲気の説明"),
    trending_only: bool = Body(False, embed=True, description="トレンドのみに絞る"),
    limit: int = Body(10, embed=True),
):
    """
    ユーザーの好み（自然言語）からライトノベルを推薦する
    例: "切ない恋愛で泣けるやつ"
        "異世界でバトルがあって笑えるやつ"
    """
    tags = generate_recommendation_tags(preference)
    if not tags:
        return {"books": [], "matched_tags": []}

    supabase = get_supabase()
    matched = []
    seen_ids = set()

    for tag in tags:
        for field in ["atmosphere_tags", "genre_tags", "sub_genre_tags"]:
            query = supabase.table("series").select("*") \
                .contains(field, [tag]) \
                .order("max_tweet_likes", desc=True) \
                .limit(limit)

            if trending_only:
                query = query.eq("is_trending", True)

            result = query.execute()
            for s in (result.data or []):
                if s["id"] not in seen_ids:
                    s["matched_tag"] = tag
                    matched.append(s)
                    seen_ids.add(s["id"])

        if len(matched) >= limit:
            break

    return {
        "series": matched[:limit],
        "matched_tags": tags,
    }
