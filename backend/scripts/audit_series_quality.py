"""
openbd_test_series / openbd_test_books の品質監査スクリプト (read-only)。

チェック項目:
  A. 孤立 series       : 紐付け書籍 0冊
  B. 単巻 series       : is_series=true なのに紐付け書籍 1冊のみ
  C. エンリッチ未完了  : popularity_score IS NULL の series
  D. タグ欠落          : エンリッチ済みなのに atmosphere/genre/sub_genre tags が空
  E. スコア欠損        : popularity はあるが他の score が NULL
  F. 著者バラバラ      : 1 series 内で異なる primary author が混在 (誤統合疑い)
  G. タイトル乖離      : series_title と紐付け書籍タイトルの語彙重なりが極端に低い
  H. 重複 series       : 同名 series_title + author で複数行
  I. 孤立 books        : series_id が series テーブルに存在しない (FK破損)

使い方:
  python -m scripts.audit_series_quality                # 全件チェック
  python -m scripts.audit_series_quality --limit 20     # サンプル件数
  python -m scripts.audit_series_quality --only A,F     # 特定チェックのみ
"""
import sys
import os
import argparse
import re
import unicodedata
from collections import Counter, defaultdict
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase


def _paginate(query_factory, page_size: int = 1000) -> list[dict]:
    rows = []
    offset = 0
    while True:
        rs = query_factory().range(offset, offset + page_size - 1).execute()
        if not rs.data:
            break
        rows.extend(rs.data)
        if len(rs.data) < page_size:
            break
        offset += page_size
    return rows


def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").lower()


_TOKEN_RE = re.compile(r"[A-Za-z0-9ぁ-んァ-ヶ一-龯]+")


def _tokens(title: str) -> set[str]:
    """簡易トークン化 (英数 / ひらがな / カタカナ / 漢字の連続)"""
    return set(_TOKEN_RE.findall(_nfkc(title)))


def fetch_all(supabase):
    print("[全 series 取得]")
    series_rows = _paginate(lambda: supabase.table("openbd_test_series").select(
        "id, series_title, author, volume_count, is_series, parent_series_id, "
        "popularity_score, momentum_score, comedy_score, serious_score, "
        "heartwarming_score, bittersweet_score, romantic_score, uplifting_score, "
        "atmosphere_tags, genre_tags, sub_genre_tags"
    ))
    print(f"  → {len(series_rows):,}件")

    print("[全 books (series_id 持ち) 取得]")
    books = _paginate(lambda: supabase.table("openbd_test_books").select(
        "isbn, title, series_id, contributors, attached_to_isbn, volume_number"
    ).not_.is_("series_id", "null"))
    print(f"  → {len(books):,}件")

    by_sid: dict[str, list[dict]] = defaultdict(list)
    for b in books:
        by_sid[b["series_id"]].append(b)
    return series_rows, books, by_sid


def _primary_author(book: dict) -> str | None:
    """contributors から最初の A01 名を返す"""
    contribs = book.get("contributors") or []
    for c in contribs:
        if isinstance(c, dict) and c.get("role") == "A01":
            return _nfkc(c.get("name") or "")
    return None


def check_a_orphan(series_rows, by_sid):
    """A. 紐付け書籍 0冊"""
    print("\n========== A. 孤立 series (書籍 0冊) ==========")
    orphans = [s for s in series_rows if not by_sid.get(s["id"])]
    print(f"件数: {len(orphans):,} / {len(series_rows):,}")
    return orphans


def check_b_singleton_marked_series(series_rows, by_sid):
    """B. is_series=true なのに 1冊しかない"""
    print("\n========== B. 単巻なのに is_series=true ==========")
    singles = [s for s in series_rows
               if s.get("is_series") and len(by_sid.get(s["id"], [])) == 1]
    print(f"件数: {len(singles):,}")
    return singles


def check_c_not_enriched(series_rows, by_sid):
    """C. popularity_score IS NULL (未エンリッチ)"""
    print("\n========== C. 未エンリッチ (popularity_score NULL) ==========")
    # 子シリーズ (parent_series_id 持ち) は親に集約されているのでスキップ
    not_enriched = [s for s in series_rows
                    if s.get("popularity_score") is None
                    and not s.get("parent_series_id")
                    and len(by_sid.get(s["id"], [])) > 0]
    print(f"件数: {len(not_enriched):,} (親シリーズかつ書籍ありのもの)")
    return not_enriched


