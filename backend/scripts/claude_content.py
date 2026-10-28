"""
新作シリーズの文章 (タグ・X 用ハッシュタグ・紹介文・3観点の原文) を Claude が作るための入出力 (2日ごとのタスク)。
Gemini (タグ・スコア) と Grok (6観点・レビュー要約) の代わり。文章は Claude が書き、このスクリプトは
材料の書き出しと、検査・ベクトル化・保存だけを行う。スコアと plot / cast / review の観点は作らない。

対象: 親シリーズで、まだ文章がなく (theme_text と content_built_at が NULL)、1巻の発売から min-age-days 日たったもの。
      新しいものから順に、1回 limit 件まで。

2 モード:
  export : 対象シリーズ (巻の ISBN・あらすじ) と、選べるタグの一覧、文体の手本を JSON に書き出す
  apply  : Claude が書いた結果 (JSON) を検査し、3観点を 1536 次元のベクトルにして DB に保存する

結果 JSON の形 (scheduled/content_builder.md に詳しく書く):
  {"results": [{
     "series_id": "<uuid>",
     "genre_tags": [...], "atmosphere_tags": [...], "sub_genre_tags": [...],   ← 一覧にあるものだけ
     "x_hashtags": [...],                                                      ← # なし、3〜5個
     "tagline": "...",                                                         ← 1文
     "theme_text": "...", "mood_text": "...", "reader_text": "...",
     "sources": ["https://bookmeter.com/books/..."],                           ← 読んだページ (感想の本文は書かない)
     "new_tag_suggestions": [...]                                               ← 一覧に無いが欲しいタグ (保存はしない)
  }]}

使い方:
  python -m scripts.claude_content export --out cache/content/2026-10-30/export.json
  python -m scripts.claude_content apply --file cache/content/2026-10-30/results.json --dry-run
  python -m scripts.claude_content apply --file cache/content/2026-10-30/results.json
"""
import sys
import os
import re
import json
import argparse
import datetime as dt
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai import OpenAI

from app.config import settings
from app.services.supabase_client import get_supabase
from app.services.pipeline_common import paginate

CONTENT_SOURCE = "claude_v1"
# 既存の 1536 列は text-embedding-3-large (3072) を切り詰めて正規化したもの。dimensions=1536 で同じ空間になる (cos 0.99987 で確認済み)
EMBED_MODEL = "text-embedding-3-large"
EMBED_DIM = 1536
ASPECTS = ["theme", "mood", "reader"]

# 文字数の範囲。既存の原文 (130〜250字程度) とそろえ、外れたら保存しない
LIMITS = {"tagline": (30, 100), "theme_text": (80, 320), "mood_text": (80, 320), "reader_text": (80, 320)}
TAG_COUNTS = {"genre_tags": (1, 5), "atmosphere_tags": (1, 6), "sub_genre_tags": (3, 15), "x_hashtags": (3, 5)}


def _targets(supabase, min_age_days: int, limit: int) -> list[dict]:
    cutoff = (dt.date.today() - dt.timedelta(days=min_age_days)).isoformat()
    return (supabase.table("openbd_test_series")
            .select("id,series_title,author,illustrator,imprint,volume_count,first_published_date,latest_published_date,description")
            .is_("parent_series_id", "null").is_("theme_text", "null").is_("content_built_at", "null")
            .lte("first_published_date", cutoff)
            .order("first_published_date", desc=True).limit(limit).execute().data or [])


def _vocabulary(supabase) -> dict[str, list[dict]]:
    rows = paginate(lambda: supabase.table("tags").select("name,tag_type,subcategory,description"))
    vocab: dict[str, list[dict]] = {"genre": [], "atmosphere": [], "sub_genre": []}
    for r in rows:
        if r["tag_type"] in vocab:
            vocab[r["tag_type"]].append({k: v for k, v in r.items() if v and k != "tag_type"})
    return vocab


def _examples(supabase) -> list[dict]:
    """文体と粒度の手本。巻数の多い (感想が多く、原文が安定している) シリーズから選ぶ"""
    return (supabase.table("openbd_test_series")
            .select("series_title,genre_tags,atmosphere_tags,sub_genre_tags,theme_text,mood_text,reader_text")
            .is_("parent_series_id", "null").not_.is_("theme_text", "null")
            .in_("series_title", ["六畳間の侵略者!?", "少年陰陽師", "ソードアート・オンライン"])
            .execute().data or [])


def export(args) -> None:
    supabase = get_supabase()
    targets = _targets(supabase, args.min_age_days, args.limit)
    for t in targets:
        vols = (supabase.table("openbd_test_books").select("isbn,title,volume_number,published_date,description")
                .eq("series_id", t["id"]).is_("attached_to_isbn", "null").order("published_date").execute().data or [])
        # あらすじは巻ごとに重複しやすいので、同じ文は1回だけ渡す
        seen: set[str] = set()
        for v in vols:
            d = (v.pop("description") or "").strip()
            v["synopsis"] = d[:400] if d and d not in seen else None
            seen.add(d)
        t["volumes"] = vols
        t["series_id"] = t.pop("id")
        t.pop("description", None)
    out = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "targets": targets,
           "vocabulary": _vocabulary(supabase), "examples": _examples(supabase),
           "limits": {**{k: list(v) for k, v in LIMITS.items()}, **{k: list(v) for k, v in TAG_COUNTS.items()}}}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[export] 対象 {len(targets)}シリーズ → {args.out}")


