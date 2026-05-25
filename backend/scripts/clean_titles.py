"""
DBに保存されたタイトルから著者名混入を除去する。

原因: 旧 _HANMOTO_OG_TITLE_RE が "author=[^ ]+" (空白なし1単語) で著者を
取っていたため、空白付き著者名 (例: "芝村 裕吏") の苗字部分がタイトル末尾に残っていた。

このスクリプトは contributors 列の各 name を title 末尾から削除する。
存在する全ての (空白+name + 任意の役割括弧) パターンを試行。

books と series 両方のテーブルを対象にする (デフォルトは両方)。

使い方:
  python -m scripts.clean_titles --dry-run                 # books + series 両方を確認
  python -m scripts.clean_titles --target books            # books だけ
  python -m scripts.clean_titles --target series           # series だけ
  python -m scripts.clean_titles --imprint 電撃文庫         # books の imprint絞り込み
  python -m scripts.clean_titles                           # 本番 (両方)
"""
import sys
import os
import re
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase


# title 末尾の "{sep}{name}{role_paren?}" を取り除くためのセパレータ・括弧・役割パターン
_SEP_PATTERN = r'[\s、,，/／]+'
_PAREN_OPTIONAL = r'(?:[\(（][^)）]+[\)）]|[\[【][^\]】]+[\]】])?'


def clean_title(title: str | None, contributors: list[dict] | None) -> str | None:
    """contributors の name を title 末尾から削除して返す。
    元の title と変化なければ同じ文字列を返す。"""
    if not title or not contributors:
        return title
    cleaned = title
    # 長い name を優先 (短い name が他の長い name の prefix である場合の誤動作防止)
    names = sorted(
        {c.get('name', '').strip() for c in contributors if c.get('name')},
        key=len, reverse=True,
    )
    # フルネーム + 苗字候補 (空白で区切れる場合) + 名前候補
    # 1文字の苗字 (桜, 林, 森 等) も対象。_SEP_PATTERN で前に区切り必須なので
    # タイトル本文の途中の1文字とは衝突しない。
    candidates: list[str] = []
    seen = set()
    for n in names:
        if n and n not in seen:
            candidates.append(n)
            seen.add(n)
        # 空白区切りの各 token を候補に追加 (苗字 + 名前)
        if ' ' in n or '　' in n:
            for tok in re.split(r'[\s　]+', n):
                tok = tok.strip()
                if tok and tok not in seen:
                    candidates.append(tok)
                    seen.add(tok)
    # 長い候補から先に試行 (短い候補が長い候補の prefix である場合の誤動作防止)
    candidates.sort(key=len, reverse=True)
    # 末尾削除を反復実行
    changed = True
    while changed:
        changed = False
        for cand in candidates:
            pat = re.compile(_SEP_PATTERN + re.escape(cand) + _PAREN_OPTIONAL + r'\s*$')
            new = pat.sub('', cleaned)
            if new != cleaned:
                cleaned = new.rstrip(' \t、,。:：/／.')
                changed = True
    return cleaned or title


def _paginate(query_factory, page_size: int = 1000) -> list[dict]:
    """Supabase の range pagination を全件取得まで繰り返す。"""
    out: list[dict] = []
    offset = 0
    while True:
        q = query_factory()
        rs = q.range(offset, offset + page_size - 1).execute()
        if not rs.data:
            break
        out.extend(rs.data)
        if len(rs.data) < page_size:
            break
        offset += page_size
    return out


def fetch_books(supabase, imprint: str | None = None) -> list[dict]:
    print("[books取得]")
    def factory():
        q = supabase.table('openbd_test_books').select('isbn, title, contributors')
        if imprint:
            q = q.eq('imprint', imprint)
        return q
    rows = _paginate(factory)
    print(f"  → {len(rows):,}件")
    return rows


def fetch_series(supabase, imprint: str | None = None) -> list[dict]:
    print("[series取得]")
    def factory():
        # NOT NULL 列 (author, normalized_title) も同時に取得する。
        # upsert は INSERT を試みるため、これらを省略すると 23502 violation になる。
        q = supabase.table('openbd_test_series').select(
            'id, series_title, author, normalized_title, contributors')
        if imprint:
            q = q.eq('imprint', imprint)
        return q
    rows = _paginate(factory)
    print(f"  → {len(rows):,}件")
    return rows


