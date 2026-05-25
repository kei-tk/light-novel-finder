"""
DBに未登録のISBNだけを対象に、openBDで「imprint候補が空」または
「openBDレコードが無い」書籍を hanmoto.com で再チェックして
対象レーベルなら openbd_test_books に追加する。

discover_openbd の hanmoto フォールバックパスを「未登録ISBN」だけに対して
もう一度回すイメージ。前回の実行で取りこぼしたものを救済できる。

使い方:
  python -m scripts.rediscover_hanmoto --dry-run
  python -m scripts.rediscover_hanmoto --imprint 電撃文庫
  python -m scripts.rediscover_hanmoto --imprint 電撃文庫 --hanmoto-max 100
  python -m scripts.rediscover_hanmoto --prefix 978404 --prefix 9784065
"""
import sys
import os
import time
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase
from scripts.discover_openbd import (
    fetch_coverage,
    filter_by_prefix,
    chunked,
    fetch_metadata_batch,
    fetch_hanmoto_record,
    _collect_imprint_candidates,
    BATCH_SIZE,
    SLEEP_BETWEEN_BATCH,
    SLEEP_BETWEEN_HANMOTO,
    DEFAULT_PREFIXES,
)


def fetch_existing_isbns(supabase) -> set[str]:
    """DBに既に登録されているISBNを全件取得"""
    print("[0/4] DBの既存ISBNを取得中...")
    existing: set[str] = set()
    page = 1000
    offset = 0
    while True:
        rows = (supabase.table("openbd_test_books")
                .select("isbn")
                .range(offset, offset + page - 1)
                .execute().data or [])
        if not rows:
            break
        for r in rows:
            if r.get("isbn"):
                existing.add(r["isbn"])
        if len(rows) < page:
            break
        offset += page
    print(f"  → 既存 {len(existing):,}件")
    return existing


def collect_unjudged_isbns(candidates: list[str], imprint: str) -> list[str]:
    """openBD で照会して、レコード無し or imprint候補が空 の ISBN を返す"""
    print(f"[2/4] openBD で {len(candidates):,}件を照会し、判定不能なものを抽出")
    unjudged: list[str] = []
    queried = 0
    for batch_idx, chunk in enumerate(chunked(candidates, BATCH_SIZE)):
        if batch_idx > 0:
            time.sleep(SLEEP_BETWEEN_BATCH)
        try:
            items = fetch_metadata_batch(chunk)
        except Exception as e:
            print(f"  ! batch {batch_idx+1} 取得失敗: {e} → 全件 hanmoto 行きに")
            unjudged.extend(chunk)
            continue
        queried += len(chunk)

        batch_unjudged = 0
        for req_isbn, it in zip(chunk, items):
            if not it:
                unjudged.append(req_isbn)
                batch_unjudged += 1
            elif not _collect_imprint_candidates(it):
                unjudged.append(req_isbn)
                batch_unjudged += 1
        if batch_unjudged:
            print(f"  [batch {batch_idx+1}] {len(chunk)}件中 判定不能 {batch_unjudged}件 "
                  f"(累計 {len(unjudged):,})")
    print(f"  → 判定不能 {len(unjudged):,}件 / 照会 {queried:,}件")
    return unjudged


def main(imprint: str, prefixes: list[str], dry_run: bool, hanmoto_max: int | None):
    supabase = get_supabase()

    existing = fetch_existing_isbns(supabase)

    print("\n[1/4] openBD coverage 取得")
    isbns = fetch_coverage()
    candidates = filter_by_prefix(isbns, prefixes)
    print(f"  → プレフィックス絞り込み後: {len(candidates):,}件")

    fresh = [i for i in candidates if i not in existing]
    skipped = len(candidates) - len(fresh)
    print(f"  → DB既存を除外: {skipped:,}件除外、対象 {len(fresh):,}件")
    if not fresh:
        print("  追加対象なし。終了")
        return

    unjudged = collect_unjudged_isbns(fresh, imprint)
    if not unjudged:
        print("  hanmoto で調べる対象なし。終了")
        return

    target_isbns = unjudged
    if hanmoto_max:
        target_isbns = target_isbns[:hanmoto_max]
        print(f"  → --hanmoto-max {hanmoto_max} 適用 → {len(target_isbns):,}件")

    print(f"\n[3/4] hanmoto.com で {len(target_isbns):,}件を確認 (imprint='{imprint}')")
    matched: list[dict] = []
    for idx, isbn in enumerate(target_isbns):
        if idx > 0:
            time.sleep(SLEEP_BETWEEN_HANMOTO)
        rec = fetch_hanmoto_record(isbn, target_imprint=imprint, verbose=False)
        if rec:
            matched.append(rec)
            print(f"  [{idx+1}/{len(target_isbns)}] match: {isbn} 「{rec['title']}」 "
                  f"(累計 {len(matched)})")

    print(f"\n[一致] {len(matched)}件 / hanmoto試行 {len(target_isbns):,}件")

    if dry_run:
        print("[DRY RUN] DB保存はスキップ")
        for r in matched[:30]:
            print(f"  - [{r['isbn']}] 「{r['title']}」 / {r['author']} / {r['published_date']}")
        return

    print("\n[4/4] 保存中...")
    saved = 0
    for rec in matched:
        try:
            supabase.table("openbd_test_books").upsert(rec, on_conflict="isbn").execute()
            saved += 1
        except Exception as e:
            print(f"  ! 保存失敗 {rec['isbn']}: {e}")
    print(f"\n[完了] {saved}件 追加保存")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="DB未登録ISBNのうち openBD で空/不明だったものを hanmoto.com で再チェック")
    parser.add_argument("--imprint", default="電撃文庫", help="対象レーベル名")
    parser.add_argument("--prefix", action="append", default=None,
                        help=f"ISBNプレフィックス（複数指定可、未指定で {DEFAULT_PREFIXES}）")
    parser.add_argument("--hanmoto-max", type=int, default=None,
                        help="hanmoto試行上限（指定なしで全件）")
    parser.add_argument("--dry-run", action="store_true", help="DBに保存しない")
    args = parser.parse_args()

    main(
        imprint=args.imprint,
        prefixes=args.prefix or DEFAULT_PREFIXES,
        dry_run=args.dry_run,
        hanmoto_max=args.hanmoto_max,
    )
