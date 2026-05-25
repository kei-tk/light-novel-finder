"""
主要なライトノベルレーベルを openBD + hanmoto で一括収集する。

フロー:
  1. 事前チェック (--check / 自動実行時は最初に表示)
       各レーベルについて hanmoto で取得可能か少数サンプルで確認
  2. レーベルごとに収集 → 1レーベル完了したら DB保存 → 次へ

データ保存先: openbd_test_books (imprint 列でレーベルを区別)

使い方:
  python -m scripts.collect_all_labels --check                  # 事前確認のみ
  python -m scripts.collect_all_labels                          # 全レーベルを順次収集
  python -m scripts.collect_all_labels --label 電撃文庫          # 1レーベルだけ
  python -m scripts.collect_all_labels --label 電撃文庫 --label MF文庫J
  python -m scripts.collect_all_labels --skip-existing          # DB既存ISBNはスキップ
  python -m scripts.collect_all_labels --hanmoto-max 50 --dry-run  # 軽く試す
"""
import sys
import os
import time
import random
import argparse
from collections import defaultdict
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase
from scripts.discover_openbd import (
    fetch_coverage,
    filter_by_prefix,
    fetch_metadata_batch,
    fetch_hanmoto_metadata,
    _collect_imprint_candidates,
    _fetch_existing_isbns,
    process_label,
    BATCH_SIZE,
    SLEEP_BETWEEN_HANMOTO,
)

# =====================================================================
# 主要ライトノベルレーベル
#   prefixes は ISBN-13 の出版者プレフィックス (978-{4|5}-XXX...)。複数指定可。
#   出版社が広いプレフィックス内で複数レーベルを出している場合、
#   その出版社のプレフィックス全部を入れて imprint 名でフィルタする。
# =====================================================================
LIGHT_NOVEL_LABELS: list[dict] = [
    # KADOKAWA系 (978-4-04)
    {"name": "電撃文庫",            "prefixes": ["978404"]},
    {"name": "MF文庫J",             "prefixes": ["978404"]},
    {"name": "富士見ファンタジア文庫", "prefixes": ["978404"]},
    {"name": "角川スニーカー文庫",    "prefixes": ["978404"]},
    {"name": "角川ビーンズ文庫",      "prefixes": ["978404"]},
    {"name": "ファミ通文庫",         "prefixes": ["978404"]},
    {"name": "電撃の新文芸",         "prefixes": ["978404"]},
    # 集英社 (978-4-08)
    {"name": "ダッシュエックス文庫",  "prefixes": ["978408"]},
    {"name": "コバルト文庫",         "prefixes": ["978408"]},
    # 小学館 (978-4-09)
    {"name": "ガガガ文庫",           "prefixes": ["978409"]},
    {"name": "ルルル文庫",           "prefixes": ["978409"]},
    # 講談社 (978-4-06)
    {"name": "講談社ラノベ文庫",      "prefixes": ["978406"]},
    {"name": "レジェンドノベルス",    "prefixes": ["978406"]},
    # ホビージャパン (978-4-7986)
    {"name": "HJ文庫",              "prefixes": ["9784798"]},
    {"name": "HJ NOVELS",           "prefixes": ["9784798"]},
    # オーバーラップ (978-4-86554, 978-4-8240)
    {"name": "オーバーラップ文庫",    "prefixes": ["9784865", "9784824"]},
    {"name": "オーバーラップノベルス", "prefixes": ["9784865", "9784824"]},
    # SBクリエイティブ (978-4-7973, 978-4-8156, 978-4-8157)
    {"name": "GA文庫",              "prefixes": ["9784797", "9784815"]},
    {"name": "GAノベル",            "prefixes": ["9784797", "9784815"]},
    # アース・スター エンターテイメント (978-4-8030)
    {"name": "アース・スターノベル",  "prefixes": ["9784803"]},
    # マイクロマガジン (978-4-89637, 978-4-86716)
    {"name": "GC NOVELS",           "prefixes": ["9784896", "9784867"]},
    # TOブックス (978-4-86472, 978-4-86699)
    {"name": "TOブックス",           "prefixes": ["9784864", "9784866"]},
    # 主婦の友インフォス
    {"name": "ヒーロー文庫",         "prefixes": ["978407", "978404"]},
    # 一迅社
    {"name": "一迅社文庫",           "prefixes": ["978475", "978476"]},
    # 双葉社
    {"name": "モンスター文庫",        "prefixes": ["978457"]},
]