def _process(rows: list[dict], title_field: str, key_field: str) -> list[dict]:
    """rows[*][title_field] を clean_title で整形した更新差分を返す。"""
    updates: list[dict] = []
    for r in rows:
        old = r.get(title_field)
        new = clean_title(old, r.get('contributors'))
        if new and new != old:
            updates.append({key_field: r[key_field], title_field: new, '_old': old})
    return updates


def _show_samples(updates: list[dict], title_field: str, key_field: str, limit: int) -> None:
    if not updates:
        return
    print(f"\n[サンプル {min(limit, len(updates))}件]")
    for u in updates[:limit]:
        print(f"  [{u[key_field]}]")
        print(f"    旧: {u['_old']}")
        print(f"    新: {u[title_field]}")


def _save_batch(supabase, table: str, records: list[dict],
                conflict_key: str, batch_size: int) -> int:
    saved = 0
    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        try:
            supabase.table(table).upsert(batch, on_conflict=conflict_key).execute()
            saved += len(batch)
            print(f"  batch {i // batch_size + 1}: +{len(batch)} (累計 {saved})")
        except Exception as e:
            print(f"  ! batch {i // batch_size + 1} 失敗: {e}")
    return saved


def process_books(supabase, imprint: str | None, dry_run: bool,
                  limit: int, batch_size: int) -> int:
    print("\n" + "=" * 60)
    print("[openbd_test_books クリーンアップ]")
    print("=" * 60)
    rows = fetch_books(supabase, imprint=imprint)
    updates = _process(rows, title_field='title', key_field='isbn')
    print(f"  → 変更対象 {len(updates):,}件 / 全{len(rows):,}件")
    _show_samples(updates, 'title', 'isbn', limit)
    if dry_run or not updates:
        if dry_run:
            print("[DRY RUN] DB更新はスキップ")
        return 0
    print(f"\n[DB更新中... batch_size={batch_size}]")
    records = [{'isbn': u['isbn'], 'title': u['title']} for u in updates]
    return _save_batch(supabase, 'openbd_test_books', records, 'isbn', batch_size)


def process_series(supabase, imprint: str | None, dry_run: bool,
                   limit: int, batch_size: int) -> int:
    print("\n" + "=" * 60)
    print("[openbd_test_series クリーンアップ]")
    print("=" * 60)
    rows = fetch_series(supabase, imprint=imprint)
    # row → updates (旧title, 新title, NOT NULL列含む)
    by_id = {r['id']: r for r in rows}
    updates: list[dict] = []
    for r in rows:
        old = r.get('series_title')
        new = clean_title(old, r.get('contributors'))
        if new and new != old:
            updates.append({
                'id': r['id'],
                'series_title': new,
                'author': r['author'],                  # NOT NULL
                'normalized_title': r['normalized_title'],  # NOT NULL
                '_old': old,
            })
    print(f"  → 変更対象 {len(updates):,}件 / 全{len(rows):,}件")
    _show_samples(updates, 'series_title', 'id', limit)
    if dry_run or not updates:
        if dry_run:
            print("[DRY RUN] DB更新はスキップ")
        return 0
    print(f"\n[DB更新中... batch_size={batch_size}]")
    records = [{
        'id': u['id'],
        'series_title': u['series_title'],
        'author': u['author'],
        'normalized_title': u['normalized_title'],
    } for u in updates]
    return _save_batch(supabase, 'openbd_test_series', records, 'id', batch_size)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', choices=['books', 'series', 'both'], default='both',
                        help='対象テーブル (デフォルト: both)')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--imprint', default=None,
                        help='レーベル絞り込み (両テーブルで適用)')
    parser.add_argument('--limit', type=int, default=20,
                        help='dry-run 時に表示する例の数')
    parser.add_argument('--batch-size', type=int, default=500)
    args = parser.parse_args()

    supabase = get_supabase()

    total_saved = 0
    if args.target in ('books', 'both'):
        total_saved += process_books(supabase, args.imprint, args.dry_run,
                                     args.limit, args.batch_size)
    if args.target in ('series', 'both'):
        total_saved += process_series(supabase, args.imprint, args.dry_run,
                                      args.limit, args.batch_size)

    if not args.dry_run:
        print(f"\n[完了] 合計 {total_saved:,}件 のタイトルをクリーンアップ")


if __name__ == '__main__':
    main()
