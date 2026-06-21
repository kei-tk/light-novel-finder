"""
変更を加えたタグだけを候補にして、全シリーズを 1 call で再評価する一発スクリプト。

CHANGED_TAGS で「再評価したいタグ」を atmosphere / genre / sub_genre 別に定義する。
各シリーズに対して:
  1. シリーズ配列から「候補にあるタグ」を全カテゴリで除去 (1からのリセット)
  2. Gemini に渡してどの候補が該当するか判定
  3. AI が選んだタグを正しい tag_type 配列に追加

使い方:
  python -m scripts.retag_changed --dry-run --limit 5
  python -m scripts.retag_changed --apply --workers 15
  python -m scripts.retag_changed --apply --limit 100 --workers 5
"""
import sys
import os
import json
import re
import time
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase
from app.services.gemini import (
    _generate_with_retry,
    _format_books_for_prompt,
    _build_series_meta_block,
    _parse_json_object,
)


# 再評価対象タグ。canonical name と短い説明をペアで持つ。
# 必要に応じてユーザーが書き換える前提。
CHANGED_TAGS: dict[str, list[dict]] = {
    "atmosphere": [
        {"name": "ほのぼの", "desc": "穏やかで日常的、安心して読める雰囲気"},
    ],
    "genre": [
        {"name": "ギャグ",        "desc": "笑える・テンポの良いギャグ要素が中心のジャンル"},
        {"name": "エロコメディ",   "desc": "性的描写を笑いに昇華するコメディジャンル"},
        {"name": "百合",          "desc": "女性同士の関係性・恋愛"},
        {"name": "BL",            "desc": "男性同士の関係性・恋愛"},
        {"name": "ミステリー",     "desc": "謎解き・推理が中心"},
    ],
    "sub_genre": [
        {"name": "師弟",            "desc": "師と弟子の関係 (主要人物同士)"},
        {"name": "パーティ冒険",     "desc": "パーティで冒険する展開"},
        {"name": "パラレルワールド",  "desc": "並行世界が物語の核"},
        {"name": "童話モチーフ",     "desc": "童話を下敷きにした要素"},
        {"name": "短編集",          "desc": "短編のオムニバス形式"},
        {"name": "婚約者",          "desc": "婚約相手・婚約状態が物語の中心"},
        {"name": "貴族",            "desc": "貴族の身分・社会が物語の中心"},
        {"name": "不老不死",         "desc": "不老 / 不死の存在が中心要素"},
        {"name": "タイムスリップ",   "desc": "時間移動が物語の重要要素 (やり直し・タイムリープ含む)"},
        {"name": "現代ファンタジー", "desc": "現代社会を舞台にしたファンタジー (和風・歴史等のファンタジー派生も含む)"},
        {"name": "異世界",          "desc": "別世界が舞台 (異世界ファンタジー等を含む)"},
    ],
}

_COL = {
    "atmosphere": "atmosphere_tags",
    "genre":      "genre_tags",
    "sub_genre":  "sub_genre_tags",
}


def _paginate(query_factory, page_size: int = 1000) -> list[dict]:
    rows = []
    offset = 0
    while True:
        q = query_factory()
        rs = q.range(offset, offset + page_size - 1).execute()
        batch = rs.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def fetch_targets(sb, limit: int | None, imprint: str | None) -> list[dict]:
    """親シリーズ (非特装) を全件取得。"""
    def factory():
        q = sb.table("openbd_test_series").select(
            "id, series_title, author, imprint, volume_count, "
            "atmosphere_tags, genre_tags, sub_genre_tags"
        ).is_("parent_series_id", "null").eq("is_special_edition", False)
        if imprint:
            q = q.eq("imprint", imprint)
        return q
    rows = _paginate(factory)
    if limit:
        rows = rows[:limit]
    return rows


def fetch_books(sb, sid: str) -> list[dict]:
    rs = sb.table("openbd_test_books").select(
        "isbn, title, description, volume_number"
    ).eq("series_id", sid).order("volume_number").limit(50).execute()
    return rs.data or []


def build_candidate_block() -> str:
    """Gemini 用の候補タグセクションを組み立てる (固定文字列なので 1 回だけ計算)"""
    lines = []
    label_map = {
        "atmosphere": "atmosphere_tags（雰囲気）",
        "genre":      "genre_tags（ジャンル）",
        "sub_genre":  "sub_genre_tags（サブジャンル・属性）",
    }
    for cat in ("atmosphere", "genre", "sub_genre"):
        items = CHANGED_TAGS.get(cat) or []
        if not items:
            continue
        lines.append(f"\n■ {label_map[cat]}：")
        for it in items:
            lines.append(f"  - {it['name']}: {it['desc']}")
    return "\n".join(lines)


_CANDIDATE_BLOCK = build_candidate_block()
_VALID_BY_CAT = {
    cat: {it["name"] for it in CHANGED_TAGS.get(cat, [])}
    for cat in ("atmosphere", "genre", "sub_genre")
}


def build_prompt(series_meta: str) -> str:
    return f"""
以下のライトノベル**シリーズ**について、以下の限定された候補タグの中から、
シリーズの特徴として**明確に該当する**ものだけを選んでください。

{series_meta}

【判定対象タグ — この候補リストの中だけから選ぶ】
{_CANDIDATE_BLOCK}

【判定ルール — 厳守】
- 候補リストに無いタグは絶対に追加しない
- そのラノベの**特徴として挙げられる**レベルで該当するもののみ選ぶ
- 主要人物の関係・属性、物語の核となる世界観・要素、作品形式に限る
- 端役・一時的な要素・微細な要素は対象外
- 推測・連想・「なんとなく」レベルは付けない (精度優先)
- 該当が無いカテゴリは空配列で返す

【出力 (JSON のみ、コードフェンス禁止)】
{{
  "atmosphere_tags": [...],
  "genre_tags": [...],
  "sub_genre_tags": [...]
}}
"""