PRECHECK_OPENBD_PER_LABEL = 1000  # 1レーベルあたり何件のISBNをopenBDでサンプルするか
PRECHECK_HANMOTO_FALLBACK = 30    # openBDで見つからなければhanmotoで何件試すか
PRECHECK_RANDOM_SEED = 42         # 再現性のため固定 (None にすれば毎回ランダム)


def _precheck_prefix_group(group: list[dict], prefix_isbns: list[str],
                           rng: random.Random) -> dict[str, dict]:
    """プレフィックスを共有するレーベル群を1回のopenBDバッチで一括検査。

    KADOKAWA(978404)のように複数レーベルが同じプレフィックスを持つ場合、
    1000*レーベル数 件をランダムサンプルし、各ISBNのimprintを全レーベル名と照合する。
    """
    if not prefix_isbns:
        return {
            ld["name"]: {
                "name": ld["name"], "status": "NG",
                "reason": "プレフィックスに該当ISBNが coverage に1件もない",
                "examples": [], "prefix_total": 0, "found_in": None,
            } for ld in group
        }

    sample_size = min(len(prefix_isbns), PRECHECK_OPENBD_PER_LABEL * max(1, len(group)))
    sample = rng.sample(prefix_isbns, sample_size)
    print(f"    openBD: ランダム {sample_size:,}件サンプル "
          f"(プレフィックス内 {len(prefix_isbns):,}件 / {len(group)}レーベル分)")
    try:
        items = fetch_metadata_batch(sample)
    except Exception as e:
        print(f"    ! openBD batch失敗: {e}")
        items = []

    label_matches: dict[str, list[tuple[str, str]]] = {ld["name"]: [] for ld in group}
    openbd_imprint_found = 0
    for req_isbn, it in zip(sample, items):
        if not it:
            continue
        candidates = _collect_imprint_candidates(it)
        if candidates:
            openbd_imprint_found += 1
        title = (it.get("summary") or {}).get("title") or ""
        for ld in group:
            name = ld["name"]
            if len(label_matches[name]) >= 3:
                continue
            for c in candidates:
                if name in c:
                    label_matches[name].append((req_isbn, title))
                    break
    print(f"    openBD: imprint判明 {openbd_imprint_found:,}/{sample_size:,}")

    results: dict[str, dict] = {}
    needs_hanmoto: list[dict] = [ld for ld in group if not label_matches[ld["name"]]]

    for ld in group:
        name = ld["name"]
        if label_matches[name]:
            results[name] = {
                "name": name, "status": "OK", "found_in": "openBD",
                "examples": label_matches[name],
                "sample_size": f"openBD {sample_size:,}",
                "prefix_total": len(prefix_isbns),
            }

    if needs_hanmoto:
        print(f"    hanmoto fallback: {[ld['name'] for ld in needs_hanmoto]}")

    for ld in needs_hanmoto:
        name = ld["name"]
        h_size = min(len(prefix_isbns), PRECHECK_HANMOTO_FALLBACK)
        h_sample = rng.sample(prefix_isbns, h_size)  # ラベルごとに別サンプル
        h_examples: list[tuple[str, str]] = []
        for idx, isbn in enumerate(h_sample):
            if idx > 0:
                time.sleep(SLEEP_BETWEEN_HANMOTO)
            meta = fetch_hanmoto_metadata(isbn, verbose=False)
            if meta and meta.get("label") and name in meta["label"]:
                h_examples.append((isbn, meta.get("title") or ""))
                if len(h_examples) >= 3:
                    break
        if h_examples:
            results[name] = {
                "name": name, "status": "OK", "found_in": "hanmoto",
                "examples": h_examples,
                "sample_size": f"openBD {sample_size:,} + hanmoto {h_size}",
                "prefix_total": len(prefix_isbns),
            }
        else:
            results[name] = {
                "name": name, "status": "WARN",
                "reason": f"openBD {sample_size:,}件 + hanmoto {h_size}件 でラベル一致なし",
                "examples": [], "found_in": None,
                "prefix_total": len(prefix_isbns),
            }
    return results


