"""
Grok (web_search + x_search) でレビュー・感想を収集し、作品の「物語性詳細要約」を
Batch API で一括生成する。生成物は JSONL に永続化し、後から DB へ適用できる。

コストカット:
  - Batch API (リアルタイムより安価 / 24h SLA)
  - プロンプトキャッシング: 固定の指示文を system に置き (全リクエスト共通 = prefix 一致)、
    可変のタイトルだけ user に置くことで cached input 割引 (約 85% off) を狙う

新作除外:
  - シリーズの「最古巻 (デビュー) 発売日」が直近 --min-age-days 日以内のものは
    レビューが溜まっていないため対象外にする (デフォルト 30 日)

3 モード:
  submit : 対象 series を選び Batch 投入。状態を cache/review_batches/<name>.json に保存
  fetch  : Batch 完了を待って結果取得 → JSONL (cache/review_summaries/<name>.jsonl) に保存
  apply  : JSONL を読み OpenAI で embedding → DB (review_summary / review_embedding) へ書込

使い方:
  python -m scripts.collect_review_summaries submit --limit 100
  python -m scripts.collect_review_summaries fetch --name <batch_name>
  python -m scripts.collect_review_summaries apply --name <batch_name>            # dry-run
  python -m scripts.collect_review_summaries apply --name <batch_name> --commit   # DB 反映
"""
import sys
import os
import json
import time
import argparse
import datetime as dt
from collections import defaultdict
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.services.supabase_client import get_supabase

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BATCH_STATE_DIR = os.path.join(BASE_DIR, "cache", "review_batches")
SUMMARY_DIR = os.path.join(BASE_DIR, "cache", "review_summaries")

GROK_MODEL = "grok-4.3"
DEFAULT_MIN_AGE_DAYS = 30   # デビューがこの日数以内の新作は除外
# server-side tool 実行後に最終回答まで生成させるための上限ターン数。
# 1 だと tool 呼び出しで止まり本文が空になる (finish_reason=REASON_INVALID)。
# 大きいほど Grok が検索を繰り返しコスト増。検索ラウンド数の上限になる。
# 検証の結果、無名作品は検索2回だと情報を拾えず空要約になるため 4 を採用。
MAX_TURNS = 4

# 全リクエスト共通の固定指示。タイトルを含めないことで prefix キャッシュを効かせる。
SYSTEM_INSTRUCTIONS = """あなたはライトノベル/小説のレビュー収集・要約の専門家です。
ユーザーが指定する作品について、Web と X(旧Twitter) から読者の感想・レビュー・ブログ記事・
ツイートを積極的に収集してください。

それらの収集結果のみを根拠に、その作品が「どのような物語か」を、ベクトル検索で類似作品を
見つけやすくする目的で、詳細かつ多角的に日本語で記述してください。

含めてほしい観点 (見出しは付けず、前置きや後置きも書かず、本文のみを 600字以上で詳細に):
1. 物語の核となる設定・あらすじ
2. ジャンル・題材 (百合 / 学園 / 異世界 / 恋愛 など)
3. 全体の雰囲気・読後感 (切ない / ほのぼの / 緊張感 / 爽やか など)
4. テーマ・主題
5. 読者が繰り返し言及している魅力・特徴
6. どんな読者・どんな気分のときに刺さるか

制約:
- 検索回数はコスト節約のため最小限にする。web_search と x_search を合わせて最大4回までで
  必要な情報を集め、それ以上の追加検索はせずに回答を生成すること。
- 検索で得た情報のみで書き、推測や創作で物語の事実 (人物名・展開) を補わない。
- 収集できた感想・レビューの要素はできるだけ多く盛り込み、600字以上の情報量を確保する。
- ただし冗長な美辞麗句や繰り返しは避け、検索で得た事実・読者の声を密度高く記述する。
- 情報が極端に乏しい場合のみ無理に膨らませず、分かった範囲で書く。
- 「以下は〜」のような前置きは書かず、本文だけを出力する。"""


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
               "Bad Gateway", "502", "503", "504", "Connection reset",
               "RemoteProtocolError", "ReadTimeout", "ConnectError")
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


