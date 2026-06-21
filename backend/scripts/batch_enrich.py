"""
Batch API による全シリーズ・エンリッチのオーケストレータ (再開可能)。

設計:
  - 各ストリーム (tags / scores / momentum / detect) を、複数シリーズを
    batch_size 件ずつ 1 リクエストにまとめて Gemini Batch API に投入する。
  - 投入したジョブの情報 (job_name, リクエスト↔series_id 対応, 適用済みフラグ) を
    state ファイル (batch_state.json) に保存する。
  - ネット断・中断後も、--poll で state を読み直して未完ジョブを再ポーリングし、
    完了したものだけ DB へ反映する (Batch ジョブはサーバ側で走るので消えない)。

サブコマンド:
  submit  : 対象を抽出してバッチ投入し、state に記録 (DB はまだ書かない)
  poll    : state 内の未完ジョブを確認し、完了分を DB に反映 (繰り返し実行可)
  status  : state の一覧表示

使い方:
  python -m scripts.batch_enrich submit --stream tags --limit 4
  python -m scripts.batch_enrich poll
  python -m scripts.batch_enrich status
"""
import sys
import os
import json
import time
import argparse
import datetime as _dt
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.genai import types

from app.services.supabase_client import get_supabase
from app.services.gemini import (
    client,
    _format_books_for_prompt,
    _parse_json_object,
    detect_series_with_gemini,
    parse_detect_response,
)

STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "batch_state.json")

# 1 バッチジョブに詰める最大リクエスト数 (大きすぎ回避のため分割)
MAX_REQUESTS_PER_JOB = 500

# モデル別 料金 (USD / 1M tokens)。Batch API はトークン 50% 引き。
MODEL_PRICING = {
    "gemini-3.5-flash":        {"input": 1.50, "output": 9.00},
    "gemini-2.5-flash":        {"input": 0.30, "output": 2.50},
    "gemini-3.1-flash-lite":   {"input": 0.10, "output": 0.40},
    "gemini-3.1-pro-preview":  {"input": 1.25, "output": 5.00},
    "gemini-2.5-pro":          {"input": 1.25, "output": 5.00},
}
BATCH_TOKEN_DISCOUNT = 0.5       # Batch API のトークン割引
GROUNDING_USD_PER_REQ = 0.035    # grounding (検索) 1 リクエストあたり (割引なし想定)


def _job_cost_usd(job, model: str, grounded: bool) -> tuple[float, dict]:
    """完了ジョブの実コスト(USD)を usage_metadata から計算。(cost, 内訳) を返す。"""
    price = MODEL_PRICING.get(model, {"input": 1.0, "output": 4.0})
    dest = getattr(job, "dest", None)
    responses = getattr(dest, "inlined_responses", None) if dest else None
    in_tok = out_tok = think_tok = 0
    n_req = 0
    for resp in (responses or []):
        n_req += 1
        rr = getattr(resp, "response", None)
        um = getattr(rr, "usage_metadata", None) if rr else None
        if not um:
            continue
        in_tok += getattr(um, "prompt_token_count", 0) or 0
        out_tok += getattr(um, "candidates_token_count", 0) or 0
        think_tok += getattr(um, "thoughts_token_count", 0) or 0
    token_usd = (in_tok / 1e6 * price["input"]
                 + (out_tok + think_tok) / 1e6 * price["output"]) * BATCH_TOKEN_DISCOUNT
    ground_usd = (n_req * GROUNDING_USD_PER_REQ) if grounded else 0.0
    return token_usd + ground_usd, {
        "input": in_tok, "output": out_tok, "thinking": think_tok,
        "requests": n_req, "token_usd": token_usd, "ground_usd": ground_usd,
    }

_SAFETY = [
    types.SafetySetting(category=c, threshold="BLOCK_NONE")
    for c in ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
              "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")
]