def run_prechecks(all_isbns: list[str], labels: list[dict]) -> list[dict]:
    print("\n" + "=" * 70)
    print(f"[事前確認] {len(labels)}レーベル / coverage {len(all_isbns):,}件")
    print("=" * 70)

    # プレフィックスのタプルでグループ化 (KADOKAWAの7レーベルは1グループにまとまる)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for ld in labels:
        groups[tuple(sorted(ld["prefixes"]))].append(ld)

    rng = random.Random(PRECHECK_RANDOM_SEED)
    results_by_label: dict[str, dict] = {}
    for gi, (pfx_key, group) in enumerate(groups.items()):
        names_in_group = [ld["name"] for ld in group]
        print(f"\n--- グループ {gi+1}/{len(groups)} prefix={list(pfx_key)} "
              f"({len(group)}レーベル: {names_in_group}) ---")
        prefix_isbns = [i for i in all_isbns if i and i.startswith(pfx_key)]
        group_results = _precheck_prefix_group(group, prefix_isbns, rng)
        results_by_label.update(group_results)

    # 元の順序で表示
    print("\n" + "=" * 70)
    print("[事前確認 詳細]")
    print("=" * 70)
    ordered: list[dict] = []
    for i, ld in enumerate(labels):
        r = results_by_label[ld["name"]]
        ordered.append(r)
        print(f"\n[{i+1}/{len(labels)}] {ld['name']} ({ld['prefixes']})")
        if r["status"] == "OK":
            print(f"  ✓ {r['found_in']} で確認 / プレフィックス内 {r['prefix_total']:,}件")
            for isbn, title in r["examples"][:3]:
                print(f"    - [{isbn}] {title[:50]}")
        elif r["status"] == "WARN":
            print(f"  △ 警告: {r['reason']} (プレフィックス内 {r['prefix_total']:,}件)")
        else:
            print(f"  ✗ NG: {r['reason']}")
    return ordered


def print_precheck_summary(results: list[dict]) -> None:
    ok = [r for r in results if r["status"] == "OK"]
    warn = [r for r in results if r["status"] == "WARN"]
    ng = [r for r in results if r["status"] == "NG"]
    print("\n" + "=" * 70)
    print(f"[事前確認 結果] OK={len(ok)} / WARN={len(warn)} / NG={len(ng)}")
    print("=" * 70)
    print(f"  ✓ 収集可能: {', '.join(r['name'] for r in ok)}")
    if warn:
        print(f"  △ 要注意:   {', '.join(r['name'] for r in warn)}")
    if ng:
        print(f"  ✗ 不可:     {', '.join(r['name'] for r in ng)}")


def collect_all(labels: list[dict], all_isbns: list[str], dry_run: bool,
                hanmoto_max: int | None, skip_existing: bool,
                concurrency: int | None = None) -> list[dict]:
    # skip_set: DB既存 + セッション内で既に hanmoto 取得済みのISBN集合
    # 不明ISBNは1回だけhanmoto取得し、判明次第DB保存 → 後続レーベルでは再取得しない
    if skip_existing:
        skip_set: set[str] = _fetch_existing_isbns(get_supabase())
        print(f"\n[skip-existing] DB既存ISBN {len(skip_set):,}件を初期スキップセットに追加")
    else:
        skip_set = set()
    all_label_names = {ld["name"] for ld in labels}

    summaries: list[dict] = []
    for i, ld in enumerate(labels):
        name = ld["name"]
        prefixes = ld["prefixes"]
        print("\n" + "=" * 70)
        print(f"[{i+1}/{len(labels)}] '{name}' 収集開始 (prefixes={prefixes})")
        print(f"  現在のスキップセット: {len(skip_set):,}件")
        print("=" * 70)

        candidates = filter_by_prefix(all_isbns, prefixes)
        others = all_label_names - {name}
        try:
            summary = process_label(
                imprint=name,
                candidates=candidates,
                dry_run=dry_run,
                hanmoto_max=hanmoto_max,
                limit=None,
                skip_existing_set=skip_set,
                other_target_labels=others,
                concurrency=concurrency,
            )
            # hanmoto試行済みISBNをスキップセットに追加 (opportunistic保存済み含む)
            if not dry_run and summary.get("attempted_isbns"):
                added = len(summary["attempted_isbns"])
                skip_set.update(summary["attempted_isbns"])
                print(f"  → セッションスキップセットに +{added:,}件 追加 (累計 {len(skip_set):,})")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"  ! 処理失敗 {name}: {e}")
            summary = {"imprint": name, "candidates": len(candidates),
                       "filtered": 0, "matched": 0, "saved": 0,
                       "opportunistic_matched": 0, "opportunistic_saved": 0,
                       "error": str(e)}
        summaries.append(summary)
        print(f"\n[{name}] 候補 {summary['candidates']:,} / フィルタ後 {summary['filtered']:,} "
              f"/ 主一致 {summary['matched']} / 保存 {summary['saved']} "
              f"/ opportunistic {summary.get('opportunistic_matched', 0)}/{summary.get('opportunistic_saved', 0)}")

    return summaries


