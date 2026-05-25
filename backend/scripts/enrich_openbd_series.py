"""
openbd_test_series に Gemini で synopsis / tags / 7軸スコア を付与する。

1シリーズあたり 1回の Gemini API call で:
  - synopsis (未読者向けあらすじ)
  - atmosphere_tags / genre_tags / sub_genre_tags
  - target_audience
  - comedy_score / serious_score / heartwarming_score
    / bittersweet_score / romantic_score / uplifting_score / popularity_score (0-100)
を一括生成し、保存する。

AI に「シリーズ名だけ」だと別作品と取り違える可能性があるため、
シリーズに属する書籍 (タイトル + あらすじ) も渡して文脈を確定させる。

使い方:
  python -m scripts.enrich_openbd_series --dry-run --limit 3      # 動作確認
  python -m scripts.enrich_openbd_series --limit 100              # 100件
  python -m scripts.enrich_openbd_series                          # 未エンリッチ全件
  python -m scripts.enrich_openbd_series --all                    # 既存も上書き再評価
  python -m scripts.enrich_openbd_series --imprint 電撃文庫       # レーベル絞り込み
  python -m scripts.enrich_openbd_series --series-id <UUID>       # 特定シリーズ
  python -m scripts.enrich_openbd_series --series-only            # 多巻のみ
"""
import sys
import os
import time
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.gemini import (
    enrich_openbd_series as gemini_enrich,
    enrich_series_popularity as gemini_enrich_popularity,
)
from app.services.supabase_client import get_supabase

GEMINI_INTERVAL_SEC = 1   # 直列実行時の間隔 (並列化時は無関係)
MAX_BOOKS_PER_SERIES = 6  # プロンプトに含める書籍数
DEFAULT_WORKERS = 5       # 並列ワーカー数

# 更新対象カラム (Gemini レスポンスのキーと一致)
_UPDATE_FIELDS = [
    "atmosphere_tags",
    "genre_tags",
    "sub_genre_tags",
    "comedy_score",
    "serious_score",
    "heartwarming_score",
    "bittersweet_score",
    "romantic_score",
    "uplifting_score",
    "popularity_score",
    "momentum_score",
]
_SCORE_FIELDS = [
    "comedy_score", "serious_score", "heartwarming_score",
    "bittersweet_score", "romantic_score", "uplifting_score",
    "popularity_score", "momentum_score",
]


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
            print(f"  ~ {what} 一時的エラー (試行 {attempt + 1}/{max_retries}) {wait:.1f}秒後再送: {err[:120]}")
            time.sleep(wait)


def _paginate_select(query_factory, page_size: int = 1000) -> list[dict]:
    rows = []
    offset = 0
    while True:
        q = query_factory()
        rs = q.range(offset, offset + page_size - 1).execute()
        if not rs.data:
            break
        rows.extend(rs.data)
        if len(rs.data) < page_size:
            break
        offset += page_size
    return rows


def fetch_target_series(supabase, redo_all: bool, imprint: str | None,
                       series_id: str | None, series_only: bool,
                       limit: int | None) -> list[dict]:
    """エンリッチ対象の series を抽出"""
    def factory():
        q = supabase.table("openbd_test_series").select(
            "id, series_title, author, imprint, volume_count, is_series, "
            "popularity_score, is_special_edition"
        )
        if imprint:
            q = q.eq("imprint", imprint)
        if series_id:
            q = q.eq("id", series_id)
        if series_only:
            q = q.eq("is_series", True)
        # 特装版は本編から継承する想定で除外
        q = q.eq("is_special_edition", False)
        if not redo_all:
            # popularity_score または momentum_score のいずれかが NULL を未エンリッチとみなす
            # (momentum_score は新規軸なので、過去 enrich 済みでも momentum_score=NULL のものは再評価対象)
            q = q.or_("popularity_score.is.null,momentum_score.is.null")
        return q
    rows = _paginate_select(factory)
    if limit:
        rows = rows[:limit]
    return rows


