"""
スケジュール実行 (書誌の取り直し・シリーズ判定・新作の文章づくり) で共通に使う処理。
"""
import datetime as dt
import time

import httpx

from app.config import settings


def window(days: int, today: dt.date | None = None) -> tuple[str, str]:
    """今日の前後 days 日 (両端を含む) を ISO 日付で返す"""
    today = today or dt.date.today()
    return (today - dt.timedelta(days=days)).isoformat(), (today + dt.timedelta(days=days)).isoformat()


def paginate(query_factory, page_size: int = 1000) -> list[dict]:
    """PostgREST は1回 1000 行までなので、range をずらして全件を取る"""
    out: list[dict] = []
    offset = 0
    while True:
        rows = query_factory().range(offset, offset + page_size - 1).execute().data or []
        out.extend(rows)
        if len(rows) < page_size:
            return out
        offset += page_size


AGG_COLS = ["volume_count", "is_series", "first_published_date", "latest_published_date", "cover_image_url", "description",
            "imprint", "illustrator", "publisher", "contributors"]


def _most_common(values: list):
    values = [v for v in values if v]
    return max(set(values), key=values.count) if values else None


def recompute_series(supabase, series_ids: set[str], dry_run: bool = False) -> int:
    """巻の追加や発売日の変更を、シリーズ行の集計 (巻数・発売日・書影など) に反映する。
    特装版 (attached_to_isbn あり) は巻に数えない。
    openbd_test_series はベクトル列と HNSW を持つので、1行の更新に数秒かかることがある。
    そのため値が同じなら更新せず、時間切れは待ってやり直し、それでも駄目なら報告して次へ進む"""
    done = 0
    failed: list[str] = []
    for sid in sorted(series_ids):
        books = (
            supabase.table("openbd_test_books")
            .select("isbn,published_date,volume_number,cover_image_url,description,illustrator,publisher,imprint,contributors")
            .eq("series_id", sid).is_("attached_to_isbn", "null")
            .execute().data or []
        )
        if not books:
            continue
        dates = sorted(b["published_date"] for b in books if b.get("published_date"))
        vol1 = min(books, key=lambda b: (b.get("volume_number") or 9999, b.get("published_date") or "9999"))
        record = {
            "volume_count": len(books),
            "is_series": len(books) >= 2,
            "first_published_date": dates[0] if dates else None,
            "latest_published_date": dates[-1] if dates else None,
            "cover_image_url": vol1.get("cover_image_url"),
            "description": vol1.get("description"),
            # 新しく作ったシリーズは題名と著者しか持たないので、カードに出すレーベルなども巻から埋める
            "imprint": _most_common([b.get("imprint") for b in books]),
            "illustrator": _most_common([b.get("illustrator") for b in books]),
            "publisher": _most_common([b.get("publisher") for b in books]),
            "contributors": vol1.get("contributors"),
        }
        current = supabase.table("openbd_test_series").select(",".join(AGG_COLS)).eq("id", sid).execute().data
        if current and all(current[0].get(k) == record[k] for k in AGG_COLS):
            continue
        if not dry_run:
            for attempt in range(3):
                try:
                    supabase.table("openbd_test_series").update(record).eq("id", sid).execute()
                    break
                except Exception as e:
                    if attempt == 2:
                        failed.append(sid)
                        print(f"  ! 集計の更新に失敗 {sid}: {str(e)[:80]}")
                    else:
                        time.sleep(5 * (attempt + 1))
        done += 1
    if failed:
        print(f"[集計] 更新できなかったシリーズ {len(failed)}件 (次の実行でもう一度試す): {failed}")
    return done


def revalidate(tags: list[str] | None = None) -> None:
    """ラノベ書庫のキャッシュを古い扱いにする (POST /api/revalidate)。失敗してもパイプラインは止めない"""
    urls = [u.strip().rstrip("/") for u in settings.revalidate_urls.split(",") if u.strip()]
    if not urls or not settings.revalidate_secret:
        print("[revalidate] REVALIDATE_URLS / REVALIDATE_SECRET が未設定なので呼ばない (反映はキャッシュの期限待ち)")
        return
    for base in urls:
        try:
            r = httpx.post(
                f"{base}/api/revalidate",
                headers={"x-revalidate-secret": settings.revalidate_secret},
                json={"tags": tags or ["series"]},
                timeout=30,
            )
            print(f"[revalidate] {base} → {r.status_code}")
        except Exception as e:
            print(f"[revalidate] {base} 失敗: {type(e).__name__}: {e}")
