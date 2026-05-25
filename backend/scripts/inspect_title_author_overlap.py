"""
DBのタイトルに著者名（苗字/フルネーム）が混入している件を診断する。

何が原因か特定する用:
  - openBD由来の古いデータ (タイトルに " : " 著者情報を含む)
  - hanmoto regex の取り漏らし
  - その他

  python -m scripts.inspect_title_author_overlap
  python -m scripts.inspect_title_author_overlap --imprint 電撃文庫
  python -m scripts.inspect_title_author_overlap --limit 50
"""
import sys
import os
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase


def _split_author_names(author: str) -> list[str]:
    """著者列を個別の名前に分解。
    "鎌池和馬 / はいむらきよたか" のような hanmoto形式と、
    "鎌池,和馬,1979-" のようなopenBD形式の両方に対応。"""
    if not author:
        return []
    names: list[str] = []
    for chunk in author.split('/'):
        chunk = chunk.strip()
        if not chunk:
            continue
        # openBD: "苗字,名前,生年-" → "苗字名前"
        if ',' in chunk:
            parts = [p.strip() for p in chunk.split(',') if p.strip()]
            # 生年(数字-)を除く
            parts = [p for p in parts if not (p and (p[0].isdigit() or p[-1] == '-'))]
            chunk = ''.join(parts)
        if chunk:
            names.append(chunk)
    return names


def _surname_candidates(name: str) -> list[str]:
    """フルネームから苗字候補を出す (空白区切り or 先頭2文字)"""
    cands: list[str] = []
    name = name.strip()
    if not name:
        return cands
    # 空白あり: 最初のチャンクが苗字
    parts = name.split()
    if len(parts) >= 2:
        cands.append(parts[0])
    # 先頭2文字 (日本人苗字は2文字が大半)
    if len(name) >= 2:
        cands.append(name[:2])
    # フルネームそのものも候補
    cands.append(name)
    # uniq, length>=2
    seen, out = set(), []
    for c in cands:
        if len(c) >= 2 and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--imprint', default=None)
    parser.add_argument('--limit', type=int, default=30)
    args = parser.parse_args()

    supabase = get_supabase()
    print("[1/2] DB から書籍取得中...")
    all_books: list[dict] = []
    offset = 0
    while True:
        q = supabase.table('openbd_test_books').select('isbn, title, author, raw')
        if args.imprint:
            q = q.eq('imprint', args.imprint)
        rs = q.range(offset, offset + 999).execute()
        if not rs.data:
            break
        all_books.extend(rs.data)
        if len(rs.data) < 1000:
            break
        offset += 1000
    print(f"  → {len(all_books):,}件")

    print("\n[2/2] タイトル中の著者名混入を検出中...")
    hits: list[dict] = []
    no_author = 0
    for b in all_books:
        title = (b.get('title') or '').strip()
        author = (b.get('author') or '').strip()
        if not title or not author:
            no_author += 1
            continue
        names = _split_author_names(author)
        for name in names:
            for cand in _surname_candidates(name):
                if cand in title:
                    hits.append({
                        'isbn': b['isbn'],
                        'title': title,
                        'author': author,
                        'matched': cand,
                        'source': (b.get('raw') or {}).get('source', '?'),
                    })
                    break
            else:
                continue
            break

    print(f"  → 検出: {len(hits):,}件 / 著者なしスキップ: {no_author}")
    if not hits:
        return

    # ソース別の内訳
    from collections import Counter
    by_source = Counter(h['source'] for h in hits)
    print(f"\n[ソース別内訳] {dict(by_source)}")

    # 上位パターン (タイトル末尾にどう入っているかで分類)
    print(f"\n[サンプル {min(args.limit, len(hits))}件]")
    for h in hits[:args.limit]:
        print(f"\n  [{h['isbn']}] source={h['source']}")
        print(f"    title:   {h['title']}")
        print(f"    author:  {h['author']}")
        print(f"    matched: {h['matched']!r}")


if __name__ == '__main__':
    main()
