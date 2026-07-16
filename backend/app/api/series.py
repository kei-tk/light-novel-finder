"""シリーズ関連 API (新スキーマ openbd_test_series/books 対応)。"""

import datetime
import random

from fastapi import APIRouter, HTTPException, Query
from app.services.supabase_client import get_supabase

router = APIRouter(prefix="/series", tags=["series"])

# 最新刊の新しさによる重みの半減期 (年)。これだけ古くなると表示確率が半分。
_RECENCY_HALFLIFE_YEARS = 2.0

# 一覧・詳細で返す列。8本のベクトル列 (embedding 系) と巨大 JSONB の raw は除外する。
# vector(3072)×6 + 単体ベクトルは 1行あたり数百KB の JSON になり、SELECT * だと
# プール 120件で十数MB を毎回転送・パースして応答が 8〜9秒に膨らむ。
# 検索用ベクトルは vector_search がインメモリで別途保持するため API では返さない。
_COLS = (
    "id,series_title,author,illustrator,contributors,publisher,imprint,"
    "cover_image_url,description,volume_count,first_published_date,latest_published_date,"
    "synopsis,atmosphere_tags,genre_tags,sub_genre_tags,target_audience,"
    "created_at,updated_at,normalized_title,is_series,parent_series_id,is_special_edition,"
    "comedy_score,serious_score,heartwarming_score,bittersweet_score,romantic_score,"
    "uplifting_score,popularity_score,tagline,momentum_score,momentum_reason,"
    "embedding_source,embedded_at,review_summary,review_summary_model,review_sources,"
    "review_embedded_at,plot_text,cast_text,theme_text,mood_text,reader_text,"
    "review_aspect_text,aspect_source,aspect_built_at"
)


def _sample(rows: list[dict], limit: int) -> list[dict]:
    """上位候補プールから limit 件をランダム抽出 (ホーム表示にランダム性を持たせる)。
    プールが limit 以下ならシャッフルのみ。"""
    if len(rows) <= limit:
        random.shuffle(rows)
        return rows
    return random.sample(rows, limit)


def _recency_weight(date_str: str | None) -> float:
    """最新刊発売日が新しいほど大きい重み (指数減衰)。古いほど小さく、日付不明は低め。"""
    if not date_str:
        return 0.15
    try:
        d = datetime.date.fromisoformat(str(date_str)[:10])
    except ValueError:
        return 0.15
    age_years = max((datetime.date.today() - d).days, 0) / 365.0
    return 0.5 ** (age_years / _RECENCY_HALFLIFE_YEARS)


def _weighted_sample(rows: list[dict], limit: int) -> list[dict]:
    """latest_published_date の新しさで重み付けした非復元ランダム抽出。
    最新刊が古いほど選ばれにくい (Efraimidis-Spirakis 法)。"""
    if len(rows) <= limit:
        random.shuffle(rows)
        return rows
    keyed = []
    for r in rows:
        w = max(_recency_weight(r.get("latest_published_date")), 1e-6)
        # key = U^(1/w): 重み大ほど key が大きくなりやすい → 上位に来やすい
        keyed.append((random.random() ** (1.0 / w), r))
    keyed.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in keyed[:limit]]


def _attach_latest_covers(series_rows: list[dict]) -> list[dict]:
    """各シリーズの cover_image_url を「カバーのある最新巻の書影」で上書きする。

    シリーズの代表書影 (= 1巻) は古いことが多いため、一覧表示では最新巻を見せたい。
    最新刊にカバーが無い場合は、カバーのある中で最も新しい巻 (巻数→発売日順) を使う。
    N+1 を避けて 1クエリでまとめて取得する。
    """
    if not series_rows:
        return series_rows
    ids = [s["id"] for s in series_rows if s.get("id")]
    if not ids:
        return series_rows
    sb = get_supabase()
    books = sb.table("openbd_test_books").select(
        "series_id, cover_image_url, published_date, volume_number"
    ).in_("series_id", ids).execute().data or []

    # series_id -> (順序キー, cover_url)。カバーのある巻のみ対象に、最新巻を保持。
    latest: dict[str, tuple[tuple, str]] = {}
    for b in books:
        sid = b.get("series_id")
        url = b.get("cover_image_url")
        if not sid or not url:  # カバーの無い巻はスキップ
            continue
        key = (b.get("volume_number") or 0, b.get("published_date") or "")
        prev = latest.get(sid)
        if prev is None or key > prev[0]:
            latest[sid] = (key, url)

    for s in series_rows:
        cover = latest.get(s["id"], (None, None))[1]
        if cover:
            s["cover_image_url"] = cover
    return series_rows