def print_collect_summary(summaries: list[dict]) -> None:
    print("\n" + "=" * 70)
    print("[全体サマリ]")
    print("=" * 70)
    total_saved = sum(s.get("saved", 0) for s in summaries)
    total_opp = sum(s.get("opportunistic_saved", 0) for s in summaries)
    print(f"{'レーベル':<24} {'候補':>10} {'フィルタ後':>10} {'主一致':>8} {'保存':>8} {'opp保存':>8}")
    print("-" * 80)
    for s in summaries:
        err = "  ※ERROR" if s.get("error") else ""
        print(f"{s['imprint']:<24} {s['candidates']:>10,} {s['filtered']:>10,} "
              f"{s['matched']:>8,} {s['saved']:>8,} {s.get('opportunistic_saved', 0):>8,}{err}")
    print("-" * 80)
    print(f"合計保存: 主 {total_saved:,} + opportunistic {total_opp:,} = {total_saved + total_opp:,} 件")


def main():
    parser = argparse.ArgumentParser(description="主要ライトノベルレーベルの一括収集")
    parser.add_argument("--check", action="store_true",
                        help="事前確認のみ実行 (収集はしない)")
    parser.add_argument("--label", action="append", default=None,
                        help="特定レーベルだけ処理 (複数指定可)。未指定で全レーベル")
    parser.add_argument("--dry-run", action="store_true", help="DBに書き込まない")
    parser.add_argument("--hanmoto-max", type=int, default=None,
                        help="各レーベルのhanmoto試行上限")
    parser.add_argument("--skip-existing", action="store_true",
                        help="DBに既に登録済みのISBNを除外")
    parser.add_argument("--no-precheck", action="store_true",
                        help="事前確認をスキップして即収集に入る")
    parser.add_argument("--concurrency", type=int, default=None,
                        help="hanmoto への並列接続数 (デフォルト 5)")
    args = parser.parse_args()

    # ラベル選択
    labels = LIGHT_NOVEL_LABELS
    if args.label:
        wanted = set(args.label)
        labels = [l for l in LIGHT_NOVEL_LABELS if l["name"] in wanted]
        unknown = wanted - {l["name"] for l in labels}
        if unknown:
            print(f"  ! 未知のレーベル: {unknown}")
            print(f"    利用可能: {[l['name'] for l in LIGHT_NOVEL_LABELS]}")
            return
    print(f"対象レーベル ({len(labels)}件): {[l['name'] for l in labels]}")

    # coverage は1回だけ取得
    all_isbns = fetch_coverage()

    # 事前確認
    if not args.no_precheck:
        results = run_prechecks(all_isbns, labels)
        print_precheck_summary(results)
        if args.check:
            return
        # WARN/NG のレーベルは収集対象から外す
        ok_names = {r["name"] for r in results if r["status"] == "OK"}
        skipped = [l for l in labels if l["name"] not in ok_names]
        if skipped:
            print(f"\n[!] 確認に通らなかった {len(skipped)}レーベルは収集をスキップします: "
                  f"{[l['name'] for l in skipped]}")
        labels = [l for l in labels if l["name"] in ok_names]
        if not labels:
            print("収集対象がありません。終了。")
            return

    # 収集
    summaries = collect_all(
        labels=labels,
        all_isbns=all_isbns,
        dry_run=args.dry_run,
        hanmoto_max=args.hanmoto_max,
        skip_existing=args.skip_existing,
        concurrency=args.concurrency,
    )
    print_collect_summary(summaries)


if __name__ == "__main__":
    main()
