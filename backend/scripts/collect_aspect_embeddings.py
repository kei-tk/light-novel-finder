"""
作品を6観点に分割して Grok (Batch API + structured output) で生成し、各観点を個別 embedding する。

材料: タグ + 7軸スコア(定性表現) + 書籍あらすじ + 既存 review_summary。
検索: 原則オフ (max_turns=2)。人気作なのに素材が乏しい場合のみ最大1回 web/x_search。

6観点 (それぞれ *_text に元テキスト、*_embedding にベクトルを DB 保存):
  plot   : あらすじ・設定・舞台・ギミック
  cast   : 主人公像・キャラ配置・関係性・勢力図
  theme  : テーマ・葛藤・問題意識
  mood   : 雰囲気・読後感・感情曲線
  reader : どんな読者/気分に向くか
  review : 実際のレビュー傾向・評価/不満点 (review_aspect_text/embedding に保存)

3 モード:
  submit : 対象 series を選び Batch 投入 (structured output)。状態を json に保存
  fetch  : Batch 完了を待ち、6観点を JSONL (元テキスト) に保存
  apply  : JSONL を読み 6観点を embedding → DB の *_text / *_embedding に反映

使い方:
  python -m scripts.collect_aspect_embeddings submit --limit 50 --name aspect_50
  python -m scripts.collect_aspect_embeddings fetch --name aspect_50
  python -m scripts.collect_aspect_embeddings apply --name aspect_50            # dry-run
  python -m scripts.collect_aspect_embeddings apply --name aspect_50 --commit
"""
import sys
import os
import json
import time
import argparse
import datetime as dt
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydantic import BaseModel, Field

from app.config import settings
from app.services.supabase_client import get_supabase

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BATCH_STATE_DIR = os.path.join(BASE_DIR, "cache", "aspect_batches")
ASPECT_DIR = os.path.join(BASE_DIR, "cache", "aspect_texts")

GROK_MODEL = "grok-4.3"
MAX_TURNS = 2
ASPECT_SOURCE_VERSION = "v1_6aspect"

# 観点キー → DB の (text列, embedding列)
ASPECT_COLUMNS = {
    "plot":   ("plot_text", "plot_embedding"),
    "cast":   ("cast_text", "cast_embedding"),
    "theme":  ("theme_text", "theme_embedding"),
    "mood":   ("mood_text", "mood_embedding"),
    "reader": ("reader_text", "reader_embedding"),
    "review": ("review_aspect_text", "review_aspect_embedding"),
}
ASPECTS = list(ASPECT_COLUMNS.keys())


class AspectSplit(BaseModel):
    """作品を6観点に分割した記述 (各 150〜300字、ベクトル検索向けに密度高く)"""
    plot: str = Field(description="あらすじ・設定・舞台・ギミック")
    cast: str = Field(description="主人公像・キャラ配置・関係性・勢力図")
    theme: str = Field(description="テーマ・葛藤・問題意識")
    mood: str = Field(description="雰囲気・読後感・感情曲線")
    reader: str = Field(description="どんな読者/どんな気分に向くか")
    review: str = Field(description="実際のレビュー傾向・評価されている点/不満点")


SYSTEM_INSTRUCTIONS = """あなたはライトノベル/小説を分析し、ベクトル検索用に観点別へ構造化する専門家です。
与えられた素材 (タグ・作風の傾向・あらすじ・既存のレビュー要約) を基に、作品を6つの観点に
分けて日本語で記述してください。各観点は 150〜300字程度、類似作品をベクトル検索で見つけやすいよう
具体的かつ密度高く書きます。

観点:
- plot   : あらすじ・設定・舞台・ギミック (何がどう起きる話か)
- cast   : 主人公像・キャラ配置・関係性・勢力図
- theme  : テーマ・葛藤・問題意識
- mood   : 雰囲気・読後感・感情曲線
- reader : どんな読者/どんな気分に向くか
- review : 実際のレビュー傾向・評価されている点や不満点

重要な制約:
- 作風の傾向や知名度は「判断材料」として参照するだけにし、本文に数値やスコアを書かないでください。
  「コメディ53・切ない68」のような数値の羅列や「人気度83で〜」といった言及は禁止です。
  傾向から読み取れる作風を自然な言葉で表現してください。
- 原則として Web 検索はしないでください。与えられた素材だけで6観点を記述します。
- ただし、知名度が高い作品なのに素材が乏しく観点を埋められない場合に限り、web_search か
  x_search を「合わせて最大1回だけ」使って補ってよいです。
- 素材から分かる範囲で書き、推測で固有名詞や展開を創作しないでください。
- 前置きや見出しは書かず、各フィールドに本文だけを入れてください。"""


