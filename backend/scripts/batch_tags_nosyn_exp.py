"""
実験: 既存シリーズ 50 件に「あらすじ無し」でタグ生成 (Batch API, batch_size=50)。

- 既存 (created_at < EXISTING_CUTOFF) のシリーズを 50 件取得
- シリーズブロックから書籍 description (あらすじ) を除外し、タイトル/著者/巻のみ渡す
- 50 件を 1 リクエストにまとめて Batch API へ投入 (batch_size=50)
- 完了をポーリング → 各シリーズの before → after を全件ログ出力 → DB 更新

使い方:
  python -m scripts.batch_tags_nosyn_exp [--model gemini-3.5-flash] [--grounding] [--dry-run]
"""
import sys
import os
import time
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.genai import types

from app.services.supabase_client import get_supabase
from app.services.gemini import client, _parse_json_object
from scripts.batch_enrich import (
    fetch_tag_candidates, build_tags_prompt, fetch_books,
    EXISTING_CUTOFF, _SAFETY,
)

N = 50


def fetch_existing_series(sb, n: int) -> list[dict]:
    """既存 (今回検出より前に作られた) シリーズを n 件。多巻優先で取る。"""
    rs = sb.table("openbd_test_series").select(
        "id, series_title, author, imprint, volume_count, "
        "atmosphere_tags, genre_tags, sub_genre_tags"
    ).is_("parent_series_id", "null").eq("is_special_edition", False)\
     .lt("created_at", EXISTING_CUTOFF)\
     .order("volume_count", desc=True).order("id", desc=False)\
     .limit(n).execute()
    return rs.data or []


def series_block_no_synopsis(sb, row: dict) -> str:
    """あらすじ抜きのシリーズブロック (書籍はタイトルのみ、description は載せない)。"""
    books = fetch_books(sb, row["id"])
    titles = []
    for b in sorted(books, key=lambda b: (b.get("volume_number") or 9999))[:6]:
        vol = b.get("volume_number")
        titles.append(f"    [{'vol'+str(vol) if vol is not None else ''}] {(b.get('title') or '').strip()}")
    book_section = "\n".join(titles) if titles else "    (書籍情報なし)"
    return (f"── series_id: {row['id']}\n"
            f"  シリーズ名: 「{row['series_title']}」\n"
            f"  著者: {row.get('author') or '?'}  レーベル: {row.get('imprint') or ''}  "
            f"既刊巻数: {row.get('volume_count') or '?'}\n"
            f"  書籍タイトル(あらすじ無し):\n{book_section}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemini-3.5-flash")
    ap.add_argument("--grounding", action="store_true", default=True)
    ap.add_argument("--no-grounding", dest="grounding", action="store_false")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sb = get_supabase()
    cands = fetch_tag_candidates(sb)
    valid = {f"{t}_tags": {c["name"] for c in cands[t]}
             for t in ("atmosphere", "genre", "sub_genre")}

    rows = fetch_existing_series(sb, N)
    print(f"[対象] 既存シリーズ {len(rows)} 件 (あらすじ無し / batch_size={len(rows)} / "
          f"model={args.model} grounding={args.grounding})")

    # 50 件を 1 リクエストにまとめる
    blocks = [series_block_no_synopsis(sb, r) for r in rows]
    prompt = build_tags_prompt(blocks, cands)
    req = types.InlinedRequest(
        model=args.model, contents=prompt,
        config=types.GenerateContentConfig(
            safety_settings=_SAFETY,
            tools=[types.Tool(google_search=types.GoogleSearch())] if args.grounding else None,
        ),
    )

    print("[Batch API] ジョブ投入 (1 リクエストに 50 件) ...")
    job = client.batches.create(model=args.model, src=[req])
    print(f"  job={job.name}  state={job.state}")

    # ポーリング
    while True:
        time.sleep(20)
        job = client.batches.get(name=job.name)
        st = str(job.state)
        print(f"  ... {st}")
        if "SUCCEEDED" in st:
            break
        if any(x in st for x in ("FAILED", "CANCELLED", "EXPIRED")):
            print(f"[失敗] {st}")
            return

    resps = getattr(getattr(job, "dest", None), "inlined_responses", None)
    text = ""
    if resps:
        rr = getattr(resps[0], "response", None)
        text = (getattr(rr, "text", None) or "") if rr else ""
    data = _parse_json_object(text)
    by_id = {it.get("series_id"): it for it in (data or {}).get("series") or []}

    # 全件ログ + 更新
    print("\n" + "=" * 70)
    print("[全件ログ] before → after")
    print("=" * 70)
    updated = missing = 0
    for i, row in enumerate(rows, 1):
        sid = row["id"]
        it = by_id.get(sid)
        before = {
            "A": row.get("atmosphere_tags") or [],
            "G": row.get("genre_tags") or [],
            "S": row.get("sub_genre_tags") or [],
        }
        if not it:
            missing += 1
            print(f"\n[{i}/{len(rows)}] 「{row['series_title'][:36]}」  ! 応答に含まれず (未更新)")
            print(f"    現状 A={before['A']} G={before['G']} S={before['S']}")
            continue
        after = {f[0].upper(): [n for n in (it.get(f) or []) if n in valid[f]]
                 for f in ("atmosphere_tags", "genre_tags", "sub_genre_tags")}
        changed = any(set(before[k]) != set(after[k]) for k in ("A", "G", "S"))
        mark = "✓更新" if changed else "= 変化なし"
        print(f"\n[{i}/{len(rows)}] 「{row['series_title'][:36]}」  {mark}")
        print(f"    A: {before['A']}  →  {after['A']}")
        print(f"    G: {before['G']}  →  {after['G']}")
        print(f"    S: {before['S']}  →  {after['S']}")
        if not args.dry_run:
            sb.table("openbd_test_series").update({
                "atmosphere_tags": after["A"],
                "genre_tags": after["G"],
                "sub_genre_tags": after["S"],
            }).eq("id", sid).execute()
        updated += 1

    print("\n" + "=" * 70)
    print(f"[完了] 応答あり {updated} 件 / 応答漏れ {missing} 件 / 全 {len(rows)} 件"
          + ("  (DRY RUN: DB未更新)" if args.dry_run else "  → DB更新済み"))


if __name__ == "__main__":
    main()
