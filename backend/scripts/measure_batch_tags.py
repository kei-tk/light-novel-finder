"""
バッチ tag 付与の計測用スクリプト (使い捨て検証)。

指定した複数シリーズを「1 Gemini call」にまとめて atmosphere/genre/sub_genre タグを
判定し (grounding ON)、結果タグと実コストを表示する。
grounding は call 数従量なので、N 件を 1 call にまとめると grounding 代が 1/N になる。

使い方:
  python -m scripts.measure_batch_tags --model gemini-3.5-flash --grounding \
      30ddfe03-... bd425b6c-... c4d6bd20-...
"""
import sys
import os
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase
from app.services.gemini import (
    _format_books_for_prompt,
    _parse_json_object,
    _generate_with_retry,
    reset_usage,
    get_usage,
)

# 3.5-flash 実単価 (USD / 1M tokens) + grounding flat 料金
PRICE = {"input": 1.50, "output": 9.00}      # 2026-06 公式値
GROUNDING_PER_CALL = 0.035


def fetch_candidates(sb, tag_type: str) -> list[dict]:
    rs = sb.table("tags").select("name, description").eq("tag_type", tag_type).execute()
    return rs.data or []


def fetch_series(sb, sid: str) -> dict | None:
    rs = sb.table("openbd_test_series").select(
        "id, series_title, author, imprint, volume_count, "
        "atmosphere_tags, genre_tags, sub_genre_tags"
    ).eq("id", sid).execute()
    return rs.data[0] if rs.data else None


def fetch_books(sb, sid: str) -> list[dict]:
    rs = sb.table("openbd_test_books").select(
        "isbn, title, description, volume_number"
    ).eq("series_id", sid).order("volume_number").limit(50).execute()
    return rs.data or []


def cand_block(cands: list[dict]) -> str:
    lines = []
    for it in cands:
        name = it.get("name")
        desc = (it.get("description") or "").strip()
        lines.append(f"  - {name}: {desc}" if desc else f"  - {name}")
    return "\n".join(lines)


def series_block(row: dict, books: list[dict]) -> str:
    title = row["series_title"]
    author = row.get("author") or "?"
    imprint = row.get("imprint") or ""
    vc = row.get("volume_count") or "?"
    bsec = _format_books_for_prompt(books, max_books=4, max_desc_chars=250) if books else "(書籍情報なし)"
    return (f"── series_id: {row['id']}\n"
            f"  シリーズ名: 「{title}」\n"
            f"  著者: {author}  レーベル: {imprint}  既刊巻数: {vc}\n"
            f"  書籍情報:\n{bsec}")