def check_d_missing_tags(series_rows):
    """D. popularity あるのにタグが全部空"""
    print("\n========== D. エンリッチ済みだがタグ欠落 ==========")
    bad = []
    for s in series_rows:
        if s.get("popularity_score") is None:
            continue
        at = s.get("atmosphere_tags") or []
        gt = s.get("genre_tags") or []
        st = s.get("sub_genre_tags") or []
        if not at and not gt and not st:
            bad.append(s)
    print(f"件数: {len(bad):,}")
    return bad


def check_e_partial_scores(series_rows):
    """E. 一部スコアが NULL (popularity ある前提)"""
    print("\n========== E. スコア部分欠損 (popularity 以外 NULL) ==========")
    bad = []
    score_fields = ["comedy_score", "serious_score", "heartwarming_score",
                    "bittersweet_score", "romantic_score", "uplifting_score"]
    for s in series_rows:
        if s.get("popularity_score") is None:
            continue
        missing = [f for f in score_fields if s.get(f) is None]
        if missing:
            bad.append((s, missing))
    print(f"件数: {len(bad):,}")
    return bad


def check_f_author_mismatch(series_rows, by_sid):
    """F. 1 series 内で異なる primary author が混在"""
    print("\n========== F. 著者バラバラ series (誤統合疑い) ==========")
    bad = []
    for s in series_rows:
        books = by_sid.get(s["id"], [])
        if len(books) < 2:
            continue
        authors = Counter()
        for b in books:
            a = _primary_author(b)
            if a:
                authors[a] += 1
        if len(authors) >= 2:
            # series.author と一致しない著者が複数いる場合
            top2 = authors.most_common(2)
            bad.append((s, top2, len(books)))
    bad.sort(key=lambda x: -x[2])
    print(f"件数: {len(bad):,}")
    return bad


def check_g_title_divergence(series_rows, by_sid):
    """G. series_title と本のタイトルでトークン重なり 0"""
    print("\n========== G. タイトル乖離 (series ↔ 書籍タイトル無関連) ==========")
    bad = []
    for s in series_rows:
        title = s.get("series_title") or ""
        if not title:
            continue
        s_toks = _tokens(title)
        if not s_toks:
            continue
        books = by_sid.get(s["id"], [])
        if not books:
            continue
        # 本のタイトル群を結合して比較
        book_toks: set[str] = set()
        for b in books:
            book_toks |= _tokens(b.get("title") or "")
        if not book_toks:
            continue
        overlap = s_toks & book_toks
        # 完全に乖離 (重なり 0)
        if not overlap:
            bad.append((s, len(books)))
    print(f"件数: {len(bad):,}")
    return bad


def check_h_duplicate_series(series_rows):
    """H. 同 series_title + author で複数 series 行"""
    print("\n========== H. 重複 series (同名+同著者) ==========")
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for s in series_rows:
        key = (_nfkc(s.get("series_title") or ""), _nfkc(s.get("author") or ""))
        if key[0]:
            by_key[key].append(s)
    dups = {k: v for k, v in by_key.items() if len(v) > 1}
    total = sum(len(v) for v in dups.values())
    print(f"件数: {len(dups):,}グループ / 合計 {total:,}行")
    return dups


def check_i_fk_broken_books(supabase, series_rows):
    """I. books の series_id が series テーブルに存在しない"""
    print("\n========== I. FK 破損 books (series_id 不整合) ==========")
    valid_ids = {s["id"] for s in series_rows}
    print("[全 books series_id 取得 (FK チェック用)]")
    books = _paginate(lambda: supabase.table("openbd_test_books").select(
        "isbn, title, series_id"
    ).not_.is_("series_id", "null"))
    bad = [b for b in books if b["series_id"] not in valid_ids]
    print(f"件数: {len(bad):,} / 全 {len(books):,}件")
    return bad


