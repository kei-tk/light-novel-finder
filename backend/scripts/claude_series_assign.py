"""
シリーズに入っていない新しい本を、どのシリーズの何巻かに振り分ける (毎週金曜のタスクで Claude が判定する)。
Gemini (gemini_detect_series) の代わり。判定は Claude が行い、このスクリプトは材料の書き出しと保存だけを行う。

2 モード:
  export : 発売日が前後 N 日で、シリーズ未所属の本と、同じ著者の既存シリーズを JSON に書き出す
  apply  : Claude が書いた判定 (JSON) を検査して DB に反映し、関係するシリーズの集計を更新する

判定 JSON の形 (scheduled/weekly_refresh.md に詳しく書く):
  {
    "new_series": [{"key": "n1", "series_title": "...", "author": "...", "parent_series_id": null}],
    "assignments": [
      {"isbn": "...", "action": "existing", "series_id": "<uuid>", "volume_number": 4},
      {"isbn": "...", "action": "new", "key": "n1", "volume_number": 1},
      {"isbn": "...", "action": "special_edition", "attached_to_isbn": "<本編の ISBN>"},
      {"isbn": "...", "action": "standalone"},
      {"isbn": "...", "action": "skip", "reason": "..."}
    ]
  }

使い方:
  python -m scripts.claude_series_assign export --out cache/weekly/2026-10-30/series_export.json
  python -m scripts.claude_series_assign apply --file cache/weekly/2026-10-30/series_decisions.json --dry-run
  python -m scripts.claude_series_assign apply --file cache/weekly/2026-10-30/series_decisions.json
"""
import sys
import os
import re
import json
import argparse
import unicodedata
import datetime as dt
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase
from app.services.pipeline_common import paginate, recompute_series, window

SERIES_COLS = "id,series_title,author,imprint,volume_count,latest_published_date,parent_series_id"
ACTIONS = {"existing", "new", "special_edition", "standalone", "skip"}


BOOK_COLS = "isbn,title,author,illustrator,imprint,published_date,description"