def fetch_books_for_series(supabase, sid: str) -> list[dict]:
    rs = supabase.table("openbd_test_books").select(
        "isbn, title, description, volume_number, published_date"
    ).eq("series_id", sid).order("volume_number").limit(50).execute()
    return rs.data or []


def fetch_anchor_examples(supabase, max_per_band: int = 2) -> list[dict]:
    """既評価シリーズから popularity_score 帯ごとに例を取得 (Gemini calibration 用)。

    7 帯 (0-14, 15-29, 30-44, 45-59, 60-74, 75-89, 90-100) × 2件 = 最大 14件。
    Gemini プロンプトに 'reference' として渡し、判定基準を揃える。
    既存の評価が無ければ空リストを返す。
    """
    bands = [(0, 14), (15, 29), (30, 44), (45, 59), (60, 74), (75, 89), (90, 100)]
    examples: list[dict] = []
    for low, high in bands:
        try:
            rs = supabase.table("openbd_test_series").select(
                "series_title, author, imprint, volume_count, "
                "popularity_score, momentum_score, "
                "comedy_score, serious_score, heartwarming_score, "
                "bittersweet_score, romantic_score, uplifting_score"
            ).gte("popularity_score", low).lte("popularity_score", high).not_.is_(
                "momentum_score", "null"
            ).order("popularity_score", desc=True).limit(max_per_band).execute()
            examples.extend(rs.data or [])
        except Exception as e:
            print(f"  ! anchor 帯 {low}-{high} 取得失敗: {e}")
    return examples


def fetch_tag_dicts(supabase) -> tuple[dict[str, list[dict]], dict[str, list[dict]], dict[tuple[str, str], dict]]:
    """tags マスタを (公式辞書, AI生成タグ) に分けて取得。
    第3戻り値は (tag_type, name) → row dict のインデックス (usage_count 更新で使う)。
    """
    rows = supabase.table("tags").select(
        "id, name, description, tag_type, is_ai_generated, usage_count"
    ).execute().data or []
    master: dict[str, list[dict]] = {"atmosphere": [], "genre": [], "sub_genre": []}
    ai:     dict[str, list[dict]] = {"atmosphere": [], "genre": [], "sub_genre": []}
    index: dict[tuple[str, str], dict] = {}
    for r in rows:
        t = r.get("tag_type")
        if t not in master:
            continue
        item = {"name": r.get("name"), "description": r.get("description") or ""}
        if r.get("is_ai_generated"):
            ai[t].append(item)
        else:
            master[t].append(item)
        index[(t, r["name"])] = r
    return master, ai, index


_TAG_TYPE_MAP = {
    "atmosphere_tags": "atmosphere",
    "genre_tags":      "genre",
    "sub_genre_tags":  "sub_genre",
}


def register_ai_tags(supabase, enriched: dict, tag_index: dict[tuple[str, str], dict]):
    """Gemini 応答内のタグを tags マスタと照合し、新規なら AI 生成タグとして登録、
    既存 AI タグなら usage_count を +1 する。マスタ既存タグは触らない。

    enriched: Gemini レスポンス
    tag_index: (tag_type, name) → existing row dict (リクエスト時のスナップショット)
    """
    new_count = 0
    incremented = 0
    for field, tag_type in _TAG_TYPE_MAP.items():
        tags = enriched.get(field) or []
        for name in tags:
            name = (name or "").strip()
            if not name:
                continue
            existing = tag_index.get((tag_type, name))
            if existing is None:
                # 新規 → AI タグとして INSERT
                try:
                    _retry_supabase(
                        lambda: supabase.table("tags").insert({
                            "name": name,
                            "tag_type": tag_type,
                            "description": None,
                            "is_ai_generated": True,
                            "usage_count": 1,
                        }).execute(),
                        what=f"tag insert {name!r}",
                    )
                    tag_index[(tag_type, name)] = {
                        "name": name, "tag_type": tag_type,
                        "is_ai_generated": True, "usage_count": 1,
                    }
                    new_count += 1
                except Exception as e:
                    # ユニーク違反 (並行時) は無視
                    if "duplicate" not in str(e).lower() and "23505" not in str(e):
                        print(f"  ! AI タグ追加失敗 {name!r}: {e}")
            elif existing.get("is_ai_generated"):
                # 既存 AI タグ → usage_count 増加
                try:
                    new_count_val = (existing.get("usage_count") or 0) + 1
                    _retry_supabase(
                        lambda nv=new_count_val: supabase.table("tags").update({"usage_count": nv}).eq(
                            "name", name).eq("tag_type", tag_type).execute(),
                        what=f"tag usage++ {name!r}",
                    )
                    existing["usage_count"] = new_count_val
                    incremented += 1
                except Exception as e:
                    print(f"  ! usage_count 更新失敗 {name!r}: {e}")
            # マスタタグはノータッチ
    return new_count, incremented