def _best_cover_from_books(books: list[dict]) -> str | None:
    """カバーのある巻の中で最新巻 (巻数→発売日順) のカバー URL を返す。"""
    best_key, best_url = None, None
    for b in books:
        url = b.get("cover_image_url")
        if not url:
            continue
        key = (b.get("volume_number") or 0, b.get("published_date") or "")
        if best_key is None or key > best_key:
            best_key, best_url = key, url
    return best_url


# ----------------- 一覧系 -----------------

def _split_tags(s: str | None) -> list[str]:
    """カンマ区切りのタグ文字列をリストに。複数選択フィルタ用。"""
    return [t.strip() for t in (s or "").split(",") if t.strip()]


@router.get("/")
def list_series(
    genre: str | None = Query(None, description="ジャンルタグ (カンマ区切りで複数=AND)"),
    atmosphere: str | None = Query(None, description="雰囲気タグ (カンマ区切りで複数=AND)"),
    sub_genre: str | None = Query(None, description="サブジャンルタグ (カンマ区切りで複数=AND)"),
    author: str | None = Query(None, description="著者で絞り込み (完全一致)"),
    sort: str = Query("popularity", description="popularity / momentum / new / title"),
    min_comedy: int | None = Query(None, ge=0, le=100),
    min_serious: int | None = Query(None, ge=0, le=100),
    min_heartwarming: int | None = Query(None, ge=0, le=100),
    min_bittersweet: int | None = Query(None, ge=0, le=100),
    min_romantic: int | None = Query(None, ge=0, le=100),
    min_uplifting: int | None = Query(None, ge=0, le=100),
    limit: int = Query(24, le=100),
    offset: int = Query(0, ge=0),
):
    """シリーズ一覧 (フィルタ + ソート)。タグはカンマ区切りで複数指定=AND 絞り込み。"""
    supabase = get_supabase()
    # ページネーション用に total を返したいので count=exact
    query = supabase.table("openbd_test_series").select(_COLS, count="exact")

    # parent_series_id が NULL の (= 親シリーズ) のみ返す。サブシリーズは詳細から辿る
    query = query.is_("parent_series_id", "null")

    if author:
        query = query.eq("author", author)

    # 複数タグは contains([...]) で「全て含む (AND)」絞り込み
    g, a, sg = _split_tags(genre), _split_tags(atmosphere), _split_tags(sub_genre)
    if g:
        query = query.contains("genre_tags", g)
    if a:
        query = query.contains("atmosphere_tags", a)
    if sg:
        query = query.contains("sub_genre_tags", sg)

    score_filters = {
        "comedy_score": min_comedy,
        "serious_score": min_serious,
        "heartwarming_score": min_heartwarming,
        "bittersweet_score": min_bittersweet,
        "romantic_score": min_romantic,
        "uplifting_score": min_uplifting,
    }
    for col, val in score_filters.items():
        if val is not None:
            query = query.gte(col, val)

    sort_map = {
        "popularity": ("popularity_score", True),
        "momentum": ("momentum_score", True),
        "new": ("latest_published_date", True),
        "title": ("series_title", False),
    }
    col, desc = sort_map.get(sort, sort_map["popularity"])
    query = query.order(col, desc=desc, nullsfirst=False)
    query = query.range(offset, offset + limit - 1)

    result = query.execute()
    rows = _attach_latest_covers(result.data or [])
    return {"series": rows, "count": len(rows), "total": result.count or 0}


