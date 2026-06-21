"""
特定タグを全シリーズに対して「付与判定」する機能 (Batch API)。

指定した1つのタグ (atmosphere / genre / sub_genre のいずれか) について、
全シリーズを Batch でまとめて Gemini に「該当する/しない」を判定させ、
該当すれば付与、--remove 指定時は非該当なら除去する。

特徴:
  - tag_type は tags テーブルから自動解決
  - あらすじ (description) を根拠に判定 (grounding は任意)
  - batch-size 件を 1 リクエストにまとめ、MAX_REQUESTS_PER_JOB 単位でジョブ化
  - 進捗カーソル + 予算上限で再開可能、全件ログ + コスト集計

使い方:
  python -m scripts.batch_tag_assign --tag 百合 [--batch-size 20] [--model gemini-2.5-flash]
      [--grounding] [--remove] [--scope all|missing] [--dry-run] [--budget-jpy 30000]
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
from app.services.gemini import client, _parse_json_object
from scripts.batch_enrich import (
    _SAFETY, _wait_job, _job_cost_usd, MAX_REQUESTS_PER_JOB, fetch_books,
)

STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "batch_tagassign_state.json")

_TYPE_TO_COL = {
    "atmosphere": "atmosphere_tags",
    "genre": "genre_tags",
    "sub_genre": "sub_genre_tags",
}


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(s: dict):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def resolve_tag(sb, tag: str) -> tuple[str, str]:
    """タグ名から (tag_type, description) を解決。"""
    rs = sb.table("tags").select("tag_type, description").eq("name", tag).execute().data
    if not rs:
        raise SystemExit(f"タグ '{tag}' が tags テーブルに存在しません")
    return rs[0]["tag_type"], (rs[0].get("description") or "")


def fetch_targets(sb, col: str, tag: str, scope: str, limit: int | None) -> list[dict]:
    rows, off = [], 0
    while True:
        q = sb.table("openbd_test_series").select(
            f"id, series_title, author, genre_tags, description, {col}"
        ).is_("parent_series_id", "null").eq("is_special_edition", False)\
         .order("volume_count", desc=True).order("id", desc=False)
        rs = q.range(off, off + 999).execute().data
        rows.extend(rs)
        if len(rs) < 1000:
            break
        off += 1000
    if scope == "missing":  # まだそのタグを持たない series のみ
        rows = [r for r in rows if tag not in (r.get(col) or [])]
    return rows[:limit] if limit else rows


def build_prompt(tag: str, desc: str, tag_type: str, chunk: list[dict]) -> str:
    blocks = []
    for r in chunk:
        d = (r.get("description") or "").strip().replace("\n", " ")[:400]
        g = ", ".join(r.get("genre_tags") or [])
        blocks.append(
            f"── series_id: {r['id']}\n"
            f"  「{r['series_title']}」 著: {r.get('author') or '?'}  ジャンル: {g}\n"
            f"  あらすじ: {d or '(不明)'}")
    body = "\n\n".join(blocks)
    desc_line = f"（説明: {desc}）" if desc else ""
    return f"""
以下の {len(chunk)} 件のライトノベルについて、{tag_type} タグ「{tag}」{desc_line}が
各作品に**明確に該当するか**を判定してください。あらすじを根拠に、明確に当てはまる場合のみ true。
推測・なんとなくは false。

【作品一覧】
{body}

