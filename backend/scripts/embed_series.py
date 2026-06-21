"""
openbd_test_series に OpenAI text-embedding-3-small でベクトル埋め込みを付与する。

埋め込みに使う情報 (series 1件 = 1ベクトル):
  - series_title / author
  - atmosphere_tags / genre_tags / sub_genre_tags
  - 7軸スコア (comedy/serious/heartwarming/bittersweet/romantic/uplifting/popularity/momentum)
  - description (配下全書籍のあらすじを連結、完全重複は除外)

並列化:
  - description フェッチ: ThreadPoolExecutor で N 並列の DB クエリ
  - embedding API: 100件バッチを ThreadPoolExecutor で N 並列で送信
  - DB 保存: 同上

OpenAI Embeddings API は 1リクエスト最大 2,048 inputs / 約 300K tokens 受け付ける。
本スクリプトは BATCH_SIZE 件単位でまとめ、さらに複数バッチを並列で投げる。

使い方:
  python -m scripts.embed_series --dry-run --limit 3            # 整形テキスト確認
  python -m scripts.embed_series --limit 100                    # 100件試す
  python -m scripts.embed_series                                # embedding NULL 全件
  python -m scripts.embed_series --redo-all                     # 既存も含めて再計算
  python -m scripts.embed_series --series-id <UUID>             # 特定 series
  python -m scripts.embed_series --workers 10                   # 並列度指定 (default 5)
"""
import sys
import os
import time
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai import OpenAI
from app.config import settings
from app.services.supabase_client import get_supabase

EMBEDDING_SOURCE_VERSION = "v1_synopsis_tags"
BATCH_SIZE = 100         # OpenAI に 1リクエストで投げる series 数
EMBEDDING_DIM = 1536
DEFAULT_WORKERS = 5

_openai_client = OpenAI(api_key=settings.openai_api_key)
_print_lock = threading.Lock()
_progress_lock = threading.Lock()


def _safe_print(msg: str) -> None:
    with _print_lock:
        print(msg)


def _retry_supabase(fn, *, what: str, max_retries: int = 5, base_wait: float = 1.0):
    """
    Supabase 呼び出しを一時的エラー (httpx HTTP/2 protocol error, Cloudflare 400 HTML,
    Bad Gateway 等) で指数バックオフリトライする。
    """
    transient_markers = (
        "pseudo-header",
        "cloudflare",
        "JSON could not be generated",
        "Bad Gateway",
        "502",
        "503",
        "504",
        "Connection reset",
        "RemoteProtocolError",
        "ReadTimeout",
        "ConnectError",
    )
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            err = str(e)
            if attempt == max_retries - 1 or not any(m in err for m in transient_markers):
                raise
            wait = base_wait * (2 ** attempt)
            _safe_print(f"  ~ {what} 一時的エラー (試行 {attempt + 1}/{max_retries}) "
                        f"{wait:.1f}秒後再送: {err[:100]}")
            time.sleep(wait)


def _paginate_select(query_factory, page_size: int = 1000) -> list[dict]:
    rows = []
    offset = 0
    while True:
        rs = query_factory().range(offset, offset + page_size - 1).execute()
        if not rs.data:
            break
        rows.extend(rs.data)
        if len(rs.data) < page_size:
            break
        offset += page_size
    return rows


def fetch_target_series(supabase, redo_all: bool, series_id: str | None,
                        limit: int | None) -> list[dict]:
    def factory():
        q = supabase.table("openbd_test_series").select(
            "id, series_title, author, atmosphere_tags, genre_tags, sub_genre_tags, "
            "comedy_score, serious_score, heartwarming_score, bittersweet_score, "
            "romantic_score, uplifting_score, popularity_score, momentum_score, "
            "embedding_source"
        )
        if series_id:
            q = q.eq("id", series_id)
        elif not redo_all:
            q = q.or_(f"embedding_source.is.null,embedding_source.neq.{EMBEDDING_SOURCE_VERSION}")
        return q
    rows = _paginate_select(factory)
    if limit:
        rows = rows[:limit]
    return rows


