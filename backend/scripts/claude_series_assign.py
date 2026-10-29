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
    """表記ゆれ (全角/半角の空白、全角英数、国立国会図書館式の「猫又,ぬこ,1990-」、「／著」) を無視して著者名を比べるための形"""
    s = unicodedata.normalize("NFKC", name or "")
    s = re.sub(r",?\s*\d{4}-\d{0,4}", "", s)
    s = re.sub(r"[/／]?\s*著$", "", s)
    return re.sub(r"[\s,，]+", "", s)


def _name_pattern(name: str) -> str:
    """「皐月 陽龍」と「皐月　陽龍」の両方に当たる ilike の形。空白の位置は表記によって違うので、文字の間を % でつなぐ"""
    key = _name_key(name)
    return "%" + "%".join(key) + "%" if len(key) >= 2 else f"%{key}%"


def _authors(books: list[dict]) -> list[str]:
    """著者名の一覧。古いデータには「著者 イラストレーター」を空白でつないだ表記があるので、3文字以上の各語も加える"""
    names: set[str] = set()
    for b in books:
        for part in re.split(r"[/／]", b.get("author") or ""):
            part = part.strip()
            if not part:
                continue
            names.add(part)
            words = [w for w in re.split(r"\s+", part) if len(_name_key(w)) >= 3]
            if len(words) >= 2:
                names.update(words)
    return sorted(n for n in names if len(_name_key(n)) >= 2)


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


def _title_pattern(title: str) -> str:
    """題名の頭5文字を % でつないだ ilike の形 (空白・全角半角の違いに強い)"""
    head = re.sub(r"\s+", "", title or "")[:5]
    return "%" + "%".join(head) + "%"


def _candidate_series(supabase, books: list[dict]) -> list[dict]:
    """候補の既存シリーズ。同じ著者名を含むものに加え、著者の表記が崩れていても拾えるように、題名の頭が似ているものも入れる。
    どれに入れるかは判定する側 (Claude) が決めるので、ここでは広めに拾う。各本には、自分の候補の id を candidate_ids として付ける。
    最近の巻のタイトルと巻数も付け、続巻かどうかを判断しやすくする"""
    found: dict[str, dict] = {}
    by_author: dict[str, set[str]] = {}
    for name in _authors(books):
        ids = by_author.setdefault(_name_key(name), set())
        for s in supabase.table("openbd_test_series").select(SERIES_COLS).ilike("author", _name_pattern(name)).limit(40).execute().data or []:
            if _same_author(s.get("author"), name):
                found[s["id"]] = s
                ids.add(s["id"])
    title_cache: dict[str, list[dict]] = {}
    for b in books:
        pat = _title_pattern(b.get("title"))
        if pat not in title_cache:
            title_cache[pat] = supabase.table("openbd_test_series").select(SERIES_COLS).ilike("series_title", pat).limit(30).execute().data or []
        ids = {s["id"] for s in title_cache[pat]}
        for s in title_cache[pat]:
            found[s["id"]] = s
        for name in _authors([b]):
            ids |= by_author.get(_name_key(name), set())
        if b.get("series_id"):
            ids.add(b["series_id"])
        b["candidate_ids"] = sorted(ids)
    missing = {b["series_id"] for b in books if b.get("series_id")} - set(found)
    for sid in missing:
        found.update({r["id"]: r for r in supabase.table("openbd_test_series").select(SERIES_COLS).eq("id", sid).execute().data or []})
    # 本が1冊も入っていない「抜け殻」のシリーズは候補から外す。集計の巻数だけが古いまま残っていることがあり、
    # 本物のシリーズに見えて新刊を吸い寄せてしまうため (2026-10 の判定で実際に起きた)
    current = {b["series_id"] for b in books if b.get("series_id")}
    empty = {sid for sid in found if sid not in current
             and not supabase.table("openbd_test_books").select("isbn").eq("series_id", sid).limit(1).execute().data}
    for sid in empty:
        found.pop(sid)
    for b in books:
        b["candidate_ids"] = [i for i in b.get("candidate_ids", []) if i not in empty]
    for s in found.values():
        recent = (supabase.table("openbd_test_books").select("title,volume_number,published_date")
                  .eq("series_id", s["id"]).is_("attached_to_isbn", "null")
                  .order("published_date", desc=True).limit(3).execute().data or [])
        s["recent_volumes"] = recent
    return sorted(found.values(), key=lambda s: (s["author"] or "", s["series_title"] or ""))


def _books_by_isbns(supabase, path: str) -> list[dict]:
    """付け替え用: 指定した ISBN の本を、今の所属 (series_id / volume_number / attached_to_isbn) つきで取る"""
    with open(path, encoding="utf-8") as f:
        isbns = [line.strip() for line in f if line.strip()]
    books: list[dict] = []
    for i in range(0, len(isbns), 100):
        books += supabase.table("openbd_test_books").select(BOOK_COLS + ",series_id,volume_number,attached_to_isbn").in_("isbn", isbns[i:i + 100]).execute().data or []
    return sorted(books, key=lambda b: (b.get("published_date") or "9999", b["isbn"]))


def _target_books(supabase, args) -> list[dict]:
    if getattr(args, "isbns_file", None):
        return _books_by_isbns(supabase, args.isbns_file)
    return _unassigned_books(supabase, args.days, args.lookback_days)


def export(args) -> None:
    supabase = get_supabase()
    books = _target_books(supabase, args)
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
    books = {b["isbn"]: b for b in _target_books(supabase, args)}
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
        # 付け替え (--isbns-file) では、今の所属と同じなら書かない
        current = {k: book.get(k) for k in change if k in book}
        if len(current) == len(change) and all(current[k] == change[k] for k in change):
            counts["unchanged"] = counts.get("unchanged", 0) + 1
            continue
        if args.dry_run:
            before = f" (今: {book.get('series_id') or book.get('attached_to_isbn') or '未所属'} v{book.get('volume_number')})" if "series_id" in book else ""
            print(f"  ~ {isbn} {action} {change}{before} {book['title'][:30]}")
            continue
        supabase.table("openbd_test_books").update(change).eq("isbn", isbn).execute()
        if change.get("series_id"):
            touched.add(change["series_id"])
        if book.get("series_id") and book["series_id"] != change.get("series_id"):
            touched.add(book["series_id"])  # 抜けた側のシリーズも巻数などを数え直す

    n = recompute_series(supabase, touched, dry_run=args.dry_run)
    print(f"[結果] {counts} / 集計を更新したシリーズ {n}件" + (" (dry-run)" if args.dry_run else ""))
    if not args.dry_run:
        # 付け替えで巻が1冊もなくなったシリーズは、消さずに一覧だけ出す (削除は人が確認してから)
        empty = [sid for sid in sorted(touched)
                 if not supabase.table("openbd_test_books").select("isbn").eq("series_id", sid).limit(1).execute().data]
        if empty:
            print(f"[空のシリーズ] 巻が無くなったシリーズ {len(empty)}件 (削除の候補): {empty}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="未所属の新しい本をシリーズに振り分ける (判定は Claude)")
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("export")
    p.add_argument("--isbns-file", help="付け替え用: この ISBN の一覧 (1行1件) を、所属済みでも判定し直す")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--lookback-days", type=int, default=365, help="未所属の本を、何日前の発売まで判定の対象にするか")
    p.add_argument("--out", required=True)
    p = sub.add_parser("apply")
    p.add_argument("--isbns-file", help="付け替え用: export と同じ一覧を渡す")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--lookback-days", type=int, default=365)
    p.add_argument("--file", required=True)
    p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    export(args) if args.mode == "export" else apply(args)