【出力 (JSON のみ、全 {len(chunk)} 件)】
{{"series":[{{"series_id":"<UUID>","applies":true}}]}}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--batch-size", type=int, default=20)
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--grounding", action="store_true")
    ap.add_argument("--remove", action="store_true", help="非該当なら既存タグを除去")
    ap.add_argument("--scope", choices=["all", "missing"], default="all")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--budget-jpy", type=float, default=30000)
    ap.add_argument("--usd-jpy", type=float, default=155)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sb = get_supabase()
    tag_type, desc = resolve_tag(sb, args.tag)
    col = _TYPE_TO_COL[tag_type]
    print(f"[タグ判定] tag='{args.tag}' type={tag_type} col={col} "
          f"model={args.model} grounding={args.grounding} batch={args.batch_size} "
          f"remove={args.remove} scope={args.scope}")

    targets = fetch_targets(sb, col, args.tag, args.scope, args.limit)
    chunks = [targets[i:i + args.batch_size] for i in range(0, len(targets), args.batch_size)]
    print(f"[対象] {len(targets):,} シリーズ / {len(chunks):,} チャンク")

    state = load_state()
    skey = f"{args.tag}/{args.scope}"
    spent = state.get("spent_usd", 0.0)
    ci = state.get("progress", {}).get(skey, 0)
    counters = state.get("counters", {}).get(skey, {"added": 0, "removed": 0, "nochange": 0, "miss": 0})
    if ci:
        print(f"[再開] chunk {ci}/{len(chunks)} から / 既消化 ¥{spent*args.usd_jpy:,.0f}")

    while ci < len(chunks):
        if spent * args.usd_jpy >= args.budget_jpy:
            print(f"[中断] 予算上限 ¥{args.budget_jpy:,.0f} 到達 (¥{spent*args.usd_jpy:,.0f})")
            break
        jchunks = chunks[ci:ci + MAX_REQUESTS_PER_JOB]
        reqs = [types.InlinedRequest(
            model=args.model,
            contents=build_prompt(args.tag, desc, tag_type, ch),
            config=types.GenerateContentConfig(
                safety_settings=_SAFETY,
                tools=[types.Tool(google_search=types.GoogleSearch())] if args.grounding else None,
            )) for ch in jchunks]
        print(f"\n[ジョブ投入] chunk {ci}~{ci+len(jchunks)} ({len(reqs)}req) ...")
        job = client.batches.create(model=args.model, src=reqs)
        job = _wait_job(job.name)
        if job is None:
            print("[失敗] ジョブが失敗。中断")
            break
        cost, _ = _job_cost_usd(job, args.model, args.grounding)
        spent += cost
        resps = getattr(getattr(job, "dest", None), "inlined_responses", None) or []

        for ridx, resp in enumerate(resps):
            if ridx >= len(jchunks):
                break
            ch = jchunks[ridx]
            rr = getattr(resp, "response", None)
            text = (getattr(rr, "text", None) or "") if rr else ""
            data = _parse_json_object(text)
            by_id = {it.get("series_id"): it for it in (data or {}).get("series") or []}
            for r in ch:
                it = by_id.get(r["id"])
                cur = r.get(col) or []
                has = args.tag in cur
                if it is None:
                    counters["miss"] += 1
                    print(f"  ? {r['series_title'][:34]}  応答漏れ")
                    continue
                applies = bool(it.get("applies"))
                if applies and not has:
                    new = cur + [args.tag]
                    if not args.dry_run:
                        sb.table("openbd_test_series").update({col: new}).eq("id", r["id"]).execute()
                    counters["added"] += 1
                    print(f"  ＋付与 {r['series_title'][:34]}")
                elif (not applies) and has and args.remove:
                    new = [t for t in cur if t != args.tag]
                    if not args.dry_run:
                        sb.table("openbd_test_series").update({col: new}).eq("id", r["id"]).execute()
                    counters["removed"] += 1
                    print(f"  －除去 {r['series_title'][:34]}")
                else:
                    counters["nochange"] += 1

        ci += len(jchunks)
        state.setdefault("progress", {})[skey] = ci
        state.setdefault("counters", {})[skey] = counters
        state["spent_usd"] = spent
        save_state(state)
        print(f"  [進捗 {ci}/{len(chunks)}] 付与{counters['added']} 除去{counters['removed']} "
              f"変化なし{counters['nochange']} 漏れ{counters['miss']}  累計¥{spent*args.usd_jpy:,.0f}")

    print(f"\n[完了] tag='{args.tag}' 付与{counters['added']} 除去{counters['removed']} "
          f"変化なし{counters['nochange']} 漏れ{counters['miss']}  "
          f"総コスト¥{spent*args.usd_jpy:,.0f}" + ("  (DRY RUN)" if args.dry_run else "  → DB更新済み"))


if __name__ == "__main__":
    main()
