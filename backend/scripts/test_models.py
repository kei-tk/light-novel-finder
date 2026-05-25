"""
discover_official で使う Gemini モデルを比較するテストスクリプト

公式サイトのHTMLは1度だけ取得し、その同一HTMLに対して各モデルで
書籍抽出を行うことで、モデル差だけを純粋に比較する。

使い方:
  python -m scripts.test_models                # 直近1年（去年）を baseline=3.1 Pro と比較
  python -m scripts.test_models --year 2025
  python -m scripts.test_models --year 2025 --baseline gemini-3.1-pro-preview \
      --candidates gemini-2.5-pro,gemini-2.5-flash,gemini-3-flash-preview

出力:
  - 標準出力に比較サマリ
  - tmp/model_test_<year>/<model>.json に各モデルの抽出結果を保存
"""
import sys
import os
import time
import json
import argparse
from datetime import date
from pathlib import Path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.gemini import _fetch_html, _extract_books_from_html

DEFAULT_BASELINE = "gemini-3.1-pro-preview"
DEFAULT_CANDIDATES = [
    "gemini-3-pro-preview",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
]


def fetch_year_html(year: int, max_pages: int = 30) -> list[str]:
    """年内の全ページHTMLを取得"""
    base_url = f"https://lanove.kodansha.co.jp/books/?label=&year={year}"
    pages_html = []
    for page in range(1, max_pages + 1):
        url = base_url if page == 1 else f"{base_url}&page={page}"
        html = _fetch_html(url)
        if not html:
            break
        pages_html.append(html)
    return pages_html


def normalize_title(t: str) -> str:
    """比較用にタイトルを正規化（空白除去・全角半角統一）"""
    if not t:
        return ""
    import unicodedata
    return unicodedata.normalize("NFKC", t).replace(" ", "").replace("　", "").lower()


def extract_with_model(pages_html: list[str], year: int, model: str) -> tuple[list[dict], float]:
    """指定モデルで全ページから書籍抽出。Returns (books, elapsed_sec)"""
    t0 = time.time()
    seen = set()
    all_books = []
    for i, html in enumerate(pages_html):
        books = _extract_books_from_html(html, year, model=model)
        for b in books:
            t = (b.get("title") or "").strip()
            if not t:
                continue
            key = normalize_title(t)
            if key in seen:
                continue
            seen.add(key)
            all_books.append(b)
    return all_books, time.time() - t0


def compare(baseline: list[dict], candidate: list[dict]) -> dict:
    base_keys = {normalize_title(b["title"]): b["title"] for b in baseline if b.get("title")}
    cand_keys = {normalize_title(b["title"]): b["title"] for b in candidate if b.get("title")}

    base_set = set(base_keys.keys())
    cand_set = set(cand_keys.keys())

    common = base_set & cand_set
    missing = base_set - cand_set       # baseline にあって候補にない
    extra = cand_set - base_set         # 候補にあって baseline にない
    union = base_set | cand_set

    jaccard = len(common) / len(union) if union else 1.0

    return {
        "baseline_count": len(base_set),
        "candidate_count": len(cand_set),
        "common": len(common),
        "missing_count": len(missing),
        "extra_count": len(extra),
        "jaccard": jaccard,
        "missing_titles": [base_keys[k] for k in missing],
        "extra_titles": [cand_keys[k] for k in extra],
    }