def _name_key(name: str) -> str:
    """表記ゆれ (全角/半角の空白、全角英数) を無視して著者名を比べるための形"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", name or ""))


def _name_pattern(name: str) -> str:
    """「皐月 陽龍」と「皐月　陽龍」の両方に当たる ilike の形。空白の位置は表記によって違うので、文字の間を % でつなぐ"""
    key = _name_key(name)
    return "%" + "%".join(key) + "%" if len(key) >= 2 else f"%{key}%"


def _authors(books: list[dict]) -> list[str]:
    return sorted({p.strip() for b in books for p in re.split(r"[/／]", b.get("author") or "") if p.strip()})


def _same_author(row_author: str | None, name: str) -> bool:
    return _name_key(name) in _name_key(row_author)


def _unassigned_books(supabase, days: int, lookback_days: int) -> list[dict]:
    """シリーズ未所属の本のうち、発売日が「lookback_days 日前 〜 days 日後」のものと、その著者の未所属の本。
    週次の取り直し (前後 days 日) より前に出た新刊が判定されないまま残らないように、過去は長めに見る。
    1巻が未所属のまま2巻だけが対象になると、2巻だけのシリーズができてしまうので、同じ著者の本も一緒に判定する"""
    lo = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
    win_lo, hi = window(days)
    books = paginate(lambda: supabase.table("openbd_test_books").select(BOOK_COLS)
                     .is_("series_id", "null").is_("attached_to_isbn", "null")
                     .gte("published_date", lo).lte("published_date", hi)
                     .order("published_date"))
    for b in books:
        if b["published_date"] < win_lo:
            b["outside_window"] = True
    seen = {b["isbn"] for b in books}
    for name in _authors(books):
        for b in (supabase.table("openbd_test_books").select(BOOK_COLS)
                  .is_("series_id", "null").is_("attached_to_isbn", "null")
                  .ilike("author", _name_pattern(name)).limit(30).execute().data or []):
            if b["isbn"] not in seen and _same_author(b.get("author"), name):
                seen.add(b["isbn"])
                books.append({**b, "outside_window": True})
    return books


def _candidate_series(supabase, books: list[dict]) -> list[dict]:
    """同じ著者名を含む既存シリーズ。最近の巻のタイトルと巻数も付け、続巻かどうかを判断しやすくする"""
    found: dict[str, dict] = {}
    for name in _authors(books):
        for s in supabase.table("openbd_test_series").select(SERIES_COLS).ilike("author", _name_pattern(name)).limit(40).execute().data or []:
            if _same_author(s.get("author"), name):
                found[s["id"]] = s
    for s in found.values():
        recent = (supabase.table("openbd_test_books").select("title,volume_number,published_date")
                  .eq("series_id", s["id"]).is_("attached_to_isbn", "null")
                  .order("published_date", desc=True).limit(3).execute().data or [])
        s["recent_volumes"] = recent
    return sorted(found.values(), key=lambda s: (s["author"] or "", s["series_title"] or ""))


def export(args) -> None:
    supabase = get_supabase()
    books = _unassigned_books(supabase, args.days, args.lookback_days)
    for b in books:
        b["description"] = (b.get("description") or "")[:160]
    out = {"window_days": args.days, "lookback_days": args.lookback_days, "books": books, "candidate_series": _candidate_series(supabase, books)}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[export] 未所属の本 {len(books)}冊 / 候補シリーズ {len(out['candidate_series'])}件 → {args.out}")


def _validate(supabase, dec: dict, books: dict[str, dict]) -> list[str]:
    errors: list[str] = []
    keys = {n.get("key") for n in dec.get("new_series", [])}
    for n in dec.get("new_series", []):
        if not n.get("key") or not (n.get("series_title") or "").strip() or not (n.get("author") or "").strip():
            errors.append(f"new_series に key / series_title / author がない: {n}")
        pid = n.get("parent_series_id")
        if pid and not supabase.table("openbd_test_series").select("id").eq("id", pid).execute().data:
            errors.append(f"parent_series_id が存在しない: {pid}")
    seen: set[str] = set()
    for a in dec.get("assignments", []):
        isbn, action = a.get("isbn"), a.get("action")
        if isbn not in books:
            errors.append(f"export に無い ISBN (未所属でないか、期間外): {isbn}")
        if isbn in seen:
            errors.append(f"ISBN が2回出てくる: {isbn}")
        seen.add(isbn)
        if action not in ACTIONS:
            errors.append(f"{isbn}: action が不正: {action}")
        if action == "existing" and not supabase.table("openbd_test_series").select("id").eq("id", a.get("series_id") or "").execute().data:
            errors.append(f"{isbn}: series_id が存在しない: {a.get('series_id')}")
        if action == "new" and a.get("key") not in keys:
            errors.append(f"{isbn}: new_series に無い key: {a.get('key')}")
        if action == "special_edition":
            base = a.get("attached_to_isbn")
            if not base or not supabase.table("openbd_test_books").select("isbn").eq("isbn", base).execute().data:
                errors.append(f"{isbn}: attached_to_isbn の本が DB に無い: {base}")
    missing = set(books) - seen
    if missing:
        print(f"[検査] 判定のない本 {len(missing)}冊 (次回に持ち越し): {sorted(missing)[:5]}…")
    return errors


def apply(args) -> None:
    supabase = get_supabase()
    with open(args.file, encoding="utf-8") as f:
        dec = json.load(f)
    books = {b["isbn"]: b for b in _unassigned_books(supabase, args.days, args.lookback_days)}
    errors = _validate(supabase, dec, books)
    if errors:
        print("[検査] 判定に誤りがあるので、何も書き込まない:")
        for e in errors:
            print("  -", e)
        sys.exit(1)

    touched: set[str] = set()
    key_to_id: dict[str, str] = {}
    for n in dec.get("new_series", []):
        title, author = n["series_title"].strip(), n["author"].strip()
        record = {"series_title": title, "author": author, "normalized_title": f"\x03claude\x03{author}\x03{title}",
                  "is_series": False, "is_special_edition": False, "parent_series_id": n.get("parent_series_id")}
        if args.dry_run:
            key_to_id[n["key"]] = f"(dry-run:{n['key']})"
            print(f"  + シリーズ「{title}」/ {author}")
            continue
        rs = supabase.table("openbd_test_series").upsert(record, on_conflict="normalized_title,author",
                                                         returning="representation").execute()
        key_to_id[n["key"]] = rs.data[0]["id"]
        touched.add(rs.data[0]["id"])

    counts = {a: 0 for a in ACTIONS}
    for a in dec.get("assignments", []):
        isbn, action = a["isbn"], a["action"]
        counts[action] += 1
        book = books[isbn]
        if action == "skip":
            print(f"  - 保留 {isbn} {book['title']}: {a.get('reason')}")
            continue
        if action == "special_edition":
            change = {"attached_to_isbn": a["attached_to_isbn"], "series_id": None, "volume_number": None, "volume_number_sub": None}
        elif action == "standalone":
            author = (book.get("author") or "").strip() or "不明"
            record = {"series_title": book["title"], "author": author,
                      "normalized_title": f"\x03claude-standalone\x03{author}\x03{isbn}",
                      "is_series": False, "is_special_edition": False}
            if args.dry_run:
                print(f"  + 単巻「{book['title']}」")
                continue
            rs = supabase.table("openbd_test_series").upsert(record, on_conflict="normalized_title,author",
                                                             returning="representation").execute()
            sid = rs.data[0]["id"]
            change = {"series_id": sid, "volume_number": None, "volume_number_sub": None}
        else:
            sid = a["series_id"] if action == "existing" else key_to_id[a["key"]]
            change = {"series_id": sid, "volume_number": a.get("volume_number"), "volume_number_sub": a.get("volume_number_sub")}
        if args.dry_run:
            print(f"  ~ {isbn} {action} {change} {book['title'][:30]}")
            continue
        supabase.table("openbd_test_books").update(change).eq("isbn", isbn).execute()
        if change.get("series_id"):
            touched.add(change["series_id"])

    n = recompute_series(supabase, touched, dry_run=args.dry_run)
    print(f"[結果] {counts} / 集計を更新したシリーズ {n}件" + (" (dry-run)" if args.dry_run else ""))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="未所属の新しい本をシリーズに振り分ける (判定は Claude)")
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("export")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--lookback-days", type=int, default=365, help="未所属の本を、何日前の発売まで判定の対象にするか")
    p.add_argument("--out", required=True)
    p = sub.add_parser("apply")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--lookback-days", type=int, default=365)
    p.add_argument("--file", required=True)
    p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    export(args) if args.mode == "export" else apply(args)