def fetch_book_description(supabase, sid: str) -> str | None:
    """series 配下の全書籍 description を連結 (重複除外、6000字キャップ)"""
    rs = supabase.table("openbd_test_books").select(
        "description, volume_number"
    ).eq("series_id", sid).order("volume_number").execute()
    if not rs.data:
        return None
    seen: set[str] = set()
    parts: list[str] = []
    for r in rs.data:
        desc = (r.get("description") or "").strip()
        if not desc:
            continue
        key = desc[:200]
        if key in seen:
            continue
        seen.add(key)
        parts.append(desc)
    if not parts:
        return None
    return "\n".join(parts)[:6000]


def fetch_descriptions_parallel(supabase, targets: list[dict], workers: int) -> None:
    """各 series の description を並列で取得して targets[i]["_description"] にセット"""
    done_count = 0
    total = len(targets)

    def task(idx: int, sid: str) -> tuple[int, str | None]:
        try:
            return idx, fetch_book_description(supabase, sid)
        except Exception as e:
            _safe_print(f"  ! description 取得失敗 sid={sid[:8]}: {e}")
            return idx, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(task, i, s["id"]) for i, s in enumerate(targets)]
        for fut in as_completed(futs):
            idx, desc = fut.result()
            targets[idx]["_description"] = desc
            with _progress_lock:
                done_count += 1
                if done_count % 500 == 0 or done_count == total:
                    _safe_print(f"  ... description {done_count:,}/{total:,}")


def format_for_embedding(series: dict, description: str | None) -> str:
    lines = []
    lines.append(f"タイトル: {series.get('series_title') or ''}")
    lines.append(f"著者: {series.get('author') or ''}")

    atmo = series.get("atmosphere_tags") or []
    genre = series.get("genre_tags") or []
    sub = series.get("sub_genre_tags") or []
    if atmo:
        lines.append(f"雰囲気: {', '.join(atmo)}")
    if genre:
        lines.append(f"ジャンル: {', '.join(genre)}")
    if sub:
        lines.append(f"サブジャンル: {', '.join(sub)}")

    # スコアは「強み」のみを言語化して詰める (NULL や 0 に近い軸はノイズになるので省略)
    score_phrases = []
    for label, key in [
        ("コメディ", "comedy_score"), ("シリアス", "serious_score"),
        ("ほのぼの", "heartwarming_score"), ("切ない", "bittersweet_score"),
        ("胸キュン", "romantic_score"), ("高揚感", "uplifting_score"),
    ]:
        v = series.get(key)
        if v is not None and v >= 40:
            score_phrases.append(f"{label}{v}")
    if score_phrases:
        lines.append(f"雰囲気スコア: {' / '.join(score_phrases)}")

    pop = series.get("popularity_score")
    mom = series.get("momentum_score")
    if pop is not None or mom is not None:
        lines.append(f"認知度: 累計知名度={pop} 直近話題度={mom}")

    if description:
        lines.append(f"あらすじ: {description}")

    return "\n".join(lines)


def process_batch(supabase, batch_idx: int, batch_series: list[dict],
                  batch_texts: list[str]) -> tuple[int, int, int]:
    """1バッチを処理: embedding 取得 → DB 保存。戻り値: (saved_count, fail_count, tokens)"""
    try:
        resp = _openai_client.embeddings.create(
            model=settings.openai_embedding_model,
            input=batch_texts,
        )
        vectors = [d.embedding for d in resp.data]
        tokens = resp.usage.total_tokens or 0
    except Exception as e:
        _safe_print(f"  ! batch {batch_idx} embeddings 失敗: {e}")
        return 0, len(batch_series), 0

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    saved = 0
    failed = 0
    for s, vec in zip(batch_series, vectors):
        try:
            _retry_supabase(
                lambda v=vec, sid=s["id"]: supabase.table("openbd_test_series").update({
                    "embedding": v,
                    "embedding_source": EMBEDDING_SOURCE_VERSION,
                    "embedded_at": now_iso,
                }).eq("id", sid).execute(),
                what=f"embedding save sid={s['id'][:8]}",
            )
            saved += 1
        except Exception as e:
            failed += 1
            _safe_print(f"  ! 保存失敗 sid={s['id'][:8]}: {str(e)[:120]}")
    return saved, failed, tokens