def _paginate(qf, ps=1000):
    rows = []
    off = 0
    while True:
        rs = qf().range(off, off + ps - 1).execute()
        if not rs.data:
            break
        rows.extend(rs.data)
        if len(rs.data) < ps:
            break
        off += ps
    return rows


def _retry_supabase(fn, *, what, max_retries=5, base_wait=1.0):
    markers = ("pseudo-header", "cloudflare", "JSON could not be generated",
               "Bad Gateway", "502", "503", "504",
               # httpx HTTP/2 接続系は文言が多様なので広めに拾う
               "Connection", "connection", "disconnect", "Terminated",
               "RemoteProtocolError", "ReadTimeout", "ReadError", "WriteError",
               "Remote end closed", "statement timeout", "57014", "timeout")
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            err = str(e)
            if attempt == max_retries - 1 or not any(m in err for m in markers):
                raise
            wait = base_wait * (2 ** attempt)
            print(f"  ~ {what} 一時エラー (試行 {attempt+1}/{max_retries}) {wait:.1f}s 後再送: {err[:100]}")
            time.sleep(wait)


def _score_band(v):
    if v is None:
        return None
    if v < 25:
        return "ほぼなし"
    if v < 45:
        return "やや弱め"
    if v < 65:
        return "中程度"
    if v < 80:
        return "強め"
    return "とても強い"


def _build_material(s: dict, descriptions: str) -> str:
    scores = []
    for label, key in [("コメディ","comedy_score"),("シリアス","serious_score"),
                       ("ほのぼの","heartwarming_score"),("切なさ","bittersweet_score"),
                       ("胸キュン","romantic_score"),("高揚感","uplifting_score")]:
        band = _score_band(s.get(key))
        if band:
            scores.append(f"{label}={band}")
    pop = _score_band(s.get("popularity_score"))
    mom = _score_band(s.get("momentum_score"))
    lines = [
        f"作品名: {s.get('series_title')}",
        f"著者: {s.get('author')}",
        f"ジャンル: {', '.join(s.get('genre_tags') or [])}",
        f"サブジャンル: {', '.join(s.get('sub_genre_tags') or [])}",
        f"雰囲気タグ: {', '.join(s.get('atmosphere_tags') or [])}",
        f"作風の傾向(参考): {' / '.join(scores)}",
        f"知名度(参考): 累計={pop or '不明'} / 直近の話題度={mom or '不明'}",
    ]
    if descriptions:
        lines.append(f"あらすじ(書誌):\n{descriptions}")
    if s.get("review_summary"):
        lines.append(f"既存レビュー要約:\n{s['review_summary']}")
    return "\n".join(lines)


def _fetch_descriptions(supabase, sid: str) -> str:
    bks = supabase.table("openbd_test_books").select("description") \
        .eq("series_id", sid).order("volume_number").execute().data or []
    seen, descs = set(), []
    for b in bks:
        d = (b.get("description") or "").strip()
        if d and d[:120] not in seen:
            seen.add(d[:120])
            descs.append(d)
    return "\n".join(descs)[:3000]


def _bulk_descriptions(supabase, target_ids: set[str]) -> dict[str, str]:
    """対象 series の全書籍 description を一括ページネーション取得 (個別クエリ回避)。"""
    from collections import defaultdict
    by_sid: dict[str, list[str]] = defaultdict(list)
    rows = _paginate(lambda: supabase.table("openbd_test_books")
                     .select("series_id, description, volume_number")
                     .not_.is_("series_id", "null").not_.is_("description", "null")
                     .order("series_id").order("volume_number"))
    seen_per: dict[str, set] = defaultdict(set)
    for r in rows:
        sid = r["series_id"]
        if sid not in target_ids:
            continue
        d = (r.get("description") or "").strip()
        if d and d[:120] not in seen_per[sid]:
            seen_per[sid].add(d[:120])
            by_sid[sid].append(d)
    return {sid: "\n".join(ds)[:3000] for sid, ds in by_sid.items()}


