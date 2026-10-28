"""
発売日が「今日の前後 N 日 (既定 30日)」の書籍を、openBD と版元ドットコムから取り直す (毎週金曜のタスク)。

予約段階の書誌は発売日・あらすじ・書影があとから変わるので、期間内の本はすべて取り直す。
期間の外の本 (既刊) には触れない。

流れ:
  1. 期間内にある既存の本 (DB) を集める
  2. openBD の coverage から、まだ DB にない ISBN を探す (主要レーベルの出版社プレフィックスだけ)
     → /v1/get で発売日が期間内のものに絞り、レーベル名が主要レーベルに一致するものを新刊とする
     → openBD でレーベルが分からないものは、版元ドットコムのレーベル表記で確かめる
  3. 期間内の本すべてについて版元ドットコムを読み、著者・イラスト・あらすじ・発売日・書影を更新する
  4. 新刊は追加、既存は変わった列だけを更新する (タイトルは clean_titles 済みなので上書きしない)

レーベルが一致しなかった ISBN は cache/refresh_window/rejected_isbns.txt に覚え、次回から問い合わせない。

使い方:
  python -m scripts.refresh_window_books --dry-run        # 件数と変更点だけ表示
  python -m scripts.refresh_window_books                  # DB に反映
  python -m scripts.refresh_window_books --days 30 --skip-discover   # 既存の本の取り直しだけ
"""
import sys
import os
import time
import json
import argparse
from concurrent.futures import ThreadPoolExecutor
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase
from app.services.pipeline_common import paginate, window
from scripts.collect_all_labels import LIGHT_NOVEL_LABELS
from scripts.discover_openbd import (
    BATCH_SIZE,
    SLEEP_BETWEEN_BATCH,
    extract_record,
    fetch_coverage,
    fetch_hanmoto_metadata,
    fetch_metadata_batch,
    matches_imprint,
    _collect_imprint_candidates,
    _imprint_norm,
    _make_record_from_meta,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(BASE_DIR, "cache", "refresh_window")
REJECTED_FILE = os.path.join(STATE_DIR, "rejected_isbns.txt")
HANMOTO_WORKERS = 4  # 版元ドットコムへの同時接続。既存の収集 (5) より控えめにする
NEIGHBOR_BACK = 5      # 近くの番号を探す幅。新刊はあとの番号に付くことが多いので、後ろを広めにとる
NEIGHBOR_FORWARD = 10

# 既存の本で取り直す列。title / imprint は整形済みの値を守るため対象外
UPDATABLE = ["author", "illustrator", "contributors", "publisher", "published_date", "cover_image_url", "description"]


def _label_of(item: dict) -> str | None:
    for label in LIGHT_NOVEL_LABELS:
        if matches_imprint(item, label["name"]):
            return label["name"]
    return None


def _hanmoto_label_match(label: str | None) -> str | None:
    if not label:
        return None
    for lb in LIGHT_NOVEL_LABELS:
        if _imprint_norm(lb["name"]) in _imprint_norm(label):
            return lb["name"]
    return None


def _load_rejected() -> set[str]:
    if not os.path.exists(REJECTED_FILE):
        return set()
    with open(REJECTED_FILE, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def _save_rejected(isbns: set[str]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(REJECTED_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(isbns)))


def discover_new(supabase, lo: str, hi: str, existing: set[str], dry_run: bool) -> dict[str, dict]:
    """まだ DB にない、期間内の主要レーベルの本を openBD から探す。isbn → openBD のレコード"""
    prefixes = tuple(sorted({p for lb in LIGHT_NOVEL_LABELS for p in lb["prefixes"]}))
    rejected = _load_rejected()
    coverage = fetch_coverage()
    candidates = [i for i in coverage if i and i.startswith(prefixes) and i not in existing and i not in rejected]
    print(f"[新刊を探す] プレフィックス一致・DB 未登録・除外リスト外: {len(candidates):,}件")

    found: dict[str, dict] = {}
    unknown_label: dict[str, dict] = {}
    newly_rejected: set[str] = set()
    for start in range(0, len(candidates), BATCH_SIZE):
        chunk = candidates[start:start + BATCH_SIZE]
        try:
            items = fetch_metadata_batch(chunk)
        except Exception as e:
            print(f"  ! openBD 取得失敗 ({start}〜): {e}")
            continue
        for item in items:
            if not item:
                continue
            rec = extract_record(item)
            if not rec or not rec["isbn"]:
                continue
            label = _label_of(item)
            pub = rec.get("published_date")
            if label is None and _collect_imprint_candidates(item):
                # openBD にレーベル名があり、主要レーベルでない → 二度と問い合わせない
                # (既存の本1,000冊での確認では、取りこぼしは 0.6%。英字表記などのレーベル)
                newly_rejected.add(rec["isbn"])
                continue
            if not pub or not (lo <= pub <= hi):
                continue  # 期間外の本は、発売日が近づいた週にもう一度見る
            if label:
                rec["imprint"] = label
                found[rec["isbn"]] = rec
            else:
                unknown_label[rec["isbn"]] = rec
        if start // BATCH_SIZE % 20 == 0:
            print(f"  … {min(start + BATCH_SIZE, len(candidates)):,}/{len(candidates):,} (新刊 {len(found)}件)")
        time.sleep(SLEEP_BETWEEN_BATCH)

    # openBD でレーベルが分からない期間内の本は、版元ドットコムのレーベル表記で確かめる
    if unknown_label:
        print(f"[新刊を探す] レーベル不明 {len(unknown_label)}件を版元ドットコムで確認")
        with ThreadPoolExecutor(max_workers=HANMOTO_WORKERS) as ex:
            metas = dict(zip(unknown_label, ex.map(fetch_hanmoto_metadata, unknown_label)))
        for isbn, meta in metas.items():
            label = _hanmoto_label_match((meta or {}).get("label"))
            if label:
                found[isbn] = {**unknown_label[isbn], "imprint": label}
            elif meta and meta.get("label"):
                newly_rejected.add(isbn)

    if newly_rejected and not dry_run:
        _save_rejected(rejected | newly_rejected)
    print(f"[新刊を探す] 主要レーベル外として除外: {len(newly_rejected):,}件" + (" (dry-run なので保存しない)" if dry_run else ""))
    print(f"[新刊を探す] openBD で見つかった期間内の新刊: {len(found)}件")
    return found


def _isbn13(body12: str) -> str:
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(body12))
    return body12 + str((10 - total % 10) % 10)


def discover_by_neighbors(seeds: list[str], skip: set[str], lo: str, hi: str) -> dict[str, dict]:
    """openBD に載る前の新刊を、版元ドットコムで探す。
    出版社は ISBN を順に振るので、期間内の本の近くの番号 (前 NEIGHBOR_BACK・後ろ NEIGHBOR_FORWARD) を確かめる"""
    probes: set[str] = set()
    for isbn in seeds:
        base = int(isbn[:12])
        for d in range(-NEIGHBOR_BACK, NEIGHBOR_FORWARD + 1):
            if d:
                probes.add(_isbn13(str(base + d)))
    # set のままだと zip の順と map の順がずれるので、並べた一覧を両方に使う
    probe_list = sorted(probes - skip)
    print(f"[新刊を探す] 版元ドットコムで近くの番号を確認: {len(probe_list)}件")
    with ThreadPoolExecutor(max_workers=HANMOTO_WORKERS) as ex:
        metas = dict(zip(probe_list, ex.map(fetch_hanmoto_metadata, probe_list)))
    found: dict[str, dict] = {}
    for isbn, meta in metas.items():
        label = _hanmoto_label_match((meta or {}).get("label"))
        pub = (meta or {}).get("published_date")
        if label and pub and lo <= pub <= hi:
            found[isbn] = {**_make_record_from_meta(isbn, meta), "imprint": label}
    print(f"[新刊を探す] 版元ドットコムで見つかった期間内の新刊: {len(found)}件")
    return found


def merge_hanmoto(rec: dict, meta: dict | None) -> dict:
    """版元ドットコムの値を優先する (あらすじは完全版、発売日は書店発売日、著者は役職つきで取れるため)"""
    if not meta:
        return rec
    out = dict(rec)
    for key in ["author", "illustrator", "contributors", "publisher", "published_date", "cover_image_url"]:
        if meta.get(key):
            out[key] = meta[key]
    desc = meta.get("description")
    if desc and len(desc) >= len(out.get("description") or ""):
        out["description"] = desc
    return out


def main(args) -> None:
    supabase = get_supabase()
    lo, hi = window(args.days)
    print(f"[期間] 発売日 {lo} 〜 {hi}")

    current = paginate(lambda: supabase.table("openbd_test_books").select("isbn," + ",".join(UPDATABLE))
                       .gte("published_date", lo).lte("published_date", hi))
    print(f"[既存の本] 期間内 {len(current)}件")

    new_records: dict[str, dict] = {}
    if not args.skip_discover:
        existing = {r["isbn"] for r in paginate(lambda: supabase.table("openbd_test_books").select("isbn"))}
        new_records = discover_new(supabase, lo, hi, existing, args.dry_run)
        seeds = [r["isbn"] for r in current] + list(new_records)
        skip = existing | set(new_records) | _load_rejected()
        for isbn, rec in discover_by_neighbors(seeds, skip, lo, hi).items():
            new_records.setdefault(isbn, rec)

    # 既存の本は openBD も取り直す (発売日の変更は openBD 側が先に変わることがある)
    openbd_now: dict[str, dict] = {}
    isbns = [r["isbn"] for r in current]
    for start in range(0, len(isbns), BATCH_SIZE):
        for item in fetch_metadata_batch(isbns[start:start + BATCH_SIZE]):
            rec = extract_record(item) if item else None
            if rec and rec["isbn"]:
                openbd_now[rec["isbn"]] = rec
        time.sleep(SLEEP_BETWEEN_BATCH)

    # 近くの番号から見つけた新刊は、すでに版元ドットコムの値でできているので取り直さない
    targets = [i for i, r in new_records.items() if (r.get("raw") or {}).get("source") != "hanmoto"] + isbns
    print(f"[版元ドットコム] {len(targets)}件を取得 (同時 {HANMOTO_WORKERS})")
    with ThreadPoolExecutor(max_workers=HANMOTO_WORKERS) as ex:
        hanmoto = dict(zip(targets, ex.map(fetch_hanmoto_metadata, targets)))

    inserted = updated = unchanged = 0
    for isbn, rec in new_records.items():
        row = merge_hanmoto(rec, hanmoto.get(isbn))
        row = {k: v for k, v in row.items() if v is not None}
        if args.dry_run:
            print(f"  + {isbn} {row.get('published_date')} {row.get('imprint')} {row.get('title')}")
        else:
            supabase.table("openbd_test_books").upsert(row, on_conflict="isbn").execute()
        inserted += 1

    for old in current:
        isbn = old["isbn"]
        fresh = merge_hanmoto(openbd_now.get(isbn, {}), hanmoto.get(isbn))
        # 取れなかった値で既存の値を消さない
        change = {k: fresh[k] for k in UPDATABLE if fresh.get(k) not in (None, "", []) and fresh.get(k) != old.get(k)}
        if not change:
            unchanged += 1
            continue
        if args.dry_run:
            print(f"  ~ {isbn} {sorted(change)}" + (f" 発売日 {old.get('published_date')} → {change['published_date']}" if "published_date" in change else ""))
        else:
            supabase.table("openbd_test_books").update(change).eq("isbn", isbn).execute()
        updated += 1

    os.makedirs(STATE_DIR, exist_ok=True)
    summary = {"window": [lo, hi], "inserted": inserted, "updated": updated, "unchanged": unchanged,
               "new_isbns": sorted(new_records), "dry_run": args.dry_run}
    with open(os.path.join(STATE_DIR, "last_run.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"[結果] 追加 {inserted} / 更新 {updated} / 変更なし {unchanged}" + (" (dry-run)" if args.dry_run else ""))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="発売日が前後 N 日の書籍を取り直す")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true", help="DB に書き込まない")
    parser.add_argument("--skip-discover", action="store_true", help="新刊探しをせず、既存の本の取り直しだけ")
    main(parser.parse_args())