@router.get("/new")
def new_releases(limit: int = Query(12, le=50)):
    """新刊シリーズ (最新巻発売日が新しい順の上位プールからランダム抽出)。"""
    supabase = get_supabase()
    pool = min(max(limit * 4, 40), 100)
    result = supabase.table("openbd_test_series").select(_COLS) \
        .is_("parent_series_id", "null") \
        .not_.is_("latest_published_date", "null") \
        .order("latest_published_date", desc=True, nullsfirst=False) \
        .limit(pool).execute()
    return {"series": _attach_latest_covers(_sample(result.data or [], limit))}


@router.get("/trending")
def trending(limit: int = Query(12, le=50)):
    """話題のシリーズ (momentum_score 上位プールからランダム抽出)。"""
    supabase = get_supabase()
    pool = min(max(limit * 6, 60), 120)
    result = supabase.table("openbd_test_series").select(_COLS) \
        .is_("parent_series_id", "null") \
        .not_.is_("momentum_score", "null") \
        .order("momentum_score", desc=True, nullsfirst=False) \
        .limit(pool).execute()
    return {"series": _attach_latest_covers(_weighted_sample(result.data or [], limit))}


@router.get("/recommended")
def recommended(limit: int = Query(12, le=50)):
    """おすすめ (popularity_score 上位プールからランダム抽出した定番シリーズ)。"""
    supabase = get_supabase()
    pool = min(max(limit * 6, 60), 120)
    result = supabase.table("openbd_test_series").select(_COLS) \
        .is_("parent_series_id", "null") \
        .not_.is_("popularity_score", "null") \
        .order("popularity_score", desc=True, nullsfirst=False) \
        .limit(pool).execute()
    return {"series": _attach_latest_covers(_weighted_sample(result.data or [], limit))}


@router.get("/search")
def search(
    q: str = Query(..., min_length=1, description="検索キーワード (タイトルまたは著者)"),
    limit: int = Query(24, le=100),
    offset: int = Query(0, ge=0),
):
    """キーワード検索 (タイトル + 著者の ilike OR 検索)。"""
    supabase = get_supabase()
    # PostgREST の OR 文法: or=(field1.ilike.%q%,field2.ilike.%q%)
    pattern = f"%{q}%"
    result = supabase.table("openbd_test_series").select(_COLS, count="exact") \
        .is_("parent_series_id", "null") \
        .or_(f"series_title.ilike.{pattern},author.ilike.{pattern}") \
        .order("popularity_score", desc=True, nullsfirst=False) \
        .range(offset, offset + limit - 1).execute()
    return {
        "series": _attach_latest_covers(result.data or []),
        "query": q,
        "total": result.count or 0,
    }


# ----------------- ベクトル検索 (類似 / 自然言語 / おすすめ) -----------------

def _fetch_series_ordered(ids: list[str]) -> list[dict]:
    """series_id のリストを順序維持で取得し、最新カバーを付与。"""
    if not ids:
        return []
    rows = get_supabase().table("openbd_test_series").select(_COLS) \
        .in_("id", ids).execute().data or []
    by_id = {r["id"]: r for r in rows}
    ordered = [by_id[i] for i in ids if i in by_id]
    return _attach_latest_covers(ordered)


