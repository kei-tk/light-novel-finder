"""
シリーズ判定の Gemini Batch API 版 (grounding OFF)。

オンライン版 (gemini_detect_series.py) と同じクラスタリング・保存ロジックを再利用しつつ、
Gemini 呼び出しだけを Batch API に置き換える。Batch は通常価格の約半額・非同期。
シリーズ判定は「同じ著者の書籍からタイトルが類似する巻を束ねる」タスクなので Web 検索は不要。

フェーズ分離 (submit / fetch) で長時間バッチに対応する:
  submit: 孤児を含むクラスタを構築。
          - 1冊クラスタ (singleton) は Gemini 不要 → その場で standalone 保存
          - 2冊以上は Batch に投入し、状態を state.json に保存
  fetch : Batch の完了をポーリングし、結果を parse して DB 保存。--wait で完了まで待つ。

状態: cache/detect_series_batch/state.json
  { "jobs": [{"name": "batches/..", "cidxs": ["0","1",..]}, ..],  # cidxs は src と同順
    "clusters": { "0": {"isbns": [..], "author": ".."}, .. } }    # multi クラスタのみ

使い方:
  python -m scripts.detect_series_batch submit            # 孤児クラスタを投入
  python -m scripts.detect_series_batch fetch             # 完了確認 → 保存 (未完了なら状態表示)
  python -m scripts.detect_series_batch fetch --wait      # 完了までポーリングして保存
  python -m scripts.detect_series_batch submit --dry-run  # 投入せず件数だけ
"""
import sys
import os
import json
import time
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google import genai
from google.genai import types
from app.config import settings
from app.services.supabase_client import get_supabase
from app.services.gemini import detect_series_with_gemini, parse_detect_response
import scripts.gemini_detect_series as G

MODEL = settings.gemini_parse_model          # gemini-2.5-flash
CHUNK = 150                                   # 1 Batch ジョブあたりの inline リクエスト数
POLL_SEC = 30                                 # --wait 時のポーリング間隔
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "cache", "detect_series_batch")
STATE = os.path.join(CACHE_DIR, "state.json")

client = genai.Client(api_key=settings.gemini_api_key)


def _safety() -> list:
    return [types.SafetySetting(category=cat, threshold="BLOCK_NONE")
            for cat in ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                        "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")]


def _build_orphan_clusters(sb) -> tuple[dict, list]:
    """全 books を取得し、孤児を含むクラスタ (megacluster 分割済み) を返す。
    既処理本も context として含める (オンライン版 --orphans-only と同じ)。"""
    books = G.fetch_all_books(sb)
    book_index = {b["isbn"]: b for b in books if b.get("isbn")}
    print("[クラスタリング (共通人物ベース)]")
    clusters = G.cluster_books_by_contributors(books)
    clusters = [c for c in clusters if any(not G._book_is_processed(b) for b in c)]
    expanded: list[list[dict]] = []
    for c in clusters:
        expanded.extend(G._split_megacluster(c, G.MAX_BOOKS_PER_CLUSTER))
    print(f"  → 孤児を含むサブクラスタ {len(expanded):,}件")
    return book_index, expanded


def submit(args):
    sb = get_supabase()
    book_index, clusters = _build_orphan_clusters(sb)
    singles = [c for c in clusters if len(c) == 1]
    multi = [c for c in clusters if len(c) >= 2]
    print(f"  → Gemini必要 (2冊+): {len(multi):,} / singleton (即standalone): {len(singles):,}")

    # singleton は Gemini 不要。その場で standalone 保存。
    s_saved = 0
    for c in singles:
        author = G._primary_author_for_cluster(c)
        entry = {"isbn": c[0].get("isbn"), "reason": "singleton cluster"}
        s_saved += G._save_standalone(sb, author, [entry], book_index, args.dry_run)
    print(f"[singleton] standalone 保存: {s_saved:,}件")

    # multi クラスタのプロンプトを構築
    clusters_meta: dict[str, dict] = {}
    reqs: list[tuple[str, str]] = []
    for i, c in enumerate(multi):
        author = G._primary_author_for_cluster(c)
        shared = G._shared_names_in_cluster(c)
        imprint = G._most_common([b.get("imprint") for b in c])
        prompt = detect_series_with_gemini(
            c, shared_names=shared, imprint_hint=imprint, build_prompt_only=True)
        clusters_meta[str(i)] = {"isbns": [b["isbn"] for b in c if b.get("isbn")],
                                 "author": author}
        reqs.append((str(i), prompt))

    if args.dry_run:
        print(f"[DRY RUN] Batch 投入スキップ (投入予定 {len(reqs):,}件 / "
              f"{(len(reqs) + CHUNK - 1) // CHUNK} ジョブ)")
        return

    # CHUNK 件ずつ inline Batch ジョブに分割投入 (grounding OFF)
    jobs: list[dict] = []
    for k in range(0, len(reqs), CHUNK):
        part = reqs[k:k + CHUNK]
        inline = [types.InlinedRequest(
            model=MODEL, contents=prompt,
            config=types.GenerateContentConfig(safety_settings=_safety()),
            metadata={"cidx": cidx},
        ) for cidx, prompt in part]
        job = client.batches.create(
            model=MODEL, src=inline,
            config=types.CreateBatchJobConfig(display_name=f"detect-series-{k}"))
        jobs.append({"name": job.name, "cidxs": [cidx for cidx, _ in part]})
        print(f"  job {len(jobs)} 投入: {job.name} ({len(part)}件) state={job.state}")

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"jobs": jobs, "clusters": clusters_meta}, f, ensure_ascii=False)
    print(f"\n[状態保存] {STATE}  jobs={len(jobs)}  clusters={len(clusters_meta):,}")
    print("次: python -m scripts.detect_series_batch fetch --wait")