def select_targets(supabase, min_age_days: int, limit: int | None, redo: bool,
                   series_ids: list[str] | None = None) -> list[dict]:
    """対象 series を選ぶ。
    - is_special_edition = false
    - review_summary 未生成 (redo 指定時は無視)
    - 最古巻 (デビュー) が min_age_days 日より前 (新作除外)

    series_ids 指定時は、その series を新作/既生成チェック抜きで直接対象にする (検証用)。
    """
    print("[対象 series 抽出]")
    if series_ids:
        rows = []
        for sid in series_ids:
            rs = supabase.table("openbd_test_series").select(
                "id, series_title, author"
            ).eq("id", sid).execute()
            rows.extend(rs.data or [])
        print(f"  → series-ids 指定: {len(rows)}件")
        return rows

    series = _paginate(lambda: supabase.table("openbd_test_series").select(
        "id, series_title, author, review_summary, is_special_edition, parent_series_id"
    ))
    print(f"  全 series: {len(series):,}")

    # books から series ごとの debut (最古 published_date) を計算
    books = _paginate(lambda: supabase.table("openbd_test_books").select(
        "series_id, published_date"
    ).not_.is_("series_id", "null").not_.is_("published_date", "null"))
    debut: dict[str, str] = {}
    for b in books:
        sid = b["series_id"]
        d = b["published_date"]
        if sid not in debut or d < debut[sid]:
            debut[sid] = d

    cutoff = (dt.date.today() - dt.timedelta(days=min_age_days)).isoformat()
    print(f"  新作除外カットオフ: デビュー {cutoff} 以降は除外")

    targets = []
    skip_special = skip_child = skip_done = skip_new = skip_nodate = 0
    for s in series:
        if s.get("is_special_edition"):
            skip_special += 1
            continue
        # 子シリーズ (外伝/SS/番外編 等) は本編とベクトルが重複しやすく、
        # 汎用名では検索精度も落ちるため対象外。本編 (親) のみ調べる。
        if s.get("parent_series_id"):
            skip_child += 1
            continue
        if not redo and s.get("review_summary"):
            skip_done += 1
            continue
        d = debut.get(s["id"])
        if d is None:
            skip_nodate += 1
            continue  # 発売日不明は安全側で除外
        if d >= cutoff:
            skip_new += 1
            continue
        targets.append(s)

    print(f"  除外: 特装版 {skip_special} / 子シリーズ {skip_child} / 既生成 {skip_done} / 新作 {skip_new} / 発売日不明 {skip_nodate}")
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
    targets = select_targets(supabase, args.min_age_days, args.limit, args.redo,
                             series_ids=args.series_ids)
    if not targets:
        print("対象なし。終了。")
        return

    os.makedirs(BATCH_STATE_DIR, exist_ok=True)
    batch_name = args.name or f"review_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"

    client = Client(api_key=settings.xai_api_key)
    batch = client.batch.create(batch_name=batch_name)
    batch_id = getattr(batch, "batch_id", None) or getattr(batch, "id", None)
    print(f"[Batch 作成] name={batch_name} batch_id={batch_id}")

    chats = []
    req_map: dict[str, dict] = {}
    for i, s in enumerate(targets):
        req_id = f"req_{i:05d}"
        chat = client.chat.create(
            model=GROK_MODEL,
            tools=[web_search(), x_search()],
            max_turns=MAX_TURNS,
            batch_request_id=req_id,
        )
        chat.append(system(SYSTEM_INSTRUCTIONS))
        chat.append(user(f"作品名: {s.get('series_title') or ''}\n著者: {s.get('author') or ''}"))
        chats.append(chat)
        req_map[req_id] = {
            "series_id": s["id"],
            "series_title": s.get("series_title"),
            "author": s.get("author"),
        }

    print(f"[Batch 投入] {len(chats)}件 を add 中...")
    client.batch.add(batch_id=batch_id, batch_requests=chats)

    state = {
        "batch_name": batch_name,
        "batch_id": batch_id,
        "model": GROK_MODEL,
        "submitted_at": dt.datetime.now().isoformat(),
        "count": len(chats),
        "requests": req_map,
    }
    state_path = os.path.join(BATCH_STATE_DIR, f"{batch_name}.json")
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    print(f"[完了] 状態を保存: {state_path}")
    print(f"  → 結果取得: python -m scripts.collect_review_summaries fetch --name {batch_name}")