# ===================== state 管理 =====================
def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"jobs": []}
    with open(STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


# ===================== DB 取得 =====================
# 既存(旧パイプライン由来)と新規(今回検出)の境界。created_at で安定的に判別する。
EXISTING_CUTOFF = "2026-06-14T00:00:00+00:00"


def fetch_targets(sb, limit, imprint, only_unenriched: bool = False,
                  scope: str | None = None, series_id: str | None = None) -> list[dict]:
    """scope: 'new'=今回検出分 / 'existing'=既存(05作成) / 'all'/None=全件。
    scope 指定時は created_at で安定判別 (momentum 走行で popularity が埋まっても不変)。
    series_id 指定時はその1件のみ。"""
    def factory():
        q = sb.table("openbd_test_series").select(
            "id, series_title, author, imprint, volume_count, "
            "popularity_score, momentum_score"
        ).is_("parent_series_id", "null").eq("is_special_edition", False)
        if series_id:
            q = q.eq("id", series_id)
        if imprint:
            q = q.eq("imprint", imprint)
        if scope == "existing":
            q = q.lt("created_at", EXISTING_CUTOFF)
        elif scope == "new":
            q = q.gte("created_at", EXISTING_CUTOFF)
        elif only_unenriched:
            # popularity 未設定 = 新規 series (tags/scores 未付与)
            q = q.is_("popularity_score", "null")
        # 価値の高い順 (多巻優先) → 予算到達時も重要作から処理済みになる
        return q.order("volume_count", desc=True).order("id", desc=False)
    rows, offset, page = [], 0, 1000
    while True:
        rs = factory().range(offset, offset + page - 1).execute()
        batch = rs.data or []
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return rows[:limit] if limit else rows


# 今回 run で更新された series の境界 (これより前=未再評価)
RUN_CUTOFF = "2026-06-14T12:00:00+00:00"


def fetch_gap_targets(sb, stream: str) -> list[dict]:
    """各ストリームで「データが欠落/未更新の series」だけ返す (gap-fill 用)。"""
    rows, off = [], 0
    while True:
        rs = sb.table("openbd_test_series").select(
            "id, series_title, author, imprint, volume_count, "
            "atmosphere_tags, comedy_score, momentum_score, created_at, updated_at"
        ).is_("parent_series_id", "null").eq("is_special_edition", False)\
         .order("volume_count", desc=True).order("id", desc=False)\
         .range(off, off + 999).execute().data
        rows.extend(rs)
        if len(rs) < 1000:
            break
        off += 1000
    if stream == "tags":
        return [r for r in rows if not r.get("atmosphere_tags")]
    if stream == "scores":
        return [r for r in rows if r.get("comedy_score") is None]
    if stream == "momentum":
        # momentum 未設定、または今回 run で更新されていない (旧ロジックのまま)
        return [r for r in rows
                if r.get("momentum_score") is None
                or (r.get("updated_at") or "") < RUN_CUTOFF]
    return rows


def fetch_books(sb, sid: str) -> list[dict]:
    rs = sb.table("openbd_test_books").select(
        "isbn, title, description, volume_number, published_date"
    ).eq("series_id", sid).order("volume_number").limit(50).execute()
    return rs.data or []


def fetch_tag_candidates(sb) -> dict:
    out = {}
    for t in ("atmosphere", "genre", "sub_genre"):
        rs = sb.table("tags").select("name, description").eq("tag_type", t).execute()
        out[t] = rs.data or []
    return out


# ===================== プロンプト (tags ストリーム) =====================
def _cand_block(cands: list[dict]) -> str:
    lines = []
    for it in cands:
        name, desc = it.get("name"), (it.get("description") or "").strip()
        lines.append(f"  - {name}: {desc}" if desc else f"  - {name}")
    return "\n".join(lines)


def _series_block(row: dict, books: list[dict]) -> str:
    bsec = _format_books_for_prompt(books, max_books=4, max_desc_chars=250) if books else "(書籍情報なし)"
    return (f"── series_id: {row['id']}\n"
            f"  シリーズ名: 「{row['series_title']}」\n"
            f"  著者: {row.get('author') or '?'}  レーベル: {row.get('imprint') or ''}  "
            f"既刊巻数: {row.get('volume_count') or '?'}\n"
            f"  書籍情報:\n{bsec}")


def build_tags_prompt(blocks: list[str], cands: dict) -> str:
    n = len(blocks)
    return f"""
以下の {n} 件のライトノベルシリーズについて、それぞれの atmosphere_tags / genre_tags /
sub_genre_tags を、下記の各候補リストの中から「明確に該当するもののみ」選んでください。
Web 検索も活用して、作品の正確な内容を踏まえて判定してください。

【atmosphere 候補 — この中からのみ選ぶ】
{_cand_block(cands['atmosphere'])}

【genre 候補 — この中からのみ選ぶ】
{_cand_block(cands['genre'])}

【sub_genre 候補 — この中からのみ選ぶ】
{_cand_block(cands['sub_genre'])}

【シリーズ一覧】
{chr(10).join(blocks)}

【判定ルール — 厳守】
- 各候補リストに無いタグは絶対に追加しない
- 明確に該当するものだけ (推測・連想は不可)、該当が無ければ空配列
- **必ず全 {n} 件について返すこと** (series_id 必須)

【出力 (JSON のみ、コードフェンス禁止)】
{{"series":[{{"series_id":"<UUID>","atmosphere_tags":["..."],"genre_tags":["..."],"sub_genre_tags":["..."]}}]}}
"""


# ===================== ストリーム定義 =====================
def _tags_build_requests(sb, chunks, model, grounding, ctx):
    cands = ctx["candidates"]
    reqs = []
    for chunk in chunks:
        blocks = [_series_block(r, fetch_books(sb, r["id"])) for r in chunk]
        reqs.append(types.InlinedRequest(
            model=model,
            contents=build_tags_prompt(blocks, cands),
            config=types.GenerateContentConfig(
                safety_settings=_SAFETY,
                tools=[types.Tool(google_search=types.GoogleSearch())] if grounding else None,
            ),
        ))
    return reqs


def _apply_series_response(sb, text, sid_list, updater):
    """series ストリーム共通: 応答 JSON の series_id を直接使って各行更新。
    並び順 (payload index) に依存せず、応答に含まれる series_id を信頼する
    (プロンプトに渡した実在IDをモデルがそのまま返すため)。安全のため
    リクエストした sid_list 内の id に限定する。"""
    data = _parse_json_object(text)
    items = (data or {}).get("series") or []
    applied = 0
    for it in items:
        sid = it.get("series_id")
        if not sid or not isinstance(sid, str):
            continue
        update = updater(it)
        if update:
            # 応答の series_id を直接更新 (存在しなければ 0 行で無害)
            sb.table("openbd_test_series").update(update).eq("id", sid).execute()
            applied += 1
    return applied


def _tags_apply(sb, text, sid_list, ctx):
    valid = ctx["valid"]
    def upd(it):
        return {f: [n for n in (it.get(f) or []) if n in valid[f]]
                for f in ("atmosphere_tags", "genre_tags", "sub_genre_tags")}
    return _apply_series_response(sb, text, sid_list, upd)


def _tags_ctx(sb) -> dict:
    cands = fetch_tag_candidates(sb)
    return {
        "candidates": cands,
        "valid": {f"{t}_tags": {c["name"] for c in cands[t]}
                  for t in ("atmosphere", "genre", "sub_genre")},
    }


# ===================== scores ストリーム (感情7軸) =====================
_EMO_FIELDS = ["comedy_score", "serious_score", "heartwarming_score",
               "bittersweet_score", "romantic_score", "uplifting_score"]


def _series_block_dated(row: dict, books: list[dict]) -> str:
    """発売日付きの軽量ブロック (scores/momentum 用)"""
    sb_books = sorted(books, key=lambda b: (b.get("volume_number") or 9999,
                                            b.get("published_date") or "9999"))
    lines = []
    for b in sb_books[:6]:
        vol = b.get("volume_number")
        lines.append(f"  [{'vol'+str(vol) if vol is not None else ''}] "
                     f"{(b.get('title') or '').strip()}  ({b.get('published_date') or '?'})")
    bsec = "\n".join(lines) if lines else "(書籍情報なし)"
    desc = ""
    for b in sb_books[:1]:
        d = (b.get("description") or "").strip()
        if d:
            desc = f"\n  あらすじ: {d[:300]}"
    return (f"── series_id: {row['id']}\n"
            f"  シリーズ名: 「{row['series_title']}」\n"
            f"  著者: {row.get('author') or '?'}  レーベル: {row.get('imprint') or ''}  "
            f"既刊巻数: {row.get('volume_count') or '?'}\n"
            f"  書籍:\n{bsec}{desc}")


def build_scores_prompt(blocks: list[str]) -> str:
    n = len(blocks)
    return f"""
以下の {n} 件のライトノベルシリーズについて、作品の雰囲気を 6 軸の感情スコア
(各 0-100) で評価してください。Web 検索であらすじ・感想も参照してよい。

【6 軸の定義】
- comedy_score      : 笑い・コメディ要素の強さ
- serious_score     : シリアス・重厚さ
- heartwarming_score: ほのぼの・心温まる度合い
- bittersweet_score : 切なさ・ほろ苦さ
- romantic_score    : 恋愛・ロマンスの強さ
- uplifting_score   : 爽快・前向き・高揚感

【ルール】
- 各軸は独立。複数軸が同時に高くてよい
- **5/10 刻みに丸めず 1 刻み**で微差を付ける (例 67, 43)
- 判断不能なら null
- **必ず全 {n} 件返す** (series_id 必須)、整数

【出力 (JSON のみ)】
{{"series":[{{"series_id":"<UUID>","comedy_score":0,"serious_score":0,"heartwarming_score":0,"bittersweet_score":0,"romantic_score":0,"uplifting_score":0}}]}}

【シリーズ一覧】
{chr(10).join(blocks)}
"""


def _scores_build_requests(sb, chunks, model, grounding, ctx):
    reqs = []
    for chunk in chunks:
        blocks = [_series_block_dated(r, fetch_books(sb, r["id"])) for r in chunk]
        reqs.append(types.InlinedRequest(
            model=model,
            contents=build_scores_prompt(blocks),
            config=types.GenerateContentConfig(
                safety_settings=_SAFETY,
                tools=[types.Tool(google_search=types.GoogleSearch())] if grounding else None,
            ),
        ))
    return reqs


def _scores_apply(sb, text, sid_list, ctx):
    def upd(it):
        u = {}
        for f in _EMO_FIELDS:
            v = it.get(f)
            if isinstance(v, (int, float)):
                u[f] = int(v)
        return u
    return _apply_series_response(sb, text, sid_list, upd)


# ===================== momentum ストリーム (popularity/momentum/reason) =====================
def build_momentum_prompt(blocks: list[str]) -> str:
    n = len(blocks)
    return f"""
以下の {n} 件のライトノベルシリーズについて、Web 検索を活用して
人気度2軸と話題の根拠注釈を評価してください。

【popularity_score: 累計知名度 (0-100)】
作品の歴史的・累積的な認知度。アニメ化/コミカライズ/長期連載/累計部数/受賞歴/一般認知。
基準: 95=超メジャー(SAO等) / 75=人気・アニメ化済 / 55=中堅 / 35=マイナー / 15=ほぼ無名 / 0=情報なし

【momentum_score: 直近の話題度 (0-100) — popularity に対する相対的な話題増分】
**最重要: popularity が高いほど momentum を高くする条件を厳しくする。**
- popularity 高 (60+): 「有名だから」では上げない。新規アニメ化/コミカライズ新刊/
  セール実施中/新刊の大反響/重版好調 等の具体的話題がある時のみ高くする。
  **受賞(新人賞/大賞/年間ランキング/このラノ等)は人気作でも正当な理由として認める。**
  具体的話題が無ければ有名でも 30 以下に抑える。
- popularity 低 (40以下): X でバズった/売れ行き良好/受賞/新刊好評 等の小さな信号でも
  momentum を高め(70-90)に付けてよい。
基準: 90=明確な大型話題が今ある / 70=具体的直近話題あり(セール/受賞等) / 45=現役感のみ /
      25=目立つ話題なし / 0=忘れられている

【momentum_reason: 話題の根拠注釈】
momentum を高め(60+)に付けた場合、具体的根拠を短く記述
(例「2026-07-31まで電子50%OFFセール」「劇場アニメ化決定」「○○大賞2025受賞」)。
日付・賞名等あれば含める。話題が無ければ null。Web で裏が取れない憶測は書かない。

【ルール】2軸は 1 刻みで微差。判断不能は null。**必ず全 {n} 件返す** (series_id 必須)。

【出力 (JSON のみ)】
{{"series":[{{"series_id":"<UUID>","popularity_score":0,"momentum_score":0,"momentum_reason":null}}]}}

【シリーズ一覧】
{chr(10).join(blocks)}
"""


def _momentum_build_requests(sb, chunks, model, grounding, ctx):
    reqs = []
    for chunk in chunks:
        blocks = [_series_block_dated(r, fetch_books(sb, r["id"])) for r in chunk]
        reqs.append(types.InlinedRequest(
            model=model,
            contents=build_momentum_prompt(blocks),
            config=types.GenerateContentConfig(
                safety_settings=_SAFETY,
                tools=[types.Tool(google_search=types.GoogleSearch())] if grounding else None,
            ),
        ))
    return reqs


def _momentum_apply(sb, text, sid_list, ctx):
    def upd(it):
        u = {}
        for f in ("popularity_score", "momentum_score"):
            v = it.get(f)
            if isinstance(v, (int, float)):
                u[f] = int(v)
        reason = it.get("momentum_reason")
        u["momentum_reason"] = reason if isinstance(reason, str) and reason.strip() else None
        return u
    return _apply_series_response(sb, text, sid_list, upd)


# ===================== detect ストリーム (シリーズ検出, batch1, grounding無) =====================
DETECT_MODEL = "gemini-2.5-flash"


def fetch_books_by_isbns(sb, isbns: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(isbns), 200):
        chunk = isbns[i:i + 200]
        rs = sb.table("openbd_test_books").select(
            "isbn, title, author, illustrator, contributors, "
            "published_date, description, series_id, attached_to_isbn"
        ).in_("isbn", chunk).execute()
        out.extend(rs.data or [])
    return out


def _detect_apply(sb, text, isbn_list, ctx):
    """1 クラスタの分類応答を {groups, standalone} としてパースし series 作成。"""
    from scripts.gemini_detect_series import (
        _save_group, _save_standalone, _primary_author_for_cluster,
    )
    data = parse_detect_response(text)
    books = fetch_books_by_isbns(sb, isbn_list)
    book_index = {b["isbn"]: b for b in books if b.get("isbn")}
    cbooks = list(book_index.values())
    if not cbooks:
        return 0
    author = _primary_author_for_cluster(cbooks)
    groups = data.get("groups") or []
    standalone = data.get("standalone") or []
    # 取りこぼし ISBN は standalone fallback (NULL series_id 防止)
    covered = set()
    for g in groups:
        for bk in (g.get("books") or []):
            if bk.get("isbn"):
                covered.add(bk["isbn"])
    for s in standalone:
        if s.get("isbn"):
            covered.add(s["isbn"])
    for isbn in set(isbn_list) - covered:
        standalone.append({"isbn": isbn, "reason": "Gemini response missed"})
    applied = 0
    for g in groups:
        summ = _save_group(sb, author, g, book_index, False)
        applied += summ.get("saved_main", 0) + summ.get("saved_sub", 0)
    applied += _save_standalone(sb, author, standalone, book_index, False)
    return applied


def submit_detect(args):
    """検出専用 submit: 本クラスタリング → 1グループ/リクエスト (batch1) で投入。
    1冊クラスタは Gemini 不要なので即 standalone 保存する。"""
    from scripts.gemini_detect_series import (
        fetch_all_books, cluster_books_by_contributors, _split_megacluster,
        _book_is_processed, _shared_names_in_cluster, _save_standalone,
        _primary_author_for_cluster, MAX_BOOKS_PER_CLUSTER,
    )
    sb = get_supabase()
    model = args.model or DETECT_MODEL
    books = fetch_all_books(sb)
    book_index = {b["isbn"]: b for b in books if b.get("isbn")}
    clusters = cluster_books_by_contributors(books)
    # 孤児 (series_id 未設定) を含むクラスタのみ対象
    clusters = [c for c in clusters if any(not _book_is_processed(b) for b in c)]
    # megacluster 分割
    expanded = []
    for c in clusters:
        expanded.extend(_split_megacluster(c, MAX_BOOKS_PER_CLUSTER))
    clusters = sorted(expanded, key=lambda c: -len(c))
    if args.limit:
        clusters = clusters[:args.limit]
    print(f"[submit detect] model={model} 対象クラスタ={len(clusters):,}")

    # 1冊クラスタは即 standalone (Gemini 不要)
    singles = [c for c in clusters if len(c) == 1]
    multi = [c for c in clusters if len(c) >= 2]
    s_saved = 0
    for c in singles:
        author = _primary_author_for_cluster(c)
        s_saved += _save_standalone(
            sb, author, [{"isbn": c[0].get("isbn"), "reason": "singleton"}],
            book_index, False)
    print(f"  → 1冊クラスタ {len(singles):,}件を standalone 即保存 ({s_saved})")
    if not multi:
        print("  → batch対象(複数冊)クラスタなし")
        return

    state = load_state()
    for jstart in range(0, len(multi), MAX_REQUESTS_PER_JOB):
        jclusters = multi[jstart:jstart + MAX_REQUESTS_PER_JOB]
        reqs = []
        rmap = []
        for c in jclusters:
            shared = _shared_names_in_cluster(c)
            imp = _most_common([b.get("imprint") for b in c]) if hasattr(c, "__iter__") else None
            prompt = detect_series_with_gemini(
                books=c, shared_names=shared, imprint_hint=imp,
                model=model, use_grounding=False, build_prompt_only=True)
            reqs.append(types.InlinedRequest(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(safety_settings=_SAFETY)))
            rmap.append([b.get("isbn") for b in c if b.get("isbn")])
        job = client.batches.create(model=model, src=reqs)
        state["jobs"].append({
            "stream": "detect", "model": model, "job_name": job.name,
            "batch_size": 1, "request_map": rmap, "status": "pending",
            "submitted_at": _now(), "applied": 0,
        })
        save_state(state)
        print(f"  ✓ 投入 job={job.name} ({len(reqs)}リクエスト) state={job.state}")


def _most_common(values):
    from collections import Counter
    vals = [v for v in values if v]
    return Counter(vals).most_common(1)[0][0] if vals else None


# ===================== genre ストリーム (genre_tags のみ再評価) =====================
def build_genre_prompt(blocks: list[str], candidates: list[dict]) -> str:
    lines = []
    for it in candidates:
        name, desc = it.get("name"), (it.get("description") or "").strip()
        lines.append(f"  - {name}: {desc}" if desc else f"  - {name}")
    n = len(blocks)
    return f"""
以下の {n} 件のライトノベルシリーズについて、それぞれの genre_tags を、
下記候補リストの中から「明確に該当するもののみ」選んでください。
Web 検索とあらすじを活用して作品の正確なジャンルを判定してください
(例: 女性同士の恋愛が主軸なら「百合」、男性同士なら「BL」を見落とさない)。

【ジャンル候補 — この中からのみ選ぶ】
{chr(10).join(lines)}

【シリーズ一覧】
{chr(10).join(blocks)}

【ルール】候補外は選ばない / 明確に該当するものだけ / 無ければ空配列 / **必ず全 {n} 件返す** (series_id 必須)

【出力 (JSON のみ)】
{{"series":[{{"series_id":"<UUID>","genre_tags":["..."]}}]}}
"""


def _genre_ctx(sb) -> dict:
    cands = fetch_tag_candidates(sb)["genre"]
    return {"candidates": cands, "valid": {c["name"] for c in cands}}


def _genre_build_requests(sb, chunks, model, grounding, ctx):
    reqs = []
    for chunk in chunks:
        blocks = [_series_block(r, fetch_books(sb, r["id"])) for r in chunk]
        reqs.append(types.InlinedRequest(
            model=model, contents=build_genre_prompt(blocks, ctx["candidates"]),
            config=types.GenerateContentConfig(
                safety_settings=_SAFETY,
                tools=[types.Tool(google_search=types.GoogleSearch())] if grounding else None)))
    return reqs


def _genre_apply(sb, text, sid_list, ctx):
    valid = ctx["valid"]
    def upd(it):
        return {"genre_tags": [n for n in (it.get("genre_tags") or []) if n in valid]}
    return _apply_series_response(sb, text, sid_list, upd)


STREAMS = {
    "tags": {
        "model": "gemini-3.5-flash", "grounding": True,
        "build_requests": _tags_build_requests, "apply": _tags_apply,
        "make_ctx": _tags_ctx,
    },
    "genre": {
        # grounding ON だと Batch 応答に function_call が混じり JSON が壊れる。
        # genre/百合 はあらすじから判定でき grounding 不要なので OFF。
        "model": "gemini-3.5-flash", "grounding": False,
        "build_requests": _genre_build_requests, "apply": _genre_apply,
        "make_ctx": _genre_ctx,
    },
    "scores": {
        "model": "gemini-2.5-flash", "grounding": False,
        "build_requests": _scores_build_requests, "apply": _scores_apply,
        "make_ctx": lambda sb: {},
    },
    "momentum": {
        "model": "gemini-3.1-pro-preview", "grounding": True,
        "build_requests": _momentum_build_requests, "apply": _momentum_apply,
        "make_ctx": lambda sb: {},
    },
    "detect": {
        "model": DETECT_MODEL, "grounding": False,
        "apply": _detect_apply, "make_ctx": lambda sb: {},
    },
}


# ===================== submit =====================
def cmd_submit(args):
    stream = args.stream
    if stream == "detect":
        submit_detect(args)
        return
    sb = get_supabase()
    if stream not in STREAMS:
        print(f"未対応ストリーム: {stream} (対応: {list(STREAMS)})")
        return
    sdef = STREAMS[stream]
    model = args.model or sdef["model"]
    grounding = sdef["grounding"]
    bs = args.batch_size

    targets = fetch_targets(sb, args.limit, args.imprint,
                            only_unenriched=args.only_unenriched,
                            series_id=getattr(args, "series_id", None))
    if not targets:
        print("対象シリーズなし")
        return
    print(f"[submit] stream={stream} model={model} grounding={grounding} "
          f"batch_size={bs} 対象={len(targets):,}件")

    ctx = sdef["make_ctx"](sb)
    chunks = [targets[i:i + bs] for i in range(0, len(targets), bs)]

    state = load_state()
    # MAX_REQUESTS_PER_JOB 単位でジョブ分割
    for jstart in range(0, len(chunks), MAX_REQUESTS_PER_JOB):
        jchunks = chunks[jstart:jstart + MAX_REQUESTS_PER_JOB]
        print(f"  ジョブ構築中: リクエスト {len(jchunks)} 件 ...")
        reqs = sdef["build_requests"](sb, jchunks, model, grounding, ctx)
        job = client.batches.create(model=model, src=reqs)
        state["jobs"].append({
            "stream": stream,
            "model": model,
            "job_name": job.name,
            "batch_size": bs,
            "request_map": [[r["id"] for r in ch] for ch in jchunks],
            "status": "pending",
            "submitted_at": _now(),
            "applied": 0,
        })
        save_state(state)
        print(f"  ✓ 投入 job={job.name}  state={job.state}")
    print(f"[submit] 完了。poll で完了を待って反映してください")


# ===================== poll =====================
def cmd_poll(args):
    sb = get_supabase()
    state = load_state()
    pending = [j for j in state["jobs"] if j["status"] not in ("applied", "failed")]
    if not pending:
        print("未完ジョブなし")
        return
    print(f"[poll] 未完 {len(pending)} ジョブを確認")
    for j in pending:
        try:
            job = client.batches.get(name=j["job_name"])
        except Exception as e:
            print(f"  ! {j['job_name']} get失敗: {e}")
            continue
        st = str(job.state)
        print(f"  {j['stream']} {j['job_name'][:40]} → {st}")
        if "SUCCEEDED" not in st:
            if "FAILED" in st or "CANCELLED" in st or "EXPIRED" in st:
                j["status"] = "failed"
                save_state(state)
            continue
        # 成功 → 結果適用
        dest = getattr(job, "dest", None)
        responses = getattr(dest, "inlined_responses", None) if dest else None
        if not responses:
            print(f"    ! inlined_responses 空")
            continue
        sdef = STREAMS[j["stream"]]
        ctx = sdef["make_ctx"](sb)
        rmap = j["request_map"]
        total_applied = 0
        for ridx, resp in enumerate(responses):
            if ridx >= len(rmap):
                break
            payload = rmap[ridx]
            rr = getattr(resp, "response", None)
            text = (getattr(rr, "text", None) or "") if rr else ""
            try:
                total_applied += sdef["apply"](sb, text, payload, ctx)
            except Exception as e:
                print(f"    ! request {ridx} apply失敗: {str(e)[:150]}")
        j["status"] = "applied"
        j["applied"] = total_applied
        save_state(state)
        print(f"    ✓ {total_applied} シリーズ反映")
    print("[poll] 完了")


def cmd_status(args):
    state = load_state()
    spent = state.get("spent_usd", 0.0)
    print(f"累計コスト: ${spent:.2f} (¥{spent*getattr(args,'usd_jpy',155):,.0f})  "
          f"進捗={state.get('progress',{})}")
    if not state["jobs"]:
        print("ジョブなし")
        return
    print(f"{'stream':<10}{'status':<10}{'applied':>8}  job")
    for j in state["jobs"]:
        print(f"{j['stream']:<10}{j['status']:<10}{j.get('applied',0):>8}  {j['job_name']}")


def _wait_job(job_name: str, poll_sec: int = 20):
    """ジョブ完了まで待機。完了 job を返す。失敗時は None。"""
    while True:
        time.sleep(poll_sec)
        job = client.batches.get(name=job_name)
        st = str(job.state)
        if "SUCCEEDED" in st:
            return job
        if any(x in st for x in ("FAILED", "CANCELLED", "EXPIRED")):
            print(f"    ! ジョブ失敗: {st}")
            return None


def _apply_job(sb, job, sdef, payloads, ctx) -> int:
    dest = getattr(job, "dest", None)
    responses = getattr(dest, "inlined_responses", None) if dest else None
    applied = 0
    for ridx, resp in enumerate(responses or []):
        if ridx >= len(payloads):
            break
        rr = getattr(resp, "response", None)
        text = (getattr(rr, "text", None) or "") if rr else ""
        try:
            applied += sdef["apply"](sb, text, payloads[ridx], ctx)
        except Exception as e:
            print(f"    ! request {ridx} apply失敗: {str(e)[:120]}")
    return applied


def cmd_run(args):
    """予算ガード付き一括実行。ジョブ単位で実コストを集計し上限超過で中断。
    progress カーソルと in-flight ジョブ追跡で再開可能。"""
    sb = get_supabase()
    usd_jpy = args.usd_jpy
    budget_jpy = args.budget_jpy
    bs = args.batch_size
    streams = args.streams.split(",") if args.streams else ["tags", "scores", "momentum"]

    state = load_state()
    spent = state.get("spent_usd", 0.0)
    progress = state.setdefault("progress", {})
    inflight = state.get("inflight")

    fill_gaps = getattr(args, "fill_gaps", False)
    scope = getattr(args, "scope", None)
    if fill_gaps:
        def stream_targets(stream):
            return fetch_gap_targets(sb, stream)
        print(f"[run] GAP-FILL モード  streams={streams}  batch_size={bs}")
    elif scope:
        # scope 明示時は全ストリームをその範囲に (例: --scope existing で既存タグ再エンリッチ)
        targets_scoped = fetch_targets(sb, args.limit, args.imprint, scope=scope)
        def stream_targets(stream):
            return targets_scoped
        print(f"[run] scope={scope} 対象 {len(targets_scoped):,}件  "
              f"streams={streams}  batch_size={bs}")
    else:
        # 既定: tags/scores は新規 series のみ、momentum は全件 (新ロジック再評価)
        targets_new = fetch_targets(sb, args.limit, args.imprint, only_unenriched=True)
        targets_all = fetch_targets(sb, args.limit, args.imprint, only_unenriched=False)
        def stream_targets(stream):
            return targets_new if stream in ("tags", "scores") else targets_all
        print(f"[run] 新規{len(targets_new):,}件 / 全{len(targets_all):,}件  "
              f"streams={streams}  batch_size={bs}")
    print(f"[run] 予算上限 ¥{budget_jpy:,}  既消化 ¥{spent*usd_jpy:,.0f} (${spent:.2f})  "
          f"進捗={progress}")

    def over_budget():
        return spent * usd_jpy >= budget_jpy

    # 中断していた in-flight ジョブをまず回収
    if inflight:
        print(f"[run] in-flight ジョブ回収: {inflight['stream']} {inflight['job_name'][:40]}")
        sdef = STREAMS[inflight["stream"]]
        ctx = sdef["make_ctx"](sb)
        job = _wait_job(inflight["job_name"])
        if job:
            cost, _ = _job_cost_usd(job, sdef["model"], sdef["grounding"])
            spent += cost
            payloads = inflight["payloads"]
            n = _apply_job(sb, job, sdef, payloads, ctx)
            progress[inflight.get("pkey", inflight["stream"])] = inflight["next_cursor"]
            print(f"  ✓ {n}反映  ${cost:.2f}  累計¥{spent*usd_jpy:,.0f}")
        state["inflight"] = None
        state["spent_usd"] = spent
        save_state(state)

    for stream in streams:
        sdef = STREAMS[stream]
        model, grounding = sdef["model"], sdef["grounding"]
        ctx = sdef["make_ctx"](sb)
        pkey = f"{stream}_gap" if fill_gaps else stream
        targets = stream_targets(stream)
        chunks = [targets[i:i + bs] for i in range(0, len(targets), bs)]
        ci = progress.get(pkey, 0)
        if ci >= len(chunks):
            print(f"[run] {stream}: 完了済みスキップ ({len(targets)}件)")
            continue
        print(f"[run] {stream}: gap対象 {len(targets):,}件" if fill_gaps else "")
        print(f"[run] === {stream} 開始 (chunk {ci}/{len(chunks)}) model={model} grounding={grounding} ===")
        while ci < len(chunks):
            if over_budget():
                print(f"[run] !! 予算上限 ¥{budget_jpy:,} 到達 (消化 ¥{spent*usd_jpy:,.0f}) → 中断")
                save_state(state)
                return
            jchunks = chunks[ci:ci + MAX_REQUESTS_PER_JOB]
            payloads = [[r["id"] for r in ch] for ch in jchunks]
            reqs = sdef["build_requests"](sb, jchunks, model, grounding, ctx)
            job = client.batches.create(model=model, src=reqs)
            next_cursor = ci + len(jchunks)
            # in-flight 記録 (apply 前に中断しても再開できる)
            state["inflight"] = {"stream": stream, "pkey": pkey, "job_name": job.name,
                                 "payloads": payloads, "next_cursor": next_cursor}
            save_state(state)
            print(f"  [{stream}] job投入 {len(reqs)}req (chunk {ci}~{next_cursor}) 完了待ち...")
            job = _wait_job(job.name)
            if job is None:
                save_state(state)
                return
            cost, brk = _job_cost_usd(job, model, grounding)
            spent += cost
            n = _apply_job(sb, job, sdef, payloads, ctx)
            ci = next_cursor
            progress[pkey] = ci
            state["inflight"] = None
            state["spent_usd"] = spent
            save_state(state)
            print(f"  [{stream}] +{n}反映  job代 ${cost:.2f}(¥{cost*usd_jpy:,.0f}) "
                  f"[token${brk['token_usd']:.2f}/ground${brk['ground_usd']:.2f}]  "
                  f"累計 ¥{spent*usd_jpy:,.0f}")
    print(f"[run] 全完了  総コスト ¥{spent*usd_jpy:,.0f} (${spent:.2f})")


def main():
    ap = argparse.ArgumentParser(description="Batch API エンリッチ オーケストレータ")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("submit")
    s.add_argument("--stream", required=True)
    s.add_argument("--limit", type=int, default=None)
    s.add_argument("--imprint", default=None)
    s.add_argument("--series-id", default=None, help="特定シリーズ1件だけ処理")
    s.add_argument("--batch-size", type=int, default=2)
    s.add_argument("--model", default=None)
    s.add_argument("--only-unenriched", action="store_true")
    s.set_defaults(func=cmd_submit)

    p = sub.add_parser("poll")
    p.set_defaults(func=cmd_poll)

    st = sub.add_parser("status")
    st.add_argument("--usd-jpy", type=float, default=155)
    st.set_defaults(func=cmd_status)

    r = sub.add_parser("run")
    r.add_argument("--streams", default=None,
                   help="カンマ区切り (省略時 tags,scores,momentum)")
    r.add_argument("--batch-size", type=int, default=3)
    r.add_argument("--limit", type=int, default=None)
    r.add_argument("--imprint", default=None)
    r.add_argument("--budget-jpy", type=float, default=50000)
    r.add_argument("--usd-jpy", type=float, default=155)
    r.add_argument("--scope", choices=["new", "existing", "all"], default=None,
                   help="全ストリームの対象を created_at で固定 "
                        "(existing=既存タグ再エンリッチ用)。省略時は tags/scores=新規・momentum=全件")
    r.add_argument("--fill-gaps", action="store_true",
                   help="欠落/未更新の series だけを対象に再実行 (gap-fill)")
    r.set_defaults(func=cmd_run)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