def main(args):
    if not settings.openai_api_key:
        print("[エラー] OPENAI_API_KEY が .env に設定されていません")
        sys.exit(1)

    supabase = get_supabase()
    workers = max(1, args.workers)

    print(f"[対象 series 抽出] redo_all={args.redo_all} limit={args.limit} series_id={args.series_id}")
    targets = fetch_target_series(supabase, args.redo_all, args.series_id, args.limit)
    print(f"  → {len(targets):,}件")
    if not targets:
        print("対象なし。終了。")
        return

    # description を並列フェッチ
    print(f"\n[description 並列取得] workers={workers}")
    fetch_start = time.time()
    fetch_descriptions_parallel(supabase, targets, workers)
    print(f"  → {time.time() - fetch_start:.1f}秒")

    # 埋め込みテキスト整形
    print("\n[埋め込みテキスト整形]")
    embed_inputs: list[str] = []
    for s in targets:
        text = format_for_embedding(s, s.pop("_description", None))
        embed_inputs.append(text)

    if args.dry_run:
        print("\n[DRY-RUN] 整形テキストのサンプル (先頭 3件):")
        for s, t in list(zip(targets, embed_inputs))[:3]:
            print(f"\n--- [{s['id'][:8]}] {s.get('series_title')!r} ---")
            print(t)
        print(f"\n[DRY-RUN] {len(targets)} 件の embedding 計算をスキップしました。")
        return

    # バッチを並列実行
    batches: list[tuple[int, list[dict], list[str]]] = []
    for i in range(0, len(targets), BATCH_SIZE):
        batches.append((
            i // BATCH_SIZE + 1,
            targets[i:i + BATCH_SIZE],
            embed_inputs[i:i + BATCH_SIZE],
        ))
    print(f"\n[Embeddings 並列実行] model={settings.openai_embedding_model} "
          f"batches={len(batches)} batch_size={BATCH_SIZE} workers={workers}")

    total_saved = 0
    total_failed = 0
    total_tokens = 0
    started = time.time()
    completed_batches = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(process_batch, supabase, bi, bs, bt): bi
            for bi, bs, bt in batches
        }
        for fut in as_completed(futs):
            saved, failed, tokens = fut.result()
            with _progress_lock:
                total_saved += saved
                total_failed += failed
                total_tokens += tokens
                completed_batches += 1
                elapsed = time.time() - started
                rate = total_saved / elapsed if elapsed > 0 else 0
                _safe_print(
                    f"  batch {completed_batches}/{len(batches)} 完了: "
                    f"+{saved} ng={failed} 累計 {total_saved}/{len(targets)} "
                    f"{rate:.1f}件/s tokens={total_tokens:,}"
                )

    elapsed = time.time() - started
    cost = total_tokens / 1_000_000 * 0.02  # text-embedding-3-small: $0.02/1M tokens
    print(f"\n========== 完了 ==========")
    print(f"  保存: {total_saved:,}/{len(targets):,}件  失敗: {total_failed:,}件")
    print(f"  embedding 実行時間: {elapsed:.1f}秒")
    print(f"  消費 tokens: {total_tokens:,}")
    print(f"  概算コスト: ${cost:.4f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="openbd_test_series に埋め込みを付与")
    parser.add_argument("--dry-run", action="store_true", help="API は叩かず整形テキストだけ確認")
    parser.add_argument("--limit", type=int, help="件数上限")
    parser.add_argument("--redo-all", action="store_true", help="既存も含めて全件再計算")
    parser.add_argument("--series-id", type=str, help="特定 series のみ")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"並列度 (default {DEFAULT_WORKERS})")
    args = parser.parse_args()
    main(args)