def _check(r: dict, vocab: dict[str, set[str]], target_ids: set[str]) -> tuple[dict | None, list[str]]:
    """検査に通れば保存する列を返す。一覧に無いタグは落とし (警告)、文字数や個数が範囲外なら保存しない"""
    problems: list[str] = []
    if r.get("series_id") not in target_ids:
        return None, ["対象ではない (すでに文章がある / 親シリーズでない / 存在しない)"]
    row: dict = {}
    for col, kind in [("genre_tags", "genre"), ("atmosphere_tags", "atmosphere"), ("sub_genre_tags", "sub_genre")]:
        tags = list(dict.fromkeys(t.strip() for t in r.get(col) or [] if t and t.strip()))
        unknown = [t for t in tags if t not in vocab[kind]]
        if unknown:
            problems.append(f"{col}: 一覧に無いので外した {unknown}")
        row[col] = [t for t in tags if t in vocab[kind]]
    row["x_hashtags"] = list(dict.fromkeys(re.sub(r"[#＃\s]", "", h) for h in r.get("x_hashtags") or [] if h and re.sub(r"[#＃\s]", "", h)))
    for col, (lo, hi) in TAG_COUNTS.items():
        if not lo <= len(row[col]) <= hi:
            return None, problems + [f"{col} の個数が範囲外: {len(row[col])} (許容 {lo}〜{hi})"]
    for col, (lo, hi) in LIMITS.items():
        text = (r.get(col) or "").strip()
        if not lo <= len(text) <= hi:
            return None, problems + [f"{col} の文字数が範囲外: {len(text)} (許容 {lo}〜{hi})"]
        row[col] = text
    return row, problems


def apply(args) -> None:
    supabase = get_supabase()
    with open(args.file, encoding="utf-8") as f:
        results = json.load(f).get("results", [])
    vocab = {k: {t["name"] for t in v} for k, v in _vocabulary(supabase).items()}
    ids = [r.get("series_id") for r in results if r.get("series_id")]
    target_ids = {t["id"] for t in (supabase.table("openbd_test_series").select("id")
                                    .in_("id", ids).is_("parent_series_id", "null")
                                    .is_("theme_text", "null").is_("content_built_at", "null").execute().data or [])} if ids else set()

    ok: list[tuple[dict, dict]] = []
    skipped = 0
    for r in results:
        row, problems = _check(r, vocab, target_ids)
        for p in problems:
            print(f"  ! {r.get('series_id')}: {p}")
        if row is None:
            skipped += 1
            continue
        ok.append((r, row))
    if r_sugg := sorted({t for r in results for t in r.get("new_tag_suggestions") or []}):
        print(f"[提案] 一覧に無いタグの提案 (保存はしない): {r_sugg}")
    print(f"[検査] 保存できる {len(ok)}件 / 保存しない {skipped}件")
    if not ok:
        return

    if args.dry_run:
        for r, row in ok:
            print(f"  ~ {r['series_id']} 紹介文「{row['tagline']}」 X {row['x_hashtags']}")
        print("[結果] dry-run なので保存しない")
        return

    # 3観点 × 件数を1回の呼び出しでベクトルにする
    texts = [row[f"{a}_text"] for _, row in ok for a in ASPECTS]
    emb = OpenAI(api_key=settings.openai_api_key).embeddings.create(model=EMBED_MODEL, input=texts, dimensions=EMBED_DIM)
    vectors = [d.embedding for d in emb.data]
    now = dt.datetime.now(dt.UTC).isoformat()
    for i, (r, row) in enumerate(ok):
        update = {
            **row,
            **{f"{a}_embedding_1536": vectors[i * len(ASPECTS) + j] for j, a in enumerate(ASPECTS)},
            "aspect_source": CONTENT_SOURCE, "aspect_built_at": now,
            "content_source": CONTENT_SOURCE, "content_built_at": now,
            # 読んだページの URL だけを残す (感想の本文は、投稿者の著作物なので保存しない)
            "review_sources": [s for s in r.get("sources") or [] if isinstance(s, str) and s.startswith("http")][:30],
        }
        supabase.table("openbd_test_series").update(update).eq("id", r["series_id"]).execute()
    print(f"[結果] 保存 {len(ok)}件 (埋め込み {emb.usage.total_tokens} トークン)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="新作シリーズの文章を Claude が作るための入出力")
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("export")
    p.add_argument("--out", required=True)
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--min-age-days", type=int, default=30)
    p = sub.add_parser("apply")
    p.add_argument("--file", required=True)
    p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    export(args) if args.mode == "export" else apply(args)
