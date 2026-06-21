"""
sub_genre タグ付与のハイブリッド・プロトタイプ。

「ベクトル検索による事前絞り込み → Gemini による最終選定」を sub_genre タグに適用する。

流れ:
  1. 294 件の sub_genre 候補タグを埋め込み (gemini-embedding-001)、キャッシュ。
  2. 対象シリーズ (タイトル+著者+あらすじ+既存タグ) を埋め込む。
  3. コサイン類似で sub_genre タグ上位 K 件に事前絞り込み (= pgvector 相当)。
  4. その K 件だけを Gemini に渡し「明確に該当するもの」を最終選定。
  5. 現状タグ / ベクトル上位K / Gemini最終結果 を比較表示 (--apply で保存)。

294 タグ程度では Python コサインで十分高速。件数が増えたら同じ流れを
Supabase pgvector の類似検索 RPC に差し替えればスケールする (本ファイル末尾の注記参照)。

使い方:
  python -m scripts.hybrid_subgenre_proto --series-id <UUID> [--topk 40] [--model gemini-3.5-flash] [--apply]
  python -m scripts.hybrid_subgenre_proto --series-id <UUID> --refresh-cache  # タグ埋め込み再生成
"""
import sys
import os
import json
import math
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.genai import types

from app.services.supabase_client import get_supabase
from app.services.gemini import (
    client,
    _generate_with_retry,
    _parse_json_object,
    _format_books_for_prompt,
)

EMB_MODEL = "gemini-embedding-001"
EMB_DIM = 768
CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "cache", "subgenre_tag_emb.json")


def embed_texts(texts: list[str], task_type: str) -> list[list[float]]:
    """テキスト群を埋め込む (バッチ)。task_type: RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY / SEMANTIC_SIMILARITY"""
    out: list[list[float]] = []
    for i in range(0, len(texts), 100):
        batch = texts[i:i + 100]
        r = client.models.embed_content(
            model=EMB_MODEL, contents=batch,
            config=types.EmbedContentConfig(
                output_dimensionality=EMB_DIM, task_type=task_type),
        )
        out.extend([list(e.values) for e in r.embeddings])
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def fetch_subgenre_tags(sb) -> list[dict]:
    rs = sb.table("tags").select("name, description").eq("tag_type", "sub_genre").execute()
    return rs.data or []


def load_tag_embeddings(sb, refresh: bool) -> dict[str, list[float]]:
    """sub_genre タグの埋め込みを取得 (キャッシュ優先)。"""
    tags = fetch_subgenre_tags(sb)
    names = [t["name"] for t in tags]
    if not refresh and os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, encoding="utf-8") as f:
            cache = json.load(f)
        if cache.get("dim") == EMB_DIM and set(cache.get("emb", {})) >= set(names):
            print(f"[キャッシュ] タグ埋め込み {len(names)} 件をロード")
            return {n: cache["emb"][n] for n in names}
    print(f"[埋め込み生成] sub_genre タグ {len(names)} 件 ...")
    # 「タグ名: 説明」を埋め込む (説明があれば文脈が増える)
    texts = []
    for t in tags:
        desc = (t.get("description") or "").strip()
        texts.append(f"{t['name']}: {desc}" if desc else t["name"])
    vecs = embed_texts(texts, "SEMANTIC_SIMILARITY")
    emb = {n: v for n, v in zip(names, vecs)}
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({"dim": EMB_DIM, "model": EMB_MODEL, "emb": emb}, f)
    print(f"[キャッシュ保存] {CACHE_PATH}")
    return emb


def fetch_series(sb, sid: str) -> dict | None:
    rs = sb.table("openbd_test_series").select(
        "id, series_title, author, imprint, volume_count, "
        "atmosphere_tags, genre_tags, sub_genre_tags, description"
    ).eq("id", sid).execute()
    return rs.data[0] if rs.data else None


def fetch_books(sb, sid: str) -> list[dict]:
    rs = sb.table("openbd_test_books").select(
        "isbn, title, description, volume_number"
    ).eq("series_id", sid).order("volume_number").limit(50).execute()
    return rs.data or []