def _poll_once(jobs: list[dict]) -> tuple[bool, list]:
    """全ジョブの状態を取得。返り値 (全完了?, job オブジェクトリスト)。"""
    objs = []
    all_done = True
    for j in jobs:
        jo = client.batches.get(name=j["name"])
        objs.append(jo)
        st = str(jo.state)
        print(f"  {j['name'][-14:]}: {st}")
        if not st.endswith("SUCCEEDED"):
            all_done = False
            if st.endswith("FAILED") or st.endswith("CANCELLED") or st.endswith("EXPIRED"):
                print(f"    ! 異常終了: {jo.error}")
    return all_done, objs


def fetch(args):
    if not os.path.exists(STATE):
        print(f"状態ファイルが無い: {STATE}  先に submit してください。")
        return
    with open(STATE, encoding="utf-8") as f:
        st = json.load(f)
    jobs = st["jobs"]
    clusters_meta = st["clusters"]
    sb = get_supabase()

    # ポーリング
    while True:
        print(f"[ポーリング] {len(jobs)} ジョブ")
        all_done, objs = _poll_once(jobs)
        if all_done:
            break
        if not args.wait:
            print("\nまだ完了していません。後で `fetch` を再実行してください (--wait で待機可)。")
            return
        time.sleep(POLL_SEC)

    print("\n[全ジョブ完了] 結果を保存します")
    # book_index を再構築 (submit からの間に他処理が走っていても整合させる)
    books = G.fetch_all_books(sb)
    book_index = {b["isbn"]: b for b in books if b.get("isbn")}

    totals = {"saved_main": 0, "saved_sub": 0, "attached": 0,
              "standalone": 0, "linked_books": 0, "errors": 0, "empty": 0}

    for jo, j in zip(objs, jobs):
        responses = jo.dest.inlined_responses or []
        cidxs = j["cidxs"]
        if len(responses) != len(cidxs):
            print(f"  ! 警告: 応答数 {len(responses)} != 投入数 {len(cidxs)} "
                  f"(job {j['name'][-14:]})")
        for i, resp in enumerate(responses):
            if i >= len(cidxs):
                break
            cidx = cidxs[i]
            meta = clusters_meta.get(cidx)
            if not meta:
                continue
            author = meta["author"]
            cbooks = [book_index[isbn] for isbn in meta["isbns"] if isbn in book_index]
            input_isbns = {b["isbn"] for b in cbooks if b.get("isbn")}

            if getattr(resp, "error", None) or not getattr(resp, "response", None):
                print(f"  ! cidx={cidx} 応答エラー: {getattr(resp, 'error', None)}")
                totals["errors"] += 1
                continue
            text = resp.response.text
            result = parse_detect_response(text)
            groups = result.get("groups") or []
            standalone = result.get("standalone") or []
            if not groups and not standalone:
                totals["empty"] += 1

            # Gemini が取りこぼした ISBN は standalone fallback (NULL series_id 防止)
            covered: set[str] = set()
            for g in groups:
                for bk in (g.get("books") or []):
                    if bk.get("isbn"):
                        covered.add(bk["isbn"])
            for s in standalone:
                if s.get("isbn"):
                    covered.add(s["isbn"])
            for isbn in input_isbns - covered:
                standalone.append({"isbn": isbn, "reason": "batch response missed"})

            for g in groups:
                summ = G._save_group(sb, author, g, book_index, args.dry_run)
                for kk in ("saved_main", "saved_sub", "attached", "linked_books"):
                    totals[kk] += summ.get(kk, 0)
                if summ.get("errors"):
                    totals["errors"] += len(summ["errors"])
            totals["standalone"] += G._save_standalone(
                sb, author, standalone, book_index, args.dry_run)

    print("\n" + "=" * 60)
    print("[全体集計]")
    for k, v in totals.items():
        print(f"  {k}: {v:,}")
    if args.dry_run:
        print("\n[DRY RUN] DB 書き込みはスキップ")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="シリーズ判定 (Gemini Batch API 版, grounding OFF)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("submit", help="孤児クラスタを Batch 投入 (singleton は即保存)")
    ps.add_argument("--dry-run", action="store_true", help="投入・保存せず件数だけ")
    pf = sub.add_parser("fetch", help="完了確認 → 結果保存")
    pf.add_argument("--wait", action="store_true", help="完了までポーリングして保存")
    pf.add_argument("--dry-run", action="store_true", help="保存せず集計だけ")
    args = parser.parse_args()
    if args.cmd == "submit":
        submit(args)
    else:
        fetch(args)
