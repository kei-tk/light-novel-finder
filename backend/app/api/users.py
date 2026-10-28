"""ユーザー機能 API (プロフィール / お気に入り / 読了済み)。

認証は Authorization: Bearer <Supabase JWT> ヘッダで受け取り、
supabase-py の auth.get_user(token) で検証する。
"""

import os

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, Field

from app.services.supabase_client import get_supabase
from app.api.series import _attach_latest_covers, _fetch_series_ordered

router = APIRouter(prefix="/users", tags=["users"])


# ----------------- 認証ヘルパ -----------------

def get_current_user(authorization: str | None = Header(None)) -> str:
    """Authorization ヘッダから JWT を検証し user_id を返す。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="認証が必要です")
    token = authorization.split(" ", 1)[1]
    try:
        sb = get_supabase()
        result = sb.auth.get_user(token)
        if not result or not result.user:
            raise HTTPException(status_code=401, detail="トークンが無効です")
        return result.user.id
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="トークン検証に失敗しました")


# ----------------- プロフィール -----------------

class ProfileUpdate(BaseModel):
    display_name: str | None = Field(None, max_length=64)
    bio: str | None = Field(None, max_length=500)
    favorite_genres: list[str] | None = None
    favorite_atmospheres: list[str] | None = None


@router.get("/me/profile")
def get_my_profile(user_id: str = Depends(get_current_user)):
    sb = get_supabase()
    result = sb.table("profiles").select("*").eq("id", user_id).maybe_single().execute()
    if not result or not result.data:
        # trigger で自動作成されているはずだが念のため空を返す
        return {"id": user_id, "display_name": None, "bio": None,
                "favorite_genres": [], "favorite_atmospheres": []}
    return result.data


@router.put("/me/profile")
def update_my_profile(body: ProfileUpdate, user_id: str = Depends(get_current_user)):
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    if not payload:
        raise HTTPException(status_code=400, detail="更新項目がありません")
    sb = get_supabase()
    result = sb.table("profiles").update(payload).eq("id", user_id).execute()
    return {"profile": result.data[0] if result.data else None}


# ----------------- お気に入り -----------------

@router.get("/me/favorites")
def list_favorites(user_id: str = Depends(get_current_user)):
    sb = get_supabase()
    rows = sb.table("favorite_series").select(
        "series_id, created_at, openbd_test_series(*)"
    ).eq("user_id", user_id).order("created_at", desc=True).execute()
    # joined series を平坦化
    series = []
    for r in rows.data or []:
        s = r.get("openbd_test_series") or {}
        if s:
            s["favorited_at"] = r.get("created_at")
            series.append(s)
    return {"series": _attach_latest_covers(series)}


@router.post("/me/favorites/{series_id}")
def add_favorite(series_id: str, user_id: str = Depends(get_current_user)):
    sb = get_supabase()
    sb.table("favorite_series").upsert({
        "user_id": user_id, "series_id": series_id,
    }).execute()
    return {"status": "ok"}


@router.delete("/me/favorites/{series_id}")
def remove_favorite(series_id: str, user_id: str = Depends(get_current_user)):
    sb = get_supabase()
    sb.table("favorite_series").delete() \
        .eq("user_id", user_id).eq("series_id", series_id).execute()
    return {"status": "ok"}


@router.get("/me/recommendations")
def my_recommendations(user_id: str = Depends(get_current_user), limit: int = 12):
    """お気に入り＋読了済みの埋め込みの重心に近い series を推薦 (ベクトルおすすめ)。"""
    sb = get_supabase()
    fav = sb.table("favorite_series").select("series_id") \
        .eq("user_id", user_id).order("created_at", desc=True).limit(50).execute().data or []
    read = sb.table("read_series").select("series_id") \
        .eq("user_id", user_id).limit(50).execute().data or []
    source_ids = list({r["series_id"] for r in fav + read})
    if not source_ids:
        return {"series": [], "based_on": 0}
    # Vercel には約600MBのベクトルキャッシュを載せないため、VPS/ローカルだけで提供する。
    if os.getenv("VERCEL"):
        return {"series": [], "based_on": len(source_ids)}
    from app.services import vector_search
    hits = vector_search.recommend(source_ids, limit)
    return {
        "series": _fetch_series_ordered([sid for sid, _ in hits]),
        "based_on": len(source_ids),
    }


# ----------------- 読了済み -----------------

class ReadEntry(BaseModel):
    rating: int | None = Field(None, ge=1, le=5)


@router.get("/me/read")
def list_read(user_id: str = Depends(get_current_user)):
    sb = get_supabase()
    rows = sb.table("read_series").select(
        "series_id, rating, finished_at, openbd_test_series(*)"
    ).eq("user_id", user_id).order("finished_at", desc=True).execute()
    series = []
    for r in rows.data or []:
        s = r.get("openbd_test_series") or {}
        if s:
            s["rating"] = r.get("rating")
            s["finished_at"] = r.get("finished_at")
            series.append(s)
    return {"series": _attach_latest_covers(series)}


@router.post("/me/read/{series_id}")
def mark_read(series_id: str, body: ReadEntry, user_id: str = Depends(get_current_user)):
    sb = get_supabase()
    sb.table("read_series").upsert({
        "user_id": user_id,
        "series_id": series_id,
        "rating": body.rating,
    }).execute()
    return {"status": "ok"}


@router.delete("/me/read/{series_id}")
def unmark_read(series_id: str, user_id: str = Depends(get_current_user)):
    sb = get_supabase()
    sb.table("read_series").delete() \
        .eq("user_id", user_id).eq("series_id", series_id).execute()
    return {"status": "ok"}


# ----------------- 状態取得 (フロントのトグル表示用) -----------------

@router.get("/me/status/{series_id}")
def get_series_status(series_id: str, user_id: str = Depends(get_current_user)):
    """特定シリーズに対する自分のフラグ。"""
    sb = get_supabase()
    fav = sb.table("favorite_series").select("series_id") \
        .eq("user_id", user_id).eq("series_id", series_id).execute()
    read = sb.table("read_series").select("rating") \
        .eq("user_id", user_id).eq("series_id", series_id).execute()
    return {
        "is_favorite": bool(fav.data),
        "is_read": bool(read.data),
        "rating": (read.data[0].get("rating") if read.data else None),
    }