def reset_and_apply(current: list[str] | None, cat: str, ai_selected: list[str]) -> list[str]:
    """
    現配列から候補にある全タグを除去 (= 1から)、AI が選んだタグを加える。
    候補に無い既存タグは保持する。
    """
    current = current or []
    valid_for_cat = _VALID_BY_CAT.get(cat, set())
    all_candidate_names = (_VALID_BY_CAT["atmosphere"] |
                           _VALID_BY_CAT["genre"] |
                           _VALID_BY_CAT["sub_genre"])
    # ステップ1: 候補に含まれる全タグを除去 (どのカテゴリの配列でも、変更対象は全部消す)
    kept = [n for n in current if n not in all_candidate_names]
    # ステップ2: AI が選んだ「このカテゴリ」のタグを加える (順序保持 + 重複除去)
    seen = set(kept)
    for n in ai_selected:
        if n in valid_for_cat and n not in seen:
            kept.append(n)
            seen.add(n)
    return kept


def process_one(sb, row: dict, model: str, use_grounding: bool,
                dry_run: bool, lock: threading.Lock) -> dict:
    sid = row["id"]
    title = row["series_title"]
    info = {"sid": sid, "title": title, "error": None,
            "selected": None, "before": None, "after": None}

    books = fetch_books(sb, sid)
    if not books:
        info["error"] = "紐付け書籍が0件"
        return info

    book_section = _format_books_for_prompt(books)
    series_meta = _build_series_meta_block(
        title, row.get("author"), row.get("imprint"),
        row.get("volume_count"), book_section
    )
    prompt = build_prompt(series_meta)

    try:
        text = _generate_with_retry(prompt, model=model, use_grounding=use_grounding)
    except Exception as e:
        info["error"] = f"Gemini 呼び出し失敗: {e}"
        return info
    if not text:
        info["error"] = "Gemini レスポンス空"
        return info
    data = _parse_json_object(text)
    if not data:
        info["error"] = "JSON parse 失敗"
        return info

    selected = {
        cat: [n for n in (data.get(_COL[cat]) or [])
              if isinstance(n, str) and n in _VALID_BY_CAT[cat]]
        for cat in ("atmosphere", "genre", "sub_genre")
    }
    info["selected"] = selected

    # 配列を組み立て (reset + apply)
    new_arrays = {}
    before = {}
    after = {}
    for cat, col in _COL.items():
        current = row.get(col)
        new_arr = reset_and_apply(current, cat, selected[cat])
        new_arrays[col] = new_arr
        before[col] = list(current or [])
        after[col] = new_arr
    info["before"] = before
    info["after"] = after

    if dry_run:
        return info

    try:
        sb.table("openbd_test_series").update(new_arrays).eq("id", sid).execute()
    except Exception as e:
        info["error"] = f"保存失敗: {e}"
    return info


def main():
    parser = argparse.ArgumentParser(description="変更タグだけで全シリーズを再評価")
    parser.add_argument("--dry-run", action="store_true",
                        help="(デフォルト) 計画のみ表示。DB は更新しない")
    parser.add_argument("--apply", action="store_true",
                        help="DB を更新する")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--imprint", default=None)
    parser.add_argument("--model", default="gemini-3.1-flash-lite")
    parser.add_argument("--no-grounding", action="store_true")
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args()
    if not (args.dry_run or args.apply):
        args.dry_run = True

    sb = get_supabase()
    print(f"[Gemini] model={args.model}  grounding={not args.no_grounding}")
    print(f"[候補タグ] atmosphere={len(CHANGED_TAGS['atmosphere'])}, "
          f"genre={len(CHANGED_TAGS['genre'])}, "
          f"sub_genre={len(CHANGED_TAGS['sub_genre'])}")
    print(f"[並列] workers={args.workers}")

    targets = fetch_targets(sb, args.limit, args.imprint)
    print(f"[対象] {len(targets):,} シリーズ")
    if not targets:
        return

    counters = {"ok": 0, "ng": 0}
    counters_lock = threading.Lock()
    print_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(process_one, sb, row, args.model,
                      not args.no_grounding, args.dry_run, threading.Lock()): row
            for row in targets
        }
        done = 0
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                info = fut.result()
            except Exception as e:
                with counters_lock:
                    counters["ng"] += 1
                with print_lock:
                    print(f"  例外 {row['series_title'][:30]}: {e}")
                continue
            with counters_lock:
                done += 1
                if info.get("error"):
                    counters["ng"] += 1
                else:
                    counters["ok"] += 1
            with print_lock:
                if info.get("error"):
                    print(f"[{done}/{len(targets)}] ! {info['title'][:40]}  {info['error']}")
                else:
                    sel = info["selected"]
                    print(f"[{done}/{len(targets)}] ✓ {info['title'][:40]}  "
                          f"A={sel['atmosphere']} G={sel['genre']} S={sel['sub_genre']}")

    print(f"\n[完了] 成功 {counters['ok']} / 失敗 {counters['ng']}"
          + ("  (DRY RUN)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
