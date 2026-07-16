"""
6観点テキスト (*_text) を text-embedding-3-large (3072次元) で再 embedding し、
*_embedding 列に書き戻す。Grok 再収集は不要 (テキストは DB に保持済み)。

migration 031 で *_embedding 列を vector(3072) に作り直した後に実行する。
resume 対応: review_aspect_embedding が既に入っている series はスキップ (--redo で無視)。

使い方:
  python -m scripts.reembed_aspects_large            # 未処理を large で埋める
  python -m scripts.reembed_aspects_large --redo     # 全件やり直し
"""
import sys
import os
import time
import argparse
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai import OpenAI
from app.config import settings
from app.services.supabase_client import get_supabase

MODEL = settings.openai_aspect_embedding_model   # text-embedding-3-large
DIM = settings.aspect_embedding_dim              # 3072
ASPECT_COLS = {
    "plot": ("plot_text", "plot_embedding"),
    "cast": ("cast_text", "cast_embedding"),
    "theme": ("theme_text", "theme_embedding"),
    "mood": ("mood_text", "mood_embedding"),
    "reader": ("reader_text", "reader_embedding"),
    "review": ("review_aspect_text", "review_aspect_embedding"),
}
ASPECTS = list(ASPECT_COLS)
_openai = OpenAI(api_key=settings.openai_api_key)


def _paginate(qf, ps=1000):
    rows, off = [], 0
    while True:
        r = qf().range(off, off + ps - 1).execute()
        if not r.data:
            break
        rows.extend(r.data)
        if len(r.data) < ps:
            break
        off += ps
    return rows


def _retry(fn, what, n=5):
    markers = ("Connection", "connection", "disconnect", "Terminated", "timeout",
               "502", "503", "504", "pseudo-header", "cloudflare", "RemoteProtocol",
               "EOF", "SSL", "_ssl", "protocol", "Bad Gateway", "reset")
    for a in range(n):
        try:
            return fn()
        except Exception as e:
            if a == n - 1 or not any(m in str(e) for m in markers):
                raise
            time.sleep(1.5 * (a + 1))


def main(args):
    sb = get_supabase()
    text_cols = [c[0] for c in ASPECT_COLS.values()]
    print(f"[対象取得] model={MODEL} dim={DIM}")
    # ベクトル列 (3072) を select すると巨大で timeout するため、テキスト列のみ取得 (page 小さめ)
    rows = _paginate(lambda: sb.table("openbd_test_series")
                     .select("id," + ",".join(text_cols))
                     .not_.is_("plot_text", "null"), ps=400)
    if not args.redo:
        # 処理済み判定は id のみ取得 (ベクトル本体は引かない = 軽量)
        done = set()
        done_rows = _paginate(lambda: sb.table("openbd_test_series").select("id")
                              .not_.is_("review_aspect_embedding", "null"), ps=1000)
        done = {r["id"] for r in done_rows}
        before = len(rows)
        rows = [r for r in rows if r["id"] not in done]
        print(f"  未処理に絞り込み: {before} → {len(rows)} (処理済 {len(done)})")
    print(f"  対象 {len(rows)}件")
    if not rows:
        print("完了済み。")
        return

    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    CHUNK = 150
    saved = 0

    def embed_and_write(chunk):
        # chunk 内の全 (row, aspect, text) を集約して 1リクエストで embedding
        flat = []
        for li, r in enumerate(chunk):
            for a in ASPECTS:
                t = (r.get(ASPECT_COLS[a][0]) or "").strip()
                if t:
                    flat.append((li, a, t[:8000]))
        vmap = {}
        for i in range(0, len(flat), 128):
            part = flat[i:i + 128]
            emb = _openai.embeddings.create(model=MODEL, input=[t for _, _, t in part])
            for (li, a, _), d in zip(part, emb.data):
                vmap[(li, a)] = d.embedding
        cnt = 0
        for li, r in enumerate(chunk):
            upd = {"aspect_built_at": now}
            for a in ASPECTS:
                if (li, a) in vmap:
                    upd[ASPECT_COLS[a][1]] = vmap[(li, a)]
            try:
                _retry(lambda u=upd, sid=r["id"]: sb.table("openbd_test_series").update(u).eq("id", sid).execute(),
                       f"save {r['id'][:8]}")
                cnt += 1
            except Exception as e:
                print(f"  ! 保存失敗 {r['id'][:8]}: {str(e)[:80]}", flush=True)
        return cnt

    for ci in range(0, len(rows), CHUNK):
        chunk = rows[ci:ci + CHUNK]
        # embedding は chunk 単位、書き込みは並列
        flat = []
        for li, r in enumerate(chunk):
            for a in ASPECTS:
                t = (r.get(ASPECT_COLS[a][0]) or "").strip()
                if t:
                    flat.append((li, a, t[:8000]))
        vmap = {}
        for i in range(0, len(flat), 128):
            part = flat[i:i + 128]
            emb = _openai.embeddings.create(model=MODEL, input=[t for _, _, t in part])
            for (li, a, _), d in zip(part, emb.data):
                vmap[(li, a)] = d.embedding

        def write(li, r):
            upd = {"aspect_built_at": now}
            for a in ASPECTS:
                if (li, a) in vmap:
                    upd[ASPECT_COLS[a][1]] = vmap[(li, a)]
            try:
                _retry(lambda: sb.table("openbd_test_series").update(upd).eq("id", r["id"]).execute(),
                       f"save {r['id'][:8]}")
                return True
            except Exception as e:
                print(f"  ! 保存失敗 {r['id'][:8]}: {str(e)[:80]}", flush=True)
                return False

        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = [ex.submit(write, li, r) for li, r in enumerate(chunk)]
            for f in as_completed(futs):
                if f.result():
                    saved += 1
        print(f"  reembedded {saved}/{len(rows)}", flush=True)

    print(f"\n[完了] {saved}/{len(rows)}件を {MODEL}({DIM}) で再embedding")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--redo", action="store_true")
    main(p.parse_args())
