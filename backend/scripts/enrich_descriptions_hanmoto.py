"""
openbd_test_books の description が truncate されている (...で終わる/短すぎる) 行を
hanmoto.com から再取得して完全版に置き換える。

背景:
  以前は hanmoto の og:description (= SNSシェア用、約100文字で truncate) を
  保存していた。新コードは <p itemprop="description"> から完全版 (300〜500文字)
  を取得するようになったので、過去の truncate データを再 fetch して上書きする。

使い方:
  python -m scripts.enrich_descriptions_hanmoto --dry-run        # 対象確認のみ
  python -m scripts.enrich_descriptions_hanmoto --dry-run --limit 20
  python -m scripts.enrich_descriptions_hanmoto                  # 全件再取得
  python -m scripts.enrich_descriptions_hanmoto --imprint 電撃文庫
  python -m scripts.enrich_descriptions_hanmoto --concurrency 10
"""
import sys
import os
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase
from scripts.discover_openbd import fetch_hanmoto_metadata, HANMOTO_CONCURRENCY


def _is_truncated(desc: str | None) -> bool:
    """description が truncate されているっぽいか判定。
    - 末尾が … (U+2026) または ... → truncate サイン
    - 全体が <= 130文字 → og:description の典型的な長さ
    """
    if not desc:
        return False
    s = desc.strip()
    if s.endswith('…') or s.endswith('...') or s.endswith('...'):
        return True
    if len(s) <= 130:
        return True
    return False


def fetch_target_rows(supabase, imprint: str | None, limit: int | None,
                      page_size: int = 1000) -> list[dict]:
    """description が truncate されていそうな行を取得"""
    rows: list[dict] = []
    offset = 0
    while True:
        q = (supabase.table("openbd_test_books")
             .select("isbn, title, imprint, description")
             .not_.is_("description", "null"))
        if imprint:
            q = q.eq("imprint", imprint)
        rs = q.range(offset, offset + page_size - 1).execute()
        if not rs.data:
            break
        for r in rs.data:
            if _is_truncated(r.get("description")):
                rows.append(r)
                if limit and len(rows) >= limit:
                    return rows
        if len(rs.data) < page_size:
            break
        offset += page_size
    return rows


def _fetch_one(isbn: str, retry: int = 1) -> tuple[str, str | None, str]:
    """並列実行用ワーカー: ISBN → (isbn, new_description, status)
    status: 'ok' / 'no_desc' / 'fetch_fail'
    transient エラーは retry 回まで再試行 (バックオフ短め)
    """
    import time as _t
    for attempt in range(retry + 1):
        try:
            meta = fetch_hanmoto_metadata(isbn)
        except Exception:
            meta = None
        if meta is None:
            if attempt < retry:
                _t.sleep(0.3 * (attempt + 1))
                continue
            return isbn, None, "fetch_fail"
        desc = meta.get("description")
        if not desc:
            return isbn, None, "no_desc"
        return isbn, desc, "ok"
    return isbn, None, "fetch_fail"


def main(imprint: str | None, limit: int | None, dry_run: bool, concurrency: int):
    supabase = get_supabase()
    print("[1/3] truncate されていそうな行を抽出中...")
    rows = fetch_target_rows(supabase, imprint, limit)
    print(f"  → {len(rows):,}件")
    if not rows:
        return

    # サンプル表示
    print("\n[sample] 抽出された truncate description 例:")
    for r in rows[:3]:
        d = r.get("description") or ""
        print(f"  [{r['isbn']}] ({len(d)}文字) {d[-100:]!r}")

    print(f"\n[2/3] hanmoto 再取得中 (並列 {concurrency}) ...")
    unchanged = 0          # 既存と完全一致 (更新不要)
    shorter = 0            # 新しい方が短い (情報減のため更新しない)
    fetch_failed = 0       # hanmoto アクセス自体が失敗
    no_desc = 0            # hanmoto に description 自体が無い
    new_descriptions: dict[str, str] = {}
    fetch_fail_samples: list[str] = []

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(_fetch_one, r['isbn']): r for r in rows}
        for idx, fut in enumerate(as_completed(futures), 1):
            row = futures[fut]
            try:
                isbn, new_desc, status = fut.result()
            except Exception as e:
                fetch_failed += 1
                if len(fetch_fail_samples) < 5:
                    fetch_fail_samples.append(f"{row['isbn']}: {e}")
                continue
            old_desc = (row.get("description") or "").strip()
            if status == "fetch_fail":
                fetch_failed += 1
                if len(fetch_fail_samples) < 5:
                    fetch_fail_samples.append(f"{isbn}: fetch_fail")
                continue
            if status == "no_desc":
                no_desc += 1
                continue
            new_desc = new_desc.strip()
            if new_desc == old_desc:
                unchanged += 1
                continue
            if len(new_desc) < len(old_desc):
                # 新しい方が短い → 既存を温存 (情報量重視)
                shorter += 1
                continue
            new_descriptions[isbn] = new_desc
            if idx <= 5 or idx % 100 == 0:
                print(f"  [{idx}/{len(rows)}] {isbn}: {len(old_desc)}文字 → {len(new_desc)}文字")

    print(f"\n  → 更新候補   {len(new_descriptions):,}")
    print(f"     同一       {unchanged}")
    print(f"     短くなる   {shorter}  (既存温存)")
    print(f"     desc なし  {no_desc}  (hanmoto 側に紹介文なし)")
    print(f"     取得失敗   {fetch_failed}")
    if fetch_fail_samples:
        print(f"     取得失敗 sample:")
        for s in fetch_fail_samples:
            print(f"       {s}")

    if dry_run or not new_descriptions:
        if dry_run:
            print("\n[DRY RUN] DB更新スキップ")
        return

    print(f"\n[3/3] DB 更新中 ({len(new_descriptions):,}件)...")
    saved = 0
    for isbn, desc in new_descriptions.items():
        try:
            supabase.table("openbd_test_books").update({
                "description": desc,
            }).eq("isbn", isbn).execute()
            saved += 1
        except Exception as e:
            print(f"  ! {isbn} 更新失敗: {e}")
    print(f"\n[完了] {saved:,}件のdescription を完全版に更新")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="openbd_test_books の truncate description を hanmoto から再取得")
    parser.add_argument("--imprint", default=None, help="レーベル絞り込み")
    parser.add_argument("--limit", type=int, default=None, help="処理件数上限 (試行用)")
    parser.add_argument("--dry-run", action="store_true", help="DB更新せず確認のみ")
    parser.add_argument("--concurrency", type=int, default=HANMOTO_CONCURRENCY,
                        help=f"hanmoto への並列接続数 (デフォルト {HANMOTO_CONCURRENCY})")
    args = parser.parse_args()
    main(imprint=args.imprint, limit=args.limit, dry_run=args.dry_run,
         concurrency=args.concurrency)