def _coerce_score(v):
    """スコア値を 0-100 の整数 or None に正規化"""
    if v is None:
        return None
    try:
        n = int(float(v))
    except (ValueError, TypeError):
        return None
    return max(0, min(100, n))


def _build_update(enriched: dict) -> dict:
    update: dict = {}
    for key in _UPDATE_FIELDS:
        v = enriched.get(key)
        if v is None:
            continue
        if key in _SCORE_FIELDS:
            v = _coerce_score(v)
            if v is None:
                continue
        update[key] = v
    return update


def main(redo_all: bool, limit: int | None, dry_run: bool,
         imprint: str | None, series_id: str | None,
         series_only: bool, sleep_sec: float,
         model: str | None = None, use_grounding: bool = False,
         two_phase: bool = False,
         popularity_model: str | None = None,
         popularity_grounding: bool = True,
         workers: int = DEFAULT_WORKERS):
    supabase = get_supabase()
    print(f"[Gemini 設定 Phase1] model={model or '(Flash)'}  grounding={use_grounding}")
    if two_phase:
        print(f"[Gemini 設定 Phase2] model={popularity_model or '(Pro)'}  "
              f"grounding={popularity_grounding}  (popularity/momentum のみ)")
    print(f"[並列 設定] workers={workers}")

    print("[タグ辞書 取得]")
    master_tags, ai_tags, tag_index = fetch_tag_dicts(supabase)
    total_master = sum(len(v) for v in master_tags.values())
    total_ai = sum(len(v) for v in ai_tags.values())
    print(f"  → 公式 {total_master}件 "
          f"(atmosphere {len(master_tags['atmosphere'])} / "
          f"genre {len(master_tags['genre'])} / sub_genre {len(master_tags['sub_genre'])})")
    print(f"  → AI生成 {total_ai}件 "
          f"(atmosphere {len(ai_tags['atmosphere'])} / "
          f"genre {len(ai_tags['genre'])} / sub_genre {len(ai_tags['sub_genre'])})")

    # anchor (既評価シリーズの参考) を取得
    print("\n[anchor 取得 (既評価シリーズの calibration 用)]")
    score_anchors = fetch_anchor_examples(supabase, max_per_band=2)
    print(f"  → {len(score_anchors)}件")
    if score_anchors:
        for a in score_anchors[:3]:
            print(f"    例: 「{a['series_title'][:30]}」 "
                  f"pop={a.get('popularity_score')} mom={a.get('momentum_score')}")

    print("\n[対象シリーズ 抽出]")
    targets = fetch_target_series(supabase, redo_all, imprint, series_id,
                                   series_only, limit)
    print(f"  → {len(targets):,}シリーズ "
          f"({'全件上書き再評価' if redo_all else '未エンリッチのみ'})")
    if not targets:
        return

    # スレッド間共有: counters と tag_index は lock 保護
    counters = {"success": 0, "failed": 0}
    counters_lock = threading.Lock()
    tag_index_lock = threading.Lock()
    print_lock = threading.Lock()

    def _process_one(idx: int, row: dict) -> dict:
        """1 シリーズ分の処理 (Gemini 呼び出し + DB 保存 + AI タグ登録)。
        スレッド内で完結させる。"""
        sid = row["id"]
        title = row["series_title"]
        author = row.get("author")
        imp = row.get("imprint")
        vc = row.get("volume_count")

        info = {
            "idx": idx, "sid": sid, "title": title, "author": author,
            "vc": vc, "enriched": None, "error": None,
            "new_ai_tags": 0, "inc_ai_tags": 0, "phase2_rationale": None,
        }

        # 書籍取得 (Supabase はスレッドセーフ)
        books = fetch_books_for_series(supabase, sid)
        if not books:
            info["error"] = "紐付け書籍が0件"
            return info

        # Phase 1
        try:
            enriched = gemini_enrich(
                series_title=title,
                author=author,
                imprint=imp,
                volume_count=vc,
                books=books[:MAX_BOOKS_PER_SERIES],
                tag_dict=master_tags,
                ai_tag_dict=ai_tags,
                model=model,
                use_grounding=use_grounding,
                score_anchors=score_anchors,
            )
        except Exception as e:
            info["error"] = f"Phase1 Gemini 呼び出し失敗: {e}"
            return info
        if not enriched:
            info["error"] = "Phase1 Gemini レスポンス空"
            return info

        # Phase 2: popularity / momentum を Pro+grounding で再評価 (option)
        if two_phase:
            try:
                pop_result = gemini_enrich_popularity(
                    series_title=title,
                    author=author,
                    imprint=imp,
                    volume_count=vc,
                    books=books[:MAX_BOOKS_PER_SERIES],
                    model=popularity_model,
                    use_grounding=popularity_grounding,
                    score_anchors=score_anchors,
                )
            except Exception as e:
                pop_result = {}
                info["phase2_error"] = str(e)
            if pop_result:
                ps = pop_result.get("popularity_score")
                ms = pop_result.get("momentum_score")
                if ps is not None:
                    enriched["popularity_score"] = ps
                if ms is not None:
                    enriched["momentum_score"] = ms
                info["phase2_rationale"] = pop_result.get("rationale")

        update = _build_update(enriched)
        if not update:
            info["error"] = "更新フィールドなし"
            return info
        info["enriched"] = enriched

        if dry_run:
            return info

        # DB 保存 (一時的エラーは指数バックオフでリトライ)
        try:
            _retry_supabase(
                lambda: supabase.table("openbd_test_series").update(update).eq("id", sid).execute(),
                what=f"series update sid={sid[:8]}",
            )
        except Exception as e:
            info["error"] = f"保存失敗: {e}"
            return info

        # AI タグ登録 (tag_index は lock で保護)
        try:
            with tag_index_lock:
                new_n, inc_n = register_ai_tags(supabase, enriched, tag_index)
            info["new_ai_tags"] = new_n
            info["inc_ai_tags"] = inc_n
        except Exception as e:
            info["error"] = f"AI タグ管理失敗 (本体は保存済み): {e}"

        return info

    # ThreadPoolExecutor で並列実行
    total = len(targets)
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_process_one, i, row): (i, row)
                   for i, row in enumerate(targets)}
        for fut in as_completed(futures):
            i, row = futures[fut]
            try:
                info = fut.result()
            except Exception as e:
                with counters_lock:
                    counters["failed"] += 1
                with print_lock:
                    print(f"\n[?/?] 例外 idx={i}: {e}")
                continue
            with counters_lock:
                completed += 1
                if info.get("error") and not info.get("enriched"):
                    counters["failed"] += 1
                elif info.get("enriched") and not dry_run:
                    counters["success"] += 1
            # 表示
            with print_lock:
                print(f"\n[{completed}/{total}] 「{info['title'][:50]}」 "
                      f"著={info['author']} ({info['vc']}冊)")
                if info.get("error") and not info.get("enriched"):
                    print(f"  ! {info['error']}")
                    continue
                enriched = info["enriched"]
                if enriched:
                    scores_disp = " ".join(
                        f"{k.replace('_score', '')}={enriched.get(k)}"
                        for k in _SCORE_FIELDS if enriched.get(k) is not None
                    )
                    print(f"  scores: {scores_disp}")
                    atmos = enriched.get("atmosphere_tags") or []
                    genre = enriched.get("genre_tags") or []
                    sub = enriched.get("sub_genre_tags") or []
                    print(f"  tags: A={atmos} G={genre} "
                          f"S={sub[:5]}{'...' if len(sub) > 5 else ''}")
                if info.get("phase2_rationale"):
                    print(f"  Phase2 根拠: {info['phase2_rationale']}")
                if info.get("phase2_error"):
                    print(f"  ! Phase2 失敗: {info['phase2_error']}")
                if info.get("new_ai_tags") or info.get("inc_ai_tags"):
                    print(f"  AI タグ: 新規 {info['new_ai_tags']} / "
                          f"既出 +1 {info['inc_ai_tags']}")
                if info.get("error") and info.get("enriched"):
                    # 保存失敗等 (Gemini 自体は成功)
                    print(f"  ! {info['error']}")

    print(f"\n[完了] 成功 {counters['success']} / 失敗 {counters['failed']}"
          f"{'  (DRY RUN: 保存スキップ)' if dry_run else ''}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="openbd_test_series の Gemini エンリッチ")
    parser.add_argument("--all", action="store_true", help="全シリーズを再評価 (上書き)")
    parser.add_argument("--limit", type=int, default=None, help="処理件数上限")
    parser.add_argument("--dry-run", action="store_true", help="DB保存せず確認のみ")
    parser.add_argument("--imprint", default=None, help="レーベル絞り込み")
    parser.add_argument("--series-id", default=None, help="特定シリーズだけ処理 (UUID)")
    parser.add_argument("--series-only", action="store_true",
                        help="多巻シリーズのみ (is_series=true)")
    parser.add_argument("--sleep", type=float, default=GEMINI_INTERVAL_SEC,
                        help=f"Gemini call 間隔秒 (デフォルト {GEMINI_INTERVAL_SEC})")
    parser.add_argument("--model", default=None,
                        help="Phase1 Gemini モデル名 (省略時は Flash)")
    parser.add_argument("--use-grounding", action="store_true",
                        help="Phase1 で Web 検索 grounding を有効化")
    parser.add_argument("--two-phase", action="store_true",
                        help="2段階エンリッチ: Phase1=Flash でタグ/雰囲気、"
                             "Phase2=Pro+grounding で popularity/momentum 再評価")
    parser.add_argument("--popularity-model", default=None,
                        help="Phase2 Gemini モデル名 (省略時は Pro)")
    parser.add_argument("--no-popularity-grounding", action="store_true",
                        help="Phase2 の Web grounding を無効化")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"並列ワーカー数 (デフォルト {DEFAULT_WORKERS})")
    args = parser.parse_args()

    def _resolve_model(name: str | None) -> str | None:
        if name == "pro":
            from app.config import settings
            return settings.gemini_enrichment_model
        if name == "flash":
            from app.config import settings
            return settings.gemini_parse_model
        return name

    model_name = _resolve_model(args.model)
    popularity_model_name = _resolve_model(args.popularity_model)
    main(redo_all=args.all, limit=args.limit, dry_run=args.dry_run,
         imprint=args.imprint, series_id=args.series_id,
         series_only=args.series_only, sleep_sec=args.sleep,
         model=model_name, use_grounding=args.use_grounding,
         two_phase=args.two_phase,
         popularity_model=popularity_model_name,
         popularity_grounding=not args.no_popularity_grounding,
         workers=args.workers)