# ============================ fetch ============================
def _load_state(name: str) -> dict:
    path = os.path.join(BATCH_STATE_DIR, f"{name}.json")
    if not os.path.exists(path):
        print(f"[エラー] 状態ファイルが見つかりません: {path}")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


ROLE_ASSISTANT = 2  # chat_pb2.ROLE_ASSISTANT


def _extract_summary_and_sources(response) -> tuple[str, list[str], float | None]:
    """chat Response から本文 / 引用 URL / コスト USD を取り出す。

    server-side tool を使う multi-turn 応答では response.content が空になり、
    最終回答は proto.outputs の最後の ASSISTANT メッセージに入る。そこから取得する。
    """
    text = (getattr(response, "content", "") or "").strip()
    if not text:
        # multi-turn: 最後の content を持つ ASSISTANT メッセージを探す
        try:
            for out in reversed(list(response.proto.outputs)):
                msg = out.message
                if msg.role == ROLE_ASSISTANT and msg.content:
                    text = msg.content.strip()
                    break
        except Exception:
            pass

    sources: list[str] = []
    raw = getattr(response, "citations", None)
    if isinstance(raw, list):
        sources = [c for c in raw if isinstance(c, str)]

    cost = getattr(response, "cost_usd", None)
    if cost is None:
        try:
            from xai_sdk.chat import cost_usd_from_usage
            usage = getattr(response, "usage", None)
            if usage is not None:
                cost = cost_usd_from_usage(usage)
        except Exception:
            pass
    return text, sources, cost