def build_series_text(row: dict, books: list[dict]) -> str:
    """埋め込み用のシリーズ説明テキスト。"""
    parts = [f"タイトル: {row['series_title']}", f"著者: {row.get('author') or '?'}"]
    if row.get("genre_tags"):
        parts.append("ジャンル: " + ", ".join(row["genre_tags"]))
    desc = (row.get("description") or "").strip()
    if not desc and books:
        desc = (books[0].get("description") or "").strip()
    if desc:
        parts.append("あらすじ: " + desc[:500])
    return "\n".join(parts)


def gemini_select(row: dict, books: list[dict], candidates: list[str],
                  model: str, use_grounding: bool = False) -> dict:
    """ベクトルで絞った候補タグから Gemini が最終選定。"""
    book_section = _format_books_for_prompt(books, max_books=4, max_desc_chars=250) if books else "(書籍情報なし)"
    cand_block = "\n".join(f"  - {c}" for c in candidates)
    prompt = f"""
以下のライトノベルについて、sub_genre タグを下記候補リストの中から
「明確に該当するもののみ」選んでください。候補は事前にベクトル類似で絞り込んだものです。

シリーズ名: 「{row['series_title']}」
著者: {row.get('author') or '?'}  レーベル: {row.get('imprint') or ''}
書籍情報:
{book_section}

【sub_genre 候補 (この中からのみ選ぶ)】
{cand_block}

【ルール】
- 候補リストに無いタグは選ばない
- 明確に該当するものだけ (推測・連想は不可)、無ければ空配列

【出力 (JSON のみ)】
{{"sub_genre_tags": ["..."]}}
"""
    text = _generate_with_retry(prompt, model=model, use_grounding=use_grounding)
    return _parse_json_object(text) or {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series-id", required=True)
    ap.add_argument("--topk", type=int, default=40, help="ベクトルで絞る候補数")
    ap.add_argument("--model", default="gemini-3.5-flash")
    ap.add_argument("--grounding", action="store_true")
    ap.add_argument("--refresh-cache", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    sb = get_supabase()
    tag_emb = load_tag_embeddings(sb, args.refresh_cache)

    row = fetch_series(sb, args.series_id)
    if not row:
        print("シリーズが見つかりません")
        return
    books = fetch_books(sb, args.series_id)

    # ① シリーズを埋め込み → ② コサインで上位K件 (pgvector 相当の事前絞り込み)
    stext = build_series_text(row, books)
    svec = embed_texts([stext], "SEMANTIC_SIMILARITY")[0]
    scored = sorted(((n, _cosine(svec, v)) for n, v in tag_emb.items()),
                    key=lambda x: -x[1])
    topk = scored[:args.topk]
    cand_names = [n for n, _ in topk]

    print(f"\n■ 「{row['series_title']}」")
    print(f"現状 sub_genre: {row.get('sub_genre_tags')}")
    print(f"\n[① ベクトル事前絞り込み] 全{len(tag_emb)}タグ → 上位{args.topk}件 (cos類似)")
    print("  " + ", ".join(f"{n}({s:.2f})" for n, s in topk[:15]) + " …")

    # ③ Gemini 最終選定
    result = gemini_select(row, books, cand_names, args.model, args.grounding)
    selected = [t for t in (result.get("sub_genre_tags") or []) if t in set(cand_names)]
    print(f"\n[② Gemini最終選定] model={args.model} grounding={args.grounding}")
    print(f"  → {selected}")

    if args.apply:
        sb.table("openbd_test_series").update(
            {"sub_genre_tags": selected}).eq("id", args.series_id).execute()
        print("\n[保存] sub_genre_tags を更新しました")
    else:
        print("\n(--apply で保存。今は dry-run)")


if __name__ == "__main__":
    main()

# ───────────────────────────────────────────────────────────────────
# pgvector へのスケール (production 化メモ):
#   1) migration: create extension vector; alter table tags add column
#      embedding vector(768); create index on tags using hnsw
#      (embedding vector_cosine_ops);
#   2) タグ埋め込みを tags.embedding に保存 (本スクリプトの emb を update)。
#   3) 類似検索 RPC を定義:
#        create function match_subgenre_tags(q vector(768), k int)
#        returns table(name text, score float) language sql as $$
#          select name, 1 - (embedding <=> q) from tags
#          where tag_type='sub_genre' and embedding is not null
#          order by embedding <=> q limit k $$;
#   4) 本スクリプトの「コサイン上位K」部分を supabase.rpc('match_subgenre_tags',
#      {'q': svec, 'k': topk}) に差し替える。Gemini 選定はそのまま。
# ───────────────────────────────────────────────────────────────────