def print_samples(label: str, items: list, formatter, limit: int):
    if not items:
        return
    print(f"\n  -- sample {min(limit, len(items))}件 --")
    for x in items[:limit]:
        print(f"  {formatter(x)}")


def main(args):
    supabase = get_supabase()
    series_rows, books, by_sid = fetch_all(supabase)
    only = set([x.strip().upper() for x in args.only.split(",")]) if args.only else None

    def should_run(key: str) -> bool:
        return only is None or key in only

    summary = {}

    if should_run("A"):
        orphans = check_a_orphan(series_rows, by_sid)
        summary["A 孤立 series"] = len(orphans)
        print_samples("A", orphans,
            lambda s: f"[{s['id'][:8]}] {(s.get('series_title') or '')[:50]!r} 著={s.get('author')!r} vc={s.get('volume_count')}",
            args.limit)

    if should_run("B"):
        singles = check_b_singleton_marked_series(series_rows, by_sid)
        summary["B 単巻 is_series=true"] = len(singles)
        print_samples("B", singles,
            lambda s: f"[{s['id'][:8]}] {(s.get('series_title') or '')[:50]!r} 著={s.get('author')!r}",
            args.limit)

    if should_run("C"):
        not_enriched = check_c_not_enriched(series_rows, by_sid)
        summary["C 未エンリッチ"] = len(not_enriched)
        print_samples("C", not_enriched,
            lambda s: f"[{s['id'][:8]}] {(s.get('series_title') or '')[:50]!r} 著={s.get('author')!r} vc={s.get('volume_count')}",
            args.limit)

    if should_run("D"):
        bad = check_d_missing_tags(series_rows)
        summary["D タグ欠落"] = len(bad)
        print_samples("D", bad,
            lambda s: f"[{s['id'][:8]}] {(s.get('series_title') or '')[:50]!r} pop={s.get('popularity_score')}",
            args.limit)

    if should_run("E"):
        bad = check_e_partial_scores(series_rows)
        summary["E スコア部分欠損"] = len(bad)
        print_samples("E", bad,
            lambda x: f"[{x[0]['id'][:8]}] {(x[0].get('series_title') or '')[:40]!r} 欠損={x[1]}",
            args.limit)

    if should_run("F"):
        bad = check_f_author_mismatch(series_rows, by_sid)
        summary["F 著者バラバラ"] = len(bad)
        print_samples("F", bad,
            lambda x: f"[{x[0]['id'][:8]}] {(x[0].get('series_title') or '')[:40]!r} 本数={x[2]} 上位著者={x[1]}",
            args.limit)

    if should_run("G"):
        bad = check_g_title_divergence(series_rows, by_sid)
        summary["G タイトル乖離"] = len(bad)
        print_samples("G", bad,
            lambda x: f"[{x[0]['id'][:8]}] {(x[0].get('series_title') or '')[:50]!r} 本数={x[1]}",
            args.limit)

    if should_run("H"):
        dups = check_h_duplicate_series(series_rows)
        summary["H 重複 series"] = sum(len(v) for v in dups.values())
        if dups:
            print(f"\n  -- sample {min(args.limit, len(dups))}グループ --")
            for key, rows in list(dups.items())[:args.limit]:
                title, author = key
                print(f"  {title!r} 著={author!r} → {len(rows)}行")
                for r in rows[:3]:
                    print(f"    - [{r['id'][:8]}] vc={r.get('volume_count')} pop={r.get('popularity_score')}")

    if should_run("I"):
        bad = check_i_fk_broken_books(supabase, series_rows)
        summary["I FK 破損 books"] = len(bad)
        print_samples("I", bad,
            lambda b: f"[{b['isbn']}] {(b.get('title') or '')[:50]!r} sid={b['series_id'][:8]}",
            args.limit)

    print("\n\n========== サマリ ==========")
    for k, v in summary.items():
        marker = "⚠️ " if v > 0 else "✓ "
        print(f"  {marker}{k}: {v:,}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="series 品質監査 (read-only)")
    parser.add_argument("--limit", type=int, default=10, help="各チェックのサンプル表示件数")
    parser.add_argument("--only", type=str, help="特定チェックのみ (例: A,F,H)")
    args = parser.parse_args()
    main(args)