# 材料に必要な列だけ (ベクトル列は重いので select しない)
_MATERIAL_COLS = (
    "id, series_title, author, atmosphere_tags, genre_tags, sub_genre_tags, "
    "comedy_score, serious_score, heartwarming_score, bittersweet_score, "
    "romantic_score, uplifting_score, popularity_score, momentum_score, "
    "review_summary, parent_series_id, aspect_source"
)


def select_targets(supabase, limit, redo, series_ids):
    print("[対象 series 抽出]")
    if series_ids:
        rows = []
        for sid in series_ids:
            rs = supabase.table("openbd_test_series").select(_MATERIAL_COLS).eq("id", sid).execute()
            rows.extend(rs.data or [])
        print(f"  → series-ids 指定: {len(rows)}件")
        return rows
    # review_summary がある (= 材料が揃っている) 親シリーズで、aspect 未生成のもの
    series = _paginate(lambda: supabase.table("openbd_test_series").select(_MATERIAL_COLS)
                       .not_.is_("review_summary", "null"))
    targets = []
    skip_child = skip_done = 0
    for s in series:
        if s.get("parent_series_id"):
            skip_child += 1
            continue
        if not redo and s.get("aspect_source") == ASPECT_SOURCE_VERSION:
            skip_done += 1
            continue
        targets.append(s)
    print(f"  review_summary あり {len(series):,} / 除外: 子 {skip_child} 既生成 {skip_done}")
    print(f"  → 対象 {len(targets):,}件")
    if limit:
        targets = targets[:limit]
        print(f"  → limit 適用: {len(targets):,}件")
    return targets