def mode_fetch(args):
    from xai_sdk import Client
    state = _load_state(args.name)
    batch_id = state["batch_id"]
    req_map = state["requests"]
    total = state["count"]

    client = Client(api_key=settings.xai_api_key)

    # 完了をポーリング
    print(f"[Batch ポーリング] batch_id={batch_id} total={total}")
    waited = 0
    while True:
        try:
            b = client.batch.get(batch_id)
            if args.verbose:
                print(f"  [batch proto] {b}")
        except Exception as e:
            print(f"  get 失敗: {e}")
        # 完了結果を集計
        results = _collect_all_results(client, batch_id)
        done = len(results)
        print(f"  完了 {done}/{total}  (経過 {waited}s)")
        if done >= total:
            break
        if args.once:
            print("  --once 指定: 未完了のまま終了")
            break
        time.sleep(args.poll_interval)
        waited += args.poll_interval

    results = _collect_all_results(client, batch_id)
    os.makedirs(SUMMARY_DIR, exist_ok=True)
    out_path = os.path.join(SUMMARY_DIR, f"{state['batch_name']}.jsonl")

    saved = 0
    fail = 0
    total_cost = 0.0
    char_lengths = []
    with open(out_path, "w", encoding="utf-8") as f:
        for r in results:
            req_id = getattr(r, "batch_request_id", None)
            meta = req_map.get(req_id, {})
            if not getattr(r, "is_success", False):
                fail += 1
                rec = {
                    "series_id": meta.get("series_id"),
                    "series_title": meta.get("series_title"),
                    "error": getattr(r, "error_message", "unknown"),
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                continue
            summary, sources, cost = _extract_summary_and_sources(r.response)
            if cost:
                total_cost += cost
            char_lengths.append(len(summary))
            rec = {
                "series_id": meta.get("series_id"),
                "series_title": meta.get("series_title"),
                "author": meta.get("author"),
                "summary": summary,
                "sources": sources,
                "model": state["model"],
                "cost_usd": cost,
                "created_at": dt.datetime.now().isoformat(),
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            saved += 1

    avg_len = sum(char_lengths) / len(char_lengths) if char_lengths else 0
    print(f"\n========== fetch 完了 ==========")
    print(f"  成功 {saved} / 失敗 {fail}")
    print(f"  平均文字数: {avg_len:.0f}字")
    print(f"  Grok コスト合計: ${total_cost:.4f}  (1件 ${total_cost/max(saved,1):.4f})")
    print(f"  保存: {out_path}")
    print(f"  → DB 適用(dry-run): python -m scripts.collect_review_summaries apply --name {state['batch_name']}")


def _collect_all_results(client, batch_id) -> list:
    """list_batch_results を pagination_token で全ページ取得"""
    out = []
    token = None
    while True:
        resp = client.batch.list_batch_results(batch_id, pagination_token=token)
        out.extend(list(getattr(resp, "results", []) or []))
        token = getattr(resp, "pagination_token", None)
        if not token:
            break
    return out


# ============================ apply ============================
def mode_apply(args):
    from openai import OpenAI
    state = _load_state(args.name)
    jsonl_path = os.path.join(SUMMARY_DIR, f"{state['batch_name']}.jsonl")
    if not os.path.exists(jsonl_path):
        print(f"[エラー] JSONL が無い: {jsonl_path}  先に fetch してください")
        sys.exit(1)

    records = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    valid = [r for r in records if r.get("summary")]
    print(f"[apply] JSONL {len(records)}件 / 要約あり {len(valid)}件")

    if not args.commit:
        print("\n[DRY-RUN] --commit 未指定。先頭3件の要約を表示:")
        for r in valid[:3]:
            print(f"\n--- {r.get('series_title')!r} ({len(r['summary'])}字, ソース {len(r.get('sources') or [])}) ---")
            print(r["summary"][:300] + ("…" if len(r["summary"]) > 300 else ""))
        print(f"\n本適用するには --commit を付けて再実行してください。")
        return

    if not settings.openai_api_key:
        print("[エラー] OPENAI_API_KEY 未設定 (embedding 用)")
        sys.exit(1)
    openai = OpenAI(api_key=settings.openai_api_key)
    supabase = get_supabase()

    saved = 0
    total_tokens = 0
    BATCH = 100
    for i in range(0, len(valid), BATCH):
        chunk = valid[i:i + BATCH]
        texts = [r["summary"][:8000] for r in chunk]
        emb = openai.embeddings.create(model=settings.openai_embedding_model, input=texts)
        total_tokens += emb.usage.total_tokens or 0
        now_iso = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        for r, d in zip(chunk, emb.data):
            try:
                _retry_supabase(
                    lambda rr=r, vec=d.embedding: supabase.table("openbd_test_series").update({
                        "review_summary": rr["summary"],
                        "review_summary_model": rr.get("model"),
                        "review_sources": rr.get("sources"),
                        "review_embedding": vec,
                        "review_embedded_at": now_iso,
                    }).eq("id", rr["series_id"]).execute(),
                    what=f"review save {r.get('series_title','')[:20]}",
                )
                saved += 1
            except Exception as e:
                print(f"  ! 保存失敗 {r.get('series_title')}: {str(e)[:100]}")
        print(f"  applied {saved}/{len(valid)}")

    cost = total_tokens / 1_000_000 * 0.02
    print(f"\n========== apply 完了 ==========")
    print(f"  DB 反映: {saved}/{len(valid)}件")
    print(f"  embedding tokens={total_tokens:,}  概算コスト ${cost:.4f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Grok レビュー収集→要約→embedding (Batch)")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_sub = sub.add_parser("submit", help="Batch 投入")
    p_sub.add_argument("--limit", type=int, help="件数上限 (検証用)")
    p_sub.add_argument("--name", type=str, help="batch 名 (省略時は日時)")
    p_sub.add_argument("--min-age-days", type=int, default=DEFAULT_MIN_AGE_DAYS,
                       help=f"デビューがこの日数以内の新作を除外 (default {DEFAULT_MIN_AGE_DAYS})")
    p_sub.add_argument("--redo", action="store_true", help="生成済みも対象に含める")
    p_sub.add_argument("--series-ids", nargs="+", metavar="UUID",
                       help="特定 series を直接指定 (新作/既生成チェックを無視、検証用)")

    p_fetch = sub.add_parser("fetch", help="Batch 結果取得→JSONL 保存")
    p_fetch.add_argument("--name", type=str, required=True)
    p_fetch.add_argument("--poll-interval", type=int, default=60, help="ポーリング間隔 秒")
    p_fetch.add_argument("--once", action="store_true", help="1回だけ確認 (待たない)")
    p_fetch.add_argument("--verbose", action="store_true", help="batch proto を表示")

    p_apply = sub.add_parser("apply", help="JSONL→embedding→DB")
    p_apply.add_argument("--name", type=str, required=True)
    p_apply.add_argument("--commit", action="store_true", help="DB へ実際に書き込む")

    args = parser.parse_args()
    if args.mode == "submit":
        mode_submit(args)
    elif args.mode == "fetch":
        mode_fetch(args)
    elif args.mode == "apply":
        mode_apply(args)