def build_prompt(blocks: list[str], atmo, genre, sub) -> str:
    n = len(blocks)
    return f"""
以下の {n} 件のライトノベルシリーズについて、それぞれの atmosphere_tags / genre_tags /
sub_genre_tags を、下記の各候補リストの中から「明確に該当するもののみ」選んでください。
Web 検索も活用して、作品の正確な内容を踏まえて判定してください。

【atmosphere 候補 — この中からのみ選ぶ】
{cand_block(atmo)}

【genre 候補 — この中からのみ選ぶ】
{cand_block(genre)}

【sub_genre 候補 — この中からのみ選ぶ】
{cand_block(sub)}

【シリーズ一覧】
{chr(10).join(blocks)}

【判定ルール — 厳守】
- 各候補リストに無いタグは絶対に追加しない
- 明確に該当するものだけ (推測・連想は不可)、該当が無ければ空配列
- **必ず全 {n} 件について返すこと** (series_id 必須)

【出力 (JSON のみ、コードフェンス禁止)】
{{
  "series": [
    {{"series_id": "<UUID>", "atmosphere_tags": ["..."], "genre_tags": ["..."], "sub_genre_tags": ["..."]}},
    ...
  ]
}}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("series_ids", nargs="+")
    ap.add_argument("--model", default="gemini-3.5-flash")
    ap.add_argument("--grounding", action="store_true")
    ap.add_argument("--batch-size", type=int, default=0,
                    help="1 call にまとめる件数 (0=全件1call)")
    args = ap.parse_args()

    sb = get_supabase()
    atmo = fetch_candidates(sb, "atmosphere")
    genre = fetch_candidates(sb, "genre")
    sub = fetch_candidates(sb, "sub_genre")
    print(f"[候補] atmosphere {len(atmo)} / genre {len(genre)} / sub_genre {len(sub)}")

    rows, row_block = [], {}
    for sid in args.series_ids:
        row = fetch_series(sb, sid)
        if not row:
            print(f"  ! {sid} 見つからず")
            continue
        rows.append(row)
        row_block[sid] = series_block(row, fetch_books(sb, sid))

    valid = {
        "atmosphere_tags": {c["name"] for c in atmo},
        "genre_tags": {c["name"] for c in genre},
        "sub_genre_tags": {c["name"] for c in sub},
    }

    bs = args.batch_size if args.batch_size > 0 else len(rows)
    chunks = [rows[i:i + bs] for i in range(0, len(rows), bs)]
    print(f"[Gemini] model={args.model}  grounding={args.grounding}  "
          f"batch_size={bs} → {len(chunks)} call")
    reset_usage()
    by_id = {}
    for ci, chunk in enumerate(chunks, 1):
        blocks = [row_block[r["id"]] for r in chunk]
        prompt = build_prompt(blocks, atmo, genre, sub)
        print(f"  call {ci}/{len(chunks)}: {len(chunk)}件 "
              f"({', '.join(r['series_title'][:12] for r in chunk)})")
        text = _generate_with_retry(prompt, model=args.model, use_grounding=args.grounding)
        data = _parse_json_object(text)
        for it in (data or {}).get("series") or []:
            by_id[it.get("series_id")] = it

    print("\n========== バッチ判定結果 ==========")
    for row in rows:
        sid = row["id"]
        it = by_id.get(sid)
        print(f"\n■ 「{row['series_title']}」")
        if not it:
            print("  ! 応答に含まれず")
            continue
        for field, label in [("atmosphere_tags", "atmosphere"),
                             ("genre_tags", "genre"),
                             ("sub_genre_tags", "sub_genre")]:
            picked = [n for n in (it.get(field) or []) if n in valid[field]]
            dropped = [n for n in (it.get(field) or []) if n not in valid[field]]
            cur = row.get(field) or []
            extra = f"  (候補外で除外: {dropped})" if dropped else ""
            print(f"  {label:10} 現状={cur}")
            print(f"  {'':10} batch={picked}{extra}")

    # コスト
    usage = get_usage()
    u = usage["by_model"].get(args.model, {"input": 0, "output": 0, "thinking": 0, "calls": 0})
    out_tok = u["output"] + u["thinking"]
    in_cost = u["input"] / 1_000_000 * PRICE["input"]
    out_cost = out_tok / 1_000_000 * PRICE["output"]
    ground_cost = (u["calls"] if args.grounding else 0) * GROUNDING_PER_CALL
    total = in_cost + out_cost + ground_cost
    n = len(rows)
    print("\n========== 実測コスト (バッチ) ==========")
    print(f"  call={u['calls']}  input={u['input']:,}  output={u['output']:,}  thinking={u['thinking']:,}")
    print(f"  token: ${in_cost + out_cost:.4f} (in ${in_cost:.4f} / out ${out_cost:.4f})")
    print(f"  grounding: ${ground_cost:.4f} ({u['calls']}call × ${GROUNDING_PER_CALL})")
    print(f"  ─────")
    print(f"  合計: ${total:.4f}  (1件あたり ${total/max(n,1):.4f})")
    print(f"  → 1,000件 外挿: ${total/max(n,1)*1000:.2f}")
    print("=====================================")


if __name__ == "__main__":
    main()