# ============================ submit ============================
def mode_submit(args):
    if not settings.xai_api_key:
        print("[エラー] XAI_API_KEY 未設定")
        sys.exit(1)
    from xai_sdk import Client
    from xai_sdk.chat import system, user
    from xai_sdk.tools import web_search, x_search

    supabase = get_supabase()
    targets = select_targets(supabase, args.limit, args.redo, args.series_ids)
    if not targets:
        print("対象なし。終了。")
        return

    os.makedirs(BATCH_STATE_DIR, exist_ok=True)
    batch_name = args.name or f"aspect_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    client = Client(api_key=settings.xai_api_key)
    batch = client.batch.create(batch_name=batch_name)
    batch_id = getattr(batch, "batch_id", None) or getattr(batch, "id", None)
    print(f"[Batch 作成] name={batch_name} batch_id={batch_id}")

    # description は一括取得しておく (個別クエリだと 8000+ 回で遅く kill されやすい)
    print("[description 一括取得]")
    desc_map = _bulk_descriptions(supabase, {s["id"] for s in targets})
    print(f"  → {len(desc_map):,} series 分")

    # メモリ枯渇を避けるため CHUNK 件ずつ chat を作って add → 解放を繰り返す
    CHUNK = 400
    req_map = {}
    chats = []
    added = 0
    for i, s in enumerate(targets):
        req_id = f"req_{i:05d}"
        descs = desc_map.get(s["id"], "")
        material = _build_material(s, descs)
        chat = client.chat.create(
            model=GROK_MODEL,
            tools=[web_search(), x_search()],
            max_turns=MAX_TURNS,
            response_format=AspectSplit,
            batch_request_id=req_id,
        )
        chat.append(system(SYSTEM_INSTRUCTIONS))
        chat.append(user(material))
        chats.append(chat)
        req_map[req_id] = {"series_id": s["id"], "series_title": s.get("series_title")}
        if len(chats) >= CHUNK:
            client.batch.add(batch_id=batch_id, batch_requests=chats)
            added += len(chats)
            print(f"  add {added}/{len(targets)}")
            chats = []
    if chats:
        client.batch.add(batch_id=batch_id, batch_requests=chats)
        added += len(chats)
        print(f"  add {added}/{len(targets)}")

    state = {
        "batch_name": batch_name, "batch_id": batch_id, "model": GROK_MODEL,
        "submitted_at": dt.datetime.now().isoformat(), "count": len(req_map),
        "requests": req_map,
    }
    with open(os.path.join(BATCH_STATE_DIR, f"{batch_name}.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    print(f"[完了] → fetch --name {batch_name}")


# ============================ fetch ============================
def _load_state(name):
    path = os.path.join(BATCH_STATE_DIR, f"{name}.json")
    if not os.path.exists(path):
        print(f"[エラー] 状態ファイルなし: {path}")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


ROLE_ASSISTANT = 2


def _extract_json(response) -> str:
    """multi-turn 応答から最終 ASSISTANT メッセージ (JSON) を取り出す"""
    text = (getattr(response, "content", "") or "").strip()
    if not text:
        try:
            for out in reversed(list(response.proto.outputs)):
                msg = out.message
                if msg.role == ROLE_ASSISTANT and msg.content:
                    text = msg.content.strip()
                    break
        except Exception:
            pass
    return text


def _collect_all(client, batch_id):
    out, token = [], None
    while True:
        resp = client.batch.list_batch_results(batch_id, pagination_token=token)
        out.extend(list(getattr(resp, "results", []) or []))
        token = getattr(resp, "pagination_token", None)
        if not token:
            break
    return out


def mode_fetch(args):
    from xai_sdk import Client
    state = _load_state(args.name)
    batch_id, req_map, total = state["batch_id"], state["requests"], state["count"]
    client = Client(api_key=settings.xai_api_key)

    print(f"[Batch ポーリング] batch_id={batch_id} total={total}")
    while True:
        results = _collect_all(client, batch_id)
        print(f"  完了 {len(results)}/{total}")
        if len(results) >= total or args.once:
            break
        time.sleep(args.poll_interval)

    os.makedirs(ASPECT_DIR, exist_ok=True)
    out_path = os.path.join(ASPECT_DIR, f"{state['batch_name']}.jsonl")
    saved = fail = 0
    total_cost = 0.0
    with open(out_path, "w", encoding="utf-8") as f:
        for r in results:
            meta = req_map.get(getattr(r, "batch_request_id", None), {})
            if not getattr(r, "is_success", False):
                fail += 1
                continue
            raw = _extract_json(r.response)
            try:
                parsed = AspectSplit.model_validate_json(raw)
            except Exception as e:
                fail += 1
                print(f"  ! parse 失敗 {meta.get('series_title')}: {str(e)[:80]}")
                continue
            cost = getattr(r.response, "cost_usd", None)
            if cost:
                total_cost += cost
            rec = {"series_id": meta.get("series_id"),
                   "series_title": meta.get("series_title"),
                   **{a: getattr(parsed, a) for a in ASPECTS},
                   "cost_usd": cost, "created_at": dt.datetime.now().isoformat()}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            saved += 1

    print(f"\n========== fetch 完了 ==========")
    print(f"  成功 {saved} / 失敗 {fail}")
    print(f"  Grok コスト合計: ${total_cost:.4f}  (1件 ${total_cost/max(saved,1):.4f})")
    print(f"  保存: {out_path}")
    print(f"  → apply --name {state['batch_name']}")


# ============================ apply ============================
def mode_apply(args):
    from openai import OpenAI
    state = _load_state(args.name)
    path = os.path.join(ASPECT_DIR, f"{state['batch_name']}.jsonl")
    if not os.path.exists(path):
        print(f"[エラー] JSONL なし: {path}  先に fetch")
        sys.exit(1)
    records = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    print(f"[apply] {len(records)}件")

    if not args.commit:
        print("\n[DRY-RUN] 先頭1件の6観点:")
        r = records[0]
        for a in ASPECTS:
            print(f"\n--- {a} ({len(r.get(a) or '')}字) ---\n{(r.get(a) or '')[:200]}")
        print("\n--commit で DB 反映")
        return

    if not settings.openai_api_key:
        print("[エラー] OPENAI_API_KEY 未設定")
        sys.exit(1)
    openai = OpenAI(api_key=settings.openai_api_key)
    supabase = get_supabase()

    # resume: 既に aspect_source=現バージョン で適用済みの series はスキップ
    # (10分上限で kill されても再実行で続きから処理できる)
    if not args.redo:
        done_ids = {r["id"] for r in _paginate(
            lambda: supabase.table("openbd_test_series").select("id")
            .eq("aspect_source", ASPECT_SOURCE_VERSION))}
        before = len(records)
        records = [r for r in records if r["series_id"] not in done_ids]
        print(f"  適用済みスキップ: {before - len(records)}件 → 残り {len(records)}件")
        if not records:
            print("  全件適用済み。終了。")
            return

    from concurrent.futures import ThreadPoolExecutor, as_completed
    now_iso = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    total_tokens = 0
    saved = 0

    def _embed_records(recs: list[dict]) -> dict:
        """recs の全観点テキストを embedding。戻り値: (idx, aspect) -> vector"""
        nonlocal total_tokens
        flat = []
        for li, r in enumerate(recs):
            for a in ASPECTS:
                t = (r.get(a) or "").strip()
                if t:
                    flat.append((li, a, t))
        vmap = {}
        for i in range(0, len(flat), 128):
            ch = flat[i:i + 128]
            emb = openai.embeddings.create(model=settings.openai_embedding_model,
                                           input=[t for _, _, t in ch])
            total_tokens += emb.usage.total_tokens or 0
            for (li, a, _), d in zip(ch, emb.data):
                vmap[(li, a)] = d.embedding
        return vmap

    def _write(li: int, r: dict, vmap: dict) -> bool:
        update = {"aspect_source": ASPECT_SOURCE_VERSION, "aspect_built_at": now_iso}
        for a in ASPECTS:
            tcol, ecol = ASPECT_COLUMNS[a]
            update[tcol] = r.get(a)
            if (li, a) in vmap:
                update[ecol] = vmap[(li, a)]
        try:
            _retry_supabase(
                lambda: supabase.table("openbd_test_series").update(update).eq("id", r["series_id"]).execute(),
                what=f"aspect save {(r.get('series_title') or '')[:16]}",
            )
            return True
        except Exception as e:
            print(f"  ! 保存失敗 {r.get('series_title')}: {str(e)[:80]}", flush=True)
            return False

    # チャンク単位で embedding → 並列書き込み を繰り返す。
    # 各チャンクで DB へコミットするので、途中 kill されても resume で続行できる。
    CHUNK = 200
    for ci in range(0, len(records), CHUNK):
        recs = records[ci:ci + CHUNK]
        vmap = _embed_records(recs)
        with ThreadPoolExecutor(max_workers=16) as ex:
            futs = [ex.submit(_write, li, r, vmap) for li, r in enumerate(recs)]
            for fut in as_completed(futs):
                if fut.result():
                    saved += 1
        print(f"  applied {saved}/{len(records)}", flush=True)

    cost = total_tokens / 1_000_000 * 0.02
    print(f"\n========== apply 完了 ==========")
    print(f"  DB 反映: {saved}/{len(records)}件")
    print(f"  embedding tokens={total_tokens:,}  概算 ${cost:.4f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="6観点分割 → 個別embedding (Batch)")
    sub = parser.add_subparsers(dest="mode", required=True)

    p = sub.add_parser("submit")
    p.add_argument("--limit", type=int)
    p.add_argument("--name", type=str)
    p.add_argument("--redo", action="store_true")
    p.add_argument("--series-ids", nargs="+", metavar="UUID")

    p = sub.add_parser("fetch")
    p.add_argument("--name", type=str, required=True)
    p.add_argument("--poll-interval", type=int, default=60)
    p.add_argument("--once", action="store_true")

    p = sub.add_parser("apply")
    p.add_argument("--name", type=str, required=True)
    p.add_argument("--commit", action="store_true")
    p.add_argument("--redo", action="store_true", help="適用済みもスキップせず全件処理")

    args = parser.parse_args()
    if args.mode == "submit":
        mode_submit(args)
    elif args.mode == "fetch":
        mode_fetch(args)
    elif args.mode == "apply":
        mode_apply(args)