def main(year: int, baseline: str, candidates: list[str], outdir: Path):
    print(f"=== モデル比較テスト: {year}年 ===")
    print(f"baseline: {baseline}")
    print(f"candidates: {candidates}\n")

    print(f"[HTML取得] {year}年の全ページを取得中...")
    pages_html = fetch_year_html(year)
    print(f"  → {len(pages_html)}ページ取得（合計 {sum(len(h) for h in pages_html):,} 文字）\n")

    if not pages_html:
        print("HTMLが取得できませんでした。終了します。")
        return

    outdir.mkdir(parents=True, exist_ok=True)

    # baseline 実行
    print(f"[baseline] {baseline} で抽出中...")
    base_books, base_time = extract_with_model(pages_html, year, baseline)
    print(f"  → {len(base_books)}件 / {base_time:.1f}秒\n")
    (outdir / f"{baseline.replace('/', '_')}.json").write_text(
        json.dumps({"model": baseline, "year": year, "elapsed_sec": base_time,
                    "count": len(base_books), "books": base_books},
                   ensure_ascii=False, indent=2)
    )

    # candidates 実行
    results = []
    for model in candidates:
        if model == baseline:
            continue
        print(f"[候補] {model} で抽出中...")
        try:
            books, elapsed = extract_with_model(pages_html, year, model)
            print(f"  → {len(books)}件 / {elapsed:.1f}秒")
        except Exception as e:
            print(f"  ! エラー: {e}")
            continue

        (outdir / f"{model.replace('/', '_')}.json").write_text(
            json.dumps({"model": model, "year": year, "elapsed_sec": elapsed,
                        "count": len(books), "books": books},
                       ensure_ascii=False, indent=2)
        )

        diff = compare(base_books, books)
        results.append({"model": model, "elapsed_sec": elapsed, **diff})
        print(f"  共通: {diff['common']} / 不足: {diff['missing_count']} / 余分: {diff['extra_count']} / Jaccard: {diff['jaccard']:.3f}\n")

    # サマリ表
    print("\n=== サマリ ===")
    print(f"{'model':<35} {'count':>6} {'time':>7} {'common':>7} {'missing':>8} {'extra':>6} {'Jaccard':>8} {'speedup':>8}")
    print("-" * 100)
    print(f"{baseline:<35} {len(base_books):>6} {base_time:>6.1f}s {'-':>7} {'-':>8} {'-':>6} {'1.000':>8} {'1.0x':>8}")
    for r in results:
        speedup = base_time / r["elapsed_sec"] if r["elapsed_sec"] > 0 else 0
        print(f"{r['model']:<35} {r['candidate_count']:>6} {r['elapsed_sec']:>6.1f}s "
              f"{r['common']:>7} {r['missing_count']:>8} {r['extra_count']:>6} "
              f"{r['jaccard']:>8.3f} {speedup:>7.1f}x")

    # 不足/余分の詳細
    print("\n=== 詳細（baseline と違うもの） ===")
    for r in results:
        if r["missing_count"] == 0 and r["extra_count"] == 0:
            print(f"\n{r['model']}: baselineと完全一致")
            continue
        print(f"\n{r['model']}:")
        if r["missing_titles"]:
            print(f"  不足（baselineにあって取れなかった）: {len(r['missing_titles'])}件")
            for t in r["missing_titles"][:10]:
                print(f"    - {t}")
            if len(r["missing_titles"]) > 10:
                print(f"    ...他 {len(r['missing_titles']) - 10}件")
        if r["extra_titles"]:
            print(f"  余分（baselineにないものを抽出）: {len(r['extra_titles'])}件")
            for t in r["extra_titles"][:10]:
                print(f"    + {t}")
            if len(r["extra_titles"]) > 10:
                print(f"    ...他 {len(r['extra_titles']) - 10}件")

    print(f"\n[保存先] {outdir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="discover_officialのモデル比較テスト")
    parser.add_argument("--year", type=int, default=date.today().year - 1,
                        help="調査対象年（デフォルト: 去年）")
    parser.add_argument("--baseline", type=str, default=DEFAULT_BASELINE,
                        help="基準モデル（このモデルとの差分を見る）")
    parser.add_argument("--candidates", type=str, default=",".join(DEFAULT_CANDIDATES),
                        help="比較候補モデル（カンマ区切り）")
    parser.add_argument("--outdir", type=str,
                        default=str(Path(__file__).parent.parent / "tmp" / "model_test"),
                        help="抽出結果JSONの保存先")
    args = parser.parse_args()

    candidates = [m.strip() for m in args.candidates.split(",") if m.strip()]
    outdir = Path(args.outdir) / str(args.year)
    main(year=args.year, baseline=args.baseline, candidates=candidates, outdir=outdir)