@router.get("/semantic")
def semantic_search(
    q: str = Query(..., min_length=1, description="自然言語クエリ"),
    genre: str | None = Query(None, description="ジャンル絞り込み。未指定=AI推定, 空文字=絞り込みなし, カンマ区切り=そのタグ"),
    genre_mode: str | None = Query(None, description="and / or。未指定なら AI 推定"),
    limit: int = Query(48, le=120, description="1ページの件数"),
    offset: int = Query(0, ge=0, description="さらに見る用オフセット"),
):
    """自然言語クエリでのハイブリッド類似検索 (6観点ベクトル + ジャンル絞り込み)。
    ジャンルタグと AND/OR は AI が推定するが、genre / genre_mode で上書きできる。
    候補を関連度順に並べ、offset/limit で『さらに見る』式に返す。件数では区切らない。
    応答の genres / genre_mode は「実際に適用した」値 (フロントのチップ表示・トグル用)。
    """
    from app.services import vector_search
    # genre 未指定(None)=AI推定 / 空文字=絞り込み無し([]) / カンマ区切り=そのタグ
    genres = None if genre is None else [g.strip() for g in genre.split(",") if g.strip()]
    hits, applied_genres, applied_mode = vector_search.semantic(
        q, genres=genres, genre_mode=genre_mode)
    total = len(hits)
    page = hits[offset:offset + limit]
    return {
        "query": q,
        "series": _fetch_series_ordered([sid for sid, _ in page]),
        "total": total,
        "offset": offset,
        "has_more": offset + len(page) < total,
        "genres": applied_genres,
        "genre_mode": applied_mode,
    }


@router.get("/{series_id}/similar")
def similar_series(series_id: str, limit: int = Query(12, le=40)):
    """この series に似ているシリーズ (埋め込みベクトル近傍)。"""
    from app.services import vector_search
    hits = vector_search.similar(series_id, limit)
    return {"series": _fetch_series_ordered([sid for sid, _ in hits])}


# ----------------- タグ -----------------

@router.get("/tags")
def list_tags():
    """利用可能なタグ一覧 (シリーズに実際に付与されているもの)。"""
    supabase = get_supabase()

    rows = supabase.table("openbd_test_series").select(
        "atmosphere_tags, genre_tags, sub_genre_tags"
    ).execute().data or []

    buckets: dict[str, set[str]] = {
        "atmosphere": set(),
        "genre": set(),
        "sub_genre": set(),
    }
    for r in rows:
        for tag in r.get("atmosphere_tags") or []:
            if tag:
                buckets["atmosphere"].add(tag)
        for tag in r.get("genre_tags") or []:
            if tag:
                buckets["genre"].add(tag)
        for tag in r.get("sub_genre_tags") or []:
            if tag:
                buckets["sub_genre"].add(tag)

    return {
        "atmosphere_tags": sorted(buckets["atmosphere"]),
        "genre_tags": sorted(buckets["genre"]),
        "sub_genre_tags": sorted(buckets["sub_genre"]),
    }


# ----------------- 詳細 -----------------

@router.get("/{series_id}")
def get_series(series_id: str):
    """シリーズ詳細 (巻一覧 + サブシリーズ付き)。"""
    supabase = get_supabase()
    result = supabase.table("openbd_test_series").select(_COLS) \
        .eq("id", series_id).maybe_single().execute()
    if not result or not result.data:
        raise HTTPException(status_code=404, detail="シリーズが見つかりません")

    # 巻一覧 (本編 + サブシリーズの本を全て取得)
    books = supabase.table("openbd_test_books").select(
        "isbn, title, author, illustrator, volume_number, volume_number_sub, "
        "attached_to_isbn, published_date, cover_image_url, description"
    ) \
        .eq("series_id", series_id) \
        .order("volume_number", desc=False, nullsfirst=False) \
        .order("published_date", desc=False, nullsfirst=False) \
        .execute()

    # サブシリーズ (このシリーズが親のもの)
    sub_series = supabase.table("openbd_test_series").select(
        "id, series_title, volume_count, cover_image_url, latest_published_date"
    ).eq("parent_series_id", series_id).execute()

    data = {
        **result.data,
        "books": books.data or [],
        "sub_series": sub_series.data or [],
    }
    # 最新刊にカバーが無ければ、カバーのある最新巻のカバーを使う
    best_cover = _best_cover_from_books(books.data or [])
    if best_cover:
        data["cover_image_url"] = best_cover
    return data
