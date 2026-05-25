"""
Gemini を使ってシリーズ判定を行うオーケストレーター。

「同じ著者役職人物 (A01 著 / A23 原作 / A24 原著) が関わっている書籍」を
Union-Find でクラスタリングし、各クラスタを Gemini に渡してシリーズ構造に分類:
  - main: メインシリーズの巻
  - sub_series: 主人公/視点キャラが大きく異なる派生シリーズ
  - special_edition: 特装版 → 本編書籍に attached_to_isbn で紐付け
  - standalone: シリーズに属さない単独作品

「ラチム / ろこ」のような複数名表記は個別の人物として認識する。
イラストレーターはクラスタ判定に含めない (人気絵師による過剰クラスタを避ける)。

使い方:
  python -m scripts.gemini_detect_series --dry-run --limit 3      # 動作確認
  python -m scripts.gemini_detect_series --limit 20               # 20クラスタ試行
  python -m scripts.gemini_detect_series --person "ラチム"         # 特定人物を含むクラスタ
  python -m scripts.gemini_detect_series --min-books 2            # 2冊以上のクラスタだけ
  python -m scripts.gemini_detect_series --reset                  # 既存 series を全削除して再構築
  python -m scripts.gemini_detect_series --skip-processed         # 既に series_id 持つクラスタは skip (中断再開用)
  python -m scripts.gemini_detect_series --orphans-only           # NULL series_id の書籍のみ Gemini 再判定
  python -m scripts.gemini_detect_series --workers 5 --model flash  # 並列・高速
"""
import sys
import os
import re
import time
import argparse
import unicodedata
import threading
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.gemini import detect_series_with_gemini
from app.services.supabase_client import get_supabase
from app.config import settings

GEMINI_INTERVAL_SEC = 1
MAX_BOOKS_PER_CLUSTER = 80  # 1クラスタで Gemini に渡す上限
DEFAULT_WORKERS = 5         # 並列ワーカー数


_AUTHOR_ROLE_LABELS = frozenset({"著", "原著", "原作"})
_AUTHOR_ROLE_CODES = frozenset({"A01", "A23", "A24"})


def _norm_name(s: str | None) -> str:
    """マッチ用に正規化: NFKC + 全空白除去"""
    if not s:
        return ""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s))


def _split_names(s: str | None) -> list[str]:
    """'A / B' のような複数名を個別人物に分解"""
    if not s:
        return []
    out = []
    for part in re.split(r"[/／]", s):
        n = _norm_name(part)
        if n:
            out.append(n)
    return out


def _all_contributor_names(b: dict) -> set[str]:
    """書籍の「著者役職」人物の正規化名を集合で返す。
    対象役職: A01 (著) / A23 (原作) / A24 (原著)
    イラストレーター (A12) は対象外 (人気絵師による過剰クラスタ化を避ける)。

    'ラチム / ろこ' のような複数名表記は個別の人物として認識する。
    """
    names: set[str] = set()
    # author 列 (= A01) の '/' 区切り複数表記
    for n in _split_names(b.get("author")):
        names.add(n)
    # contributors から A01 / A23 / A24 のみ
    for c in (b.get("contributors") or []):
        if not isinstance(c, dict):
            continue
        if (c.get("role_label") in _AUTHOR_ROLE_LABELS
                or c.get("role_code") in _AUTHOR_ROLE_CODES):
            n = _norm_name(c.get("name"))
            if n:
                names.add(n)
    return names


_ORG_NAME_PATTERN = re.compile(
    r'メディアワークス|ファクトリー|企画|編集部|編集委員会|プロジェクト'
    r'|アンソロジー|出版社?|公式|愛読'
)


def _looks_organizational(name: str) -> bool:
    """contributor 名が組織/プロジェクト/編集部のように見えるか。
    これらはクラスタリングのハブとして連結を作らないよう除外する。
    """
    if not name:
        return False
    return bool(_ORG_NAME_PATTERN.search(name))


def _primary_a01(b: dict) -> str | None:
    """書籍の主著者 (A01) を返す。author 列の先頭、無ければ contributors の A01。"""
    names = _split_names(b.get("author"))
    if names:
        return names[0]
    for c in (b.get("contributors") or []):
        if not isinstance(c, dict):
            continue
        if c.get("role_code") == "A01" or c.get("role_label") == "著":
            n = _norm_name(c.get("name"))
            if n:
                return n
    return None


def _all_a01_names(b: dict) -> set[str]:
    """書籍の全 A01 著者名集合"""
    out: set[str] = set()
    for n in _split_names(b.get("author")):
        out.add(n)
    for c in (b.get("contributors") or []):
        if isinstance(c, dict) and (c.get("role_code") == "A01"
                                    or c.get("role_label") == "著"):
            n = _norm_name(c.get("name"))
            if n:
                out.add(n)
    return out


def _is_collab_book(b: dict, threshold: int = 3) -> bool:
    """A01 著者が threshold 人以上 → コラボレーション書籍。
    クラスタ連結のハブとして機能させないため独立扱いにする。
    """
    return len(_all_a01_names(b)) >= threshold


def _author_role_names(b: dict) -> list[str]:
    """A01 (著) / A23 (原作) / A24 (原著) の名前を正規化して返す。
    series.author 列の代表値決定に使う。
    """
    out: list[str] = []
    for n in _split_names(b.get("author")):
        out.append(n)
    for c in (b.get("contributors") or []):
        if not isinstance(c, dict):
            continue
        if (c.get("role_label") in _AUTHOR_ROLE_LABELS
                or c.get("role_code") in _AUTHOR_ROLE_CODES):
            n = _norm_name(c.get("name"))
            if n:
                out.append(n)
    return out


def cluster_books_by_contributors(books: list[dict],
                                  hub_threshold: int = 3) -> list[list[dict]]:
    """著者役職 (A01/A23/A24) の共有で書籍を Union-Find クラスタリング。

    - 'ろこ' が vol1 のイラスト担当、vol2 の著者担当 でも同じ人物として同クラスタ
    - 組織名 (メディアワークス, 編集部, 企画 等) はハブとして除外
    - 3+ 異なる primary A01 と関わるのに自分は primary A01 でない名前もハブ除外
      (= コラボレーション専用の名前を切り離す)

    Args:
      books: 書籍リスト
      hub_threshold: ハブ判定の最小 primary author 数 (デフォルト 3)
    """
    n = len(books)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    # 名前 → 書籍 index の逆引き
    # コラボレーション書籍 (3+ A01 著者) は連結ハブにしない (独立クラスタへ)
    name_idx: dict[str, list[int]] = defaultdict(list)
    isolated = 0
    collab_count = 0
    collab_indices: set[int] = set()
    for i, b in enumerate(books):
        ns = _all_contributor_names(b)
        if not ns:
            isolated += 1
            continue
        if _is_collab_book(b):
            collab_indices.add(i)
            collab_count += 1
            continue  # name_idx に登録しない (他書籍と連結させない)
        for nm in ns:
            name_idx[nm].append(i)
    if collab_count:
        print(f"  → コラボレーション書籍 (3+ A01) {collab_count}冊は独立扱い")

    # 各書籍の primary A01 を事前計算
    primaries: list[str | None] = [_primary_a01(b) for b in books]

    # ハブ検出
    # 1) 組織名 (メディアワークス, 企画 等) は即除外
    # 2) hub_threshold 個以上の異なる primary author の本に共通する名前
    #    かつ「その名前自身が primary A01 として登場していない」
    #    → コラボハブとして除外
    org_names: set[str] = set()
    hub_names: set[str] = set()
    for nm, indices in name_idx.items():
        if _looks_organizational(nm):
            org_names.add(nm)
            continue
        if len(indices) < hub_threshold:
            continue
        primary_set = {primaries[i] for i in indices if primaries[i]}
        is_own_primary = nm in primary_set
        if len(primary_set) >= hub_threshold and not is_own_primary:
            hub_names.add(nm)

    print(f"  → 組織名フィルタ {len(org_names)}件, "
          f"ハブ contributor フィルタ {len(hub_names)}件")

    skip_names = org_names | hub_names

    # 除外名以外で union
    for nm, indices in name_idx.items():
        if nm in skip_names:
            continue
        if len(indices) < 2:
            continue
        root = indices[0]
        for j in indices[1:]:
            union(root, j)

    # cluster_id → 書籍リスト
    clusters: dict[int, list[dict]] = defaultdict(list)
    for i, b in enumerate(books):
        if not _all_contributor_names(b) or i in collab_indices:
            # contributor 不明 or コラボ書籍 → 独立クラスタ
            clusters[-(i + 1)] = [b]
        else:
            clusters[find(i)].append(b)
    if isolated:
        print(f"  → contributor 不明 {isolated}冊は独立クラスタ扱い")
    return list(clusters.values())


def fetch_all_books(supabase, page_size: int = 1000) -> list[dict]:
    """全 books を取得 (シリーズ判定の context として既処理本も含む)"""
    print("[books 取得中]")
    rows: list[dict] = []
    offset = 0
    while True:
        rs = supabase.table("openbd_test_books").select(
            "isbn, title, author, illustrator, contributors, "
            "imprint, published_date, description, "
            "series_id, attached_to_isbn"
        ).range(offset, offset + page_size - 1).execute()
        if not rs.data:
            break
        rows.extend(rs.data)
        if len(rs.data) < page_size:
            break
        offset += page_size
    print(f"  → {len(rows):,}冊")
    return rows


def _book_is_processed(b: dict) -> bool:
    """書籍が既にシリーズ判定済みか (series_id か attached_to_isbn のいずれかが set)"""
    return bool(b.get("series_id")) or bool(b.get("attached_to_isbn"))


def _split_megacluster(cluster: list[dict], max_size: int) -> list[list[dict]]:
    """クラスタが max_size を超える場合、primary A01 ごとにサブクラスタへ分割。

    A01 が同じ書籍同士は一緒に扱う (同シリーズの可能性が高いため)。
    A01 が None の書籍は "(不明)" にまとめる。
    分割後のサブクラスタが依然 max_size を超える場合は、発売日順に max_size 単位で
    チャンク化して複数 Gemini call にかける。
    """
    if len(cluster) <= max_size:
        return [cluster]
    by_primary: dict[str, list[dict]] = defaultdict(list)
    for b in cluster:
        pa = _primary_a01(b) or "(不明)"
        by_primary[pa].append(b)
    out: list[list[dict]] = []
    for pa, sub in by_primary.items():
        if len(sub) <= max_size:
            out.append(sub)
        else:
            # それでも大きい場合は発売日順チャンク
            sorted_sub = sorted(sub, key=lambda b: b.get("published_date") or "")
            for i in range(0, len(sorted_sub), max_size):
                out.append(sorted_sub[i:i + max_size])
    return out


def _coerce_volume(v) -> int | None:
    """Gemini が 3.5 のような小数 / 文字列を返した場合に int に正規化。
    変換不能なら None (DB の INT 列に入れない)。
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    try:
        f = float(v)
    except (ValueError, TypeError):
        return None
    if f < 0:
        return None
    return int(f)  # 切り捨て (3.5 → 3)


def _primary_author_for_cluster(books: list[dict]) -> str:
    """クラスタ内の代表著者を決定 (A01 最頻値、無ければ A23/A24 等)。
    series.author 列 (NOT NULL) に保存するための値。
    """
    counter: Counter[str] = Counter()
    for b in books:
        for n in _author_role_names(b):
            counter[n] += 1
    if counter:
        return counter.most_common(1)[0][0]
    # 著者役職が誰もいない場合は全 contributor から
    for b in books:
        names = _all_contributor_names(b)
        if names:
            return next(iter(names))
    return "(不明)"


def _shared_names_in_cluster(books: list[dict]) -> list[str]:
    """クラスタ内で複数書籍に共通して登場する人物名"""
    counter: Counter[str] = Counter()
    for b in books:
        for n in _all_contributor_names(b):
            counter[n] += 1
    return [n for n, c in counter.most_common() if c >= 2]


def _most_common(values: list):
    from collections import Counter
    filtered = [v for v in values if v]
    if not filtered:
        return None
    return Counter(filtered).most_common(1)[0][0]


def _aggregate(books: list[dict]) -> dict:
    """シリーズ集約メタデータ"""
    pub_dates = [b.get("published_date") for b in books if b.get("published_date")]
    sorted_books = sorted(
        books,
        key=lambda b: (b.get("_volume_number") if b.get("_volume_number") is not None else 9999,
                       b.get("published_date") or "9999"),
    )
    vol1 = sorted_books[0] if sorted_books else {}
    return {
        "illustrator": _most_common([b.get("illustrator") for b in books]),
        "contributors": vol1.get("contributors"),
        "publisher": _most_common([b.get("publisher") for b in books]),
        "imprint": _most_common([b.get("imprint") for b in books]),
        "cover_image_url": vol1.get("cover_image_url"),
        "description": vol1.get("description"),
        "volume_count": len(books),
        "first_published_date": min(pub_dates) if pub_dates else None,
        "latest_published_date": max(pub_dates) if pub_dates else None,
    }


def _save_group(supabase, author: str, group: dict, book_index: dict[str, dict],
                dry_run: bool) -> dict:
    """1つの groups エントリを DB に反映。返り値: { saved_main, saved_sub, attached, errors }"""
    summary = {"saved_main": 0, "saved_sub": 0, "attached": 0, "linked_books": 0, "errors": []}

    main_title = (group.get("main_series_title") or "").strip()
    group_books = group.get("books") or []
    if not main_title or not group_books:
        return summary

    # 1) main 巻と sub_series 巻を分類
    main_books_raw = [b for b in group_books if b.get("role") == "main"]
    special_books_raw = [b for b in group_books if b.get("role") == "special_edition"]
    sub_books_by_sub_title: dict[str, list[dict]] = defaultdict(list)
    for b in group_books:
        if b.get("role") == "sub_series":
            st = (b.get("sub_series_title") or "サブシリーズ").strip()
            sub_books_by_sub_title[st].append(b)

    # ISBN → book row 参照を保証
    def _lookup_book(isbn: str) -> dict | None:
        b = book_index.get(isbn)
        if not b:
            summary["errors"].append(f"unknown isbn={isbn}")
        return b

    # 2) main シリーズを upsert
    main_book_rows: list[dict] = []
    for entry in main_books_raw:
        bk = _lookup_book(entry.get("isbn"))
        if bk:
            bk = dict(bk)
            bk["_volume_number"] = _coerce_volume(entry.get("volume_number"))
            main_book_rows.append(bk)
    if not main_book_rows:
        # main 0冊 ＝ 全部 sub_series や special_edition のみ → 親は仮置きで作る必要あり
        # ここではメインシリーズなしと判定して標準は作らない
        pass

    main_series_id: str | None = None
    if main_book_rows:
        agg = _aggregate(main_book_rows)
        normalized_title = f"\x03gemini\x03{author}\x03{main_title}"
        record = {
            "series_title": main_title,
            "author": author,
            "normalized_title": normalized_title,
            "is_series": len(main_book_rows) >= 2,
            "is_special_edition": False,
            **agg,
        }
        if dry_run:
            main_series_id = "(dry-run)"
        else:
            try:
                rs = supabase.table("openbd_test_series").upsert(
                    record, on_conflict="normalized_title,author",
                    returning="representation",
                ).execute()
                if rs.data:
                    main_series_id = rs.data[0]["id"]
                    summary["saved_main"] += 1
            except Exception as e:
                summary["errors"].append(f"main upsert failed: {e}")

    # 3) sub_series ごとに upsert (parent_series_id = main_series_id)
    for sub_title, sub_entries in sub_books_by_sub_title.items():
        sub_book_rows = []
        for entry in sub_entries:
            bk = _lookup_book(entry.get("isbn"))
            if bk:
                bk = dict(bk)
                bk["_volume_number"] = _coerce_volume(entry.get("volume_number"))
                sub_book_rows.append(bk)
        if not sub_book_rows:
            continue
        agg = _aggregate(sub_book_rows)
        normalized_title = f"\x03gemini\x03{author}\x03{main_title}\x03{sub_title}"
        record = {
            "series_title": sub_title,
            "author": author,
            "normalized_title": normalized_title,
            "is_series": len(sub_book_rows) >= 2,
            "is_special_edition": False,
            **agg,
        }
        if main_series_id and main_series_id != "(dry-run)":
            record["parent_series_id"] = main_series_id

        sub_series_id: str | None = None
        if dry_run:
            sub_series_id = "(dry-run-sub)"
        else:
            try:
                rs = supabase.table("openbd_test_series").upsert(
                    record, on_conflict="normalized_title,author",
                    returning="representation",
                ).execute()
                if rs.data:
                    sub_series_id = rs.data[0]["id"]
                    summary["saved_sub"] += 1
            except Exception as e:
                summary["errors"].append(f"sub upsert failed ({sub_title}): {e}")

        # sub_series の books に series_id を書き戻し
        if sub_series_id and not dry_run:
            for entry in sub_entries:
                isbn = entry.get("isbn")
                if not isbn:
                    continue
                try:
                    supabase.table("openbd_test_books").update({
                        "series_id": sub_series_id,
                        "volume_number": _coerce_volume(entry.get("volume_number")),
                        "attached_to_isbn": None,
                    }).eq("isbn", isbn).execute()
                    summary["linked_books"] += 1
                except Exception as e:
                    summary["errors"].append(f"book link sub failed {isbn}: {e}")

    # 4) main 巻の books に series_id を書き戻し
    if main_series_id and main_series_id != "(dry-run)" and not dry_run:
        for entry in main_books_raw:
            isbn = entry.get("isbn")
            if not isbn:
                continue
            try:
                supabase.table("openbd_test_books").update({
                    "series_id": main_series_id,
                    "volume_number": _coerce_volume(entry.get("volume_number")),
                    "attached_to_isbn": None,
                }).eq("isbn", isbn).execute()
                summary["linked_books"] += 1
            except Exception as e:
                summary["errors"].append(f"book link main failed {isbn}: {e}")

    # 5) special_edition: attached_to_isbn にひも付け、series_id は NULL
    for entry in special_books_raw:
        isbn = entry.get("isbn")
        attach = entry.get("attached_to_isbn")
        if not isbn:
            continue
        if dry_run:
            summary["attached"] += 1
            continue
        try:
            supabase.table("openbd_test_books").update({
                "attached_to_isbn": attach,
                "series_id": None,
                "volume_number": None,
                "volume_number_sub": None,
            }).eq("isbn", isbn).execute()
            summary["attached"] += 1
        except Exception as e:
            summary["errors"].append(f"attach failed {isbn}: {e}")

    return summary


def _save_standalone(supabase, author: str, standalone_entries: list[dict],
                     book_index: dict[str, dict], dry_run: bool) -> int:
    """standalone 書籍を 1冊1series 行として保存"""
    saved = 0
    for entry in standalone_entries:
        isbn = entry.get("isbn")
        bk = book_index.get(isbn)
        if not bk:
            continue
        title = (bk.get("title") or "").strip()
        if not title:
            continue
        normalized_title = f"\x03gemini-standalone\x03{author}\x03{isbn}"
        record = {
            "series_title": title,
            "author": author,
            "normalized_title": normalized_title,
            "is_series": False,
            "is_special_edition": False,
            **_aggregate([{**bk, "_volume_number": None}]),
        }
        if dry_run:
            saved += 1
            continue
        try:
            rs = supabase.table("openbd_test_series").upsert(
                record, on_conflict="normalized_title,author",
                returning="representation",
            ).execute()
            if rs.data:
                sid = rs.data[0]["id"]
                supabase.table("openbd_test_books").update({
                    "series_id": sid,
                    "volume_number": None,
                    "volume_number_sub": None,
                    "attached_to_isbn": None,
                }).eq("isbn", isbn).execute()
                saved += 1
        except Exception as e:
            print(f"  ! standalone 保存失敗 {isbn}: {e}")
    return saved


def main(args):
    supabase = get_supabase()

    if args.reset:
        if not args.dry_run:
            confirm = input("既存の openbd_test_series 全行を削除しますか? [y/N]: ").strip().lower()
            if confirm not in ("y", "yes"):
                print("中断しました")
                return
            supabase.table("openbd_test_series").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
            print("[reset] series 全削除完了")

    # 全 books 取得 → contributor 共有でクラスタリング
    # 既処理本も context として含めるため、ここでは全件取得する
    books = fetch_all_books(supabase)
    book_index = {b["isbn"]: b for b in books if b.get("isbn")}

    print("[クラスタリング (共通人物ベース)]")
    clusters = cluster_books_by_contributors(books)
    print(f"  → {len(clusters):,}クラスタ "
          f"(2冊以上: {sum(1 for c in clusters if len(c) >= 2):,}件)")

    effective_min_books = args.min_books

    # --orphans-only: 「クラスタ内に1冊でも孤児 (series_id NULL & attached NULL) が
    # 含まれていれば」処理対象、それ以外のクラスタは skip。
    # この時、Gemini には既処理本も含めた全 books を context として渡すため、
    # 単独著者の場合 (例: 孤児 vol3 + 既処理 vol1, vol2) でも自然に同じ
    # 既存シリーズへ統合される判定が期待できる。
    if args.orphans_only:
        before = len(clusters)
        clusters = [
            c for c in clusters
            if any(not _book_is_processed(b) for b in c)
        ]
        print(f"  → --orphans-only: 孤児を含むクラスタ {len(clusters):,}件 / 全 {before:,}件")
        # 孤児が単独クラスタになっているケースも処理するため min_books を 1 に下げる
        if effective_min_books > 1:
            print(f"  → --orphans-only: --min-books を 1 に変更 (元 {effective_min_books})")
            effective_min_books = 1

    # フィルタ
    if args.person:
        target = _norm_name(args.person)
        clusters = [c for c in clusters
                    if any(target in _all_contributor_names(b) for b in c)]
        print(f"  → 人物 '{args.person}' を含むクラスタ: {len(clusters):,}件")
    if effective_min_books > 1:
        clusters = [c for c in clusters if len(c) >= effective_min_books]
        print(f"  → {effective_min_books}冊以上のクラスタ: {len(clusters):,}件")

    # megacluster は primary A01 で分割し、それでも大きいなら発売日順チャンク化
    expanded: list[list[dict]] = []
    split_count = 0
    for c in clusters:
        parts = _split_megacluster(c, MAX_BOOKS_PER_CLUSTER)
        if len(parts) > 1:
            split_count += 1
            print(f"  → megacluster ({len(c)}冊) を {len(parts)}個に分割")
        expanded.extend(parts)
    if split_count:
        print(f"  → {split_count}個の megacluster を分割 → 合計 {len(expanded):,}サブクラスタ")
    clusters = expanded

    # 書籍数の多い順
    clusters_sorted = sorted(clusters, key=lambda c: (-len(c),))

    # 処理済みスキップ: クラスタ内の書籍が全て series_id/attached_to_isbn 済みなら skip
    if args.skip_processed:
        before = len(clusters_sorted)
        kept: list[list[dict]] = []
        skipped_clusters = 0
        skipped_books = 0
        for c in clusters_sorted:
            if all(_book_is_processed(b) for b in c):
                skipped_clusters += 1
                skipped_books += len(c)
            else:
                kept.append(c)
        clusters_sorted = kept
        print(f"  → --skip-processed: 全冊処理済みクラスタ {skipped_clusters:,} ({skipped_books:,}冊) "
              f"を skip → 残 {len(clusters_sorted):,}クラスタ")

    if args.limit:
        clusters_sorted = clusters_sorted[:args.limit]

    totals = {"saved_main": 0, "saved_sub": 0, "attached": 0,
              "standalone": 0, "linked_books": 0, "errors": 0}
    totals_lock = threading.Lock()

    # 使うモデルを決定
    if args.model == "pro":
        model_name = settings.gemini_enrichment_model
    elif args.model == "flash":
        model_name = settings.gemini_parse_model
    else:
        model_name = args.model  # 任意モデル名

    use_grounding = not args.no_grounding
    print(f"\n[Gemini 設定] model={model_name}  grounding={use_grounding}  並列={args.workers}")

    def _process_cluster(idx: int, cbooks: list[dict]) -> dict:
        """1 クラスタを処理 (Gemini 呼び出し + DB 保存)。スレッドから呼ばれる。
        返り値: {primary_author, n_groups, n_standalone, summary_per_group, ...}
        """
        primary_author = _primary_author_for_cluster(cbooks)
        shared = _shared_names_in_cluster(cbooks)
        imprint_hint = _most_common([b.get("imprint") for b in cbooks])
        result_info = {
            "idx": idx,
            "n_books": len(cbooks),
            "primary_author": primary_author,
            "shared": shared,
            "groups_saved": [],
            "standalone_saved": 0,
            "error": None,
        }
        # 1冊クラスタは Gemini を呼ばず直接 standalone (高速化)
        if len(cbooks) == 1:
            entry = {"isbn": cbooks[0].get("isbn"), "reason": "singleton cluster"}
            s_n = _save_standalone(supabase, primary_author, [entry],
                                   book_index, args.dry_run)
            result_info["standalone_saved"] = s_n
            with totals_lock:
                totals["standalone"] += s_n
            return result_info
        try:
            result = detect_series_with_gemini(
                books=cbooks,
                shared_names=shared,
                imprint_hint=imprint_hint,
                model=model_name,
                use_grounding=use_grounding,
            )
        except Exception as e:
            result_info["error"] = f"Gemini エラー: {e}"
            return result_info
        groups = result.get("groups") or []
        standalone = result.get("standalone") or []

        # Gemini 応答に含まれなかった ISBN を取りこぼしとして検出し、
        # standalone fallback に追加する (NULL series_id 防止)
        input_isbns = {b.get("isbn") for b in cbooks if b.get("isbn")}
        covered: set[str] = set()
        for g in groups:
            for bk in (g.get("books") or []):
                isbn = bk.get("isbn")
                if isbn:
                    covered.add(isbn)
        for s in standalone:
            isbn = s.get("isbn")
            if isbn:
                covered.add(isbn)
        missed = input_isbns - covered
        if missed:
            for isbn in missed:
                standalone.append({"isbn": isbn, "reason": "Gemini response missed"})
            result_info["missed_count"] = len(missed)

        # DB 保存 (各クラスタ独立)
        for g in groups:
            summary = _save_group(supabase, primary_author, g, book_index, args.dry_run)
            result_info["groups_saved"].append({
                "title": g.get("main_series_title"),
                **summary,
            })
            with totals_lock:
                for k in ["saved_main", "saved_sub", "attached", "linked_books"]:
                    totals[k] += summary.get(k, 0)
                if summary.get("errors"):
                    totals["errors"] += len(summary["errors"])

        s_n = _save_standalone(supabase, primary_author, standalone, book_index, args.dry_run)
        result_info["standalone_saved"] = s_n
        with totals_lock:
            totals["standalone"] += s_n
        return result_info

    # ThreadPoolExecutor で並列実行
    print_lock = threading.Lock()
    completed = 0
    total = len(clusters_sorted)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_process_cluster, i, c): (i, c)
                   for i, c in enumerate(clusters_sorted, 1)}
        for fut in as_completed(futures):
            idx, cbooks = futures[fut]
            with print_lock:
                completed += 1
            try:
                info = fut.result()
            except Exception as e:
                with print_lock:
                    print(f"\n[{completed}/{total}] cluster idx={idx}: 例外 {e}")
                with totals_lock:
                    totals["errors"] += 1
                continue
            with print_lock:
                shared = info["shared"]
                print(f"\n[{completed}/{total}] クラスタ ({info['n_books']}冊) "
                      f"主著={info['primary_author']!r}  "
                      f"共通人物={shared[:5]}{'...' if len(shared)>5 else ''}")
                if info["error"]:
                    print(f"  ! {info['error']}")
                    continue
                for g in info["groups_saved"]:
                    print(f"  - 「{g.get('title')}」 main+{g['saved_main']} "
                          f"sub+{g['saved_sub']} 特装+{g['attached']} "
                          f"linked+{g['linked_books']}")
                    for er in (g.get("errors") or [])[:3]:
                        print(f"    ! {er}")
                if info["standalone_saved"]:
                    print(f"  - standalone +{info['standalone_saved']}")
                if info.get("missed_count"):
                    print(f"  ! Gemini 取りこぼし {info['missed_count']}冊 → standalone fallback")

    print()
    print("=" * 60)
    print("[全体集計]")
    for k, v in totals.items():
        print(f"  {k}: {v:,}")
    if args.dry_run:
        print("\n[DRY RUN] DB 書き込みはスキップ")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gemini によるシリーズ判定")
    parser.add_argument("--dry-run", action="store_true", help="DB 書き込みしない")
    parser.add_argument("--limit", type=int, default=None,
                        help="処理するクラスタ数の上限 (試行用)")
    parser.add_argument("--person", default=None,
                        help="特定人物 (役職問わず) を含むクラスタのみ処理")
    parser.add_argument("--min-books", type=int, default=2,
                        help="N冊以上のクラスタだけ処理 (デフォルト2)")
    parser.add_argument("--reset", action="store_true",
                        help="開始前に openbd_test_series を全削除")
    parser.add_argument("--skip-processed", action="store_true",
                        help="クラスタ内の書籍が全て処理済み (series_id or attached_to_isbn) なら skip")
    parser.add_argument("--orphans-only", action="store_true",
                        help="孤児 (series_id NULL & attached_to_isbn NULL) を含むクラスタのみ "
                             "処理。既処理本も context として Gemini に渡す")
    parser.add_argument("--sleep", type=float, default=GEMINI_INTERVAL_SEC,
                        help=f"(廃止予定) Gemini 呼び出し間隔秒 (デフォルト {GEMINI_INTERVAL_SEC})")
    parser.add_argument("--model", default="flash",
                        help="Gemini モデル: 'flash' (高速) / 'pro' (高品質) / 任意モデル名 "
                             "(デフォルト flash)")
    parser.add_argument("--no-grounding", action="store_true",
                        help="Web 検索 grounding を無効化 (速度↑, 無名作品の認識↓)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"並列ワーカー数 (デフォルト {DEFAULT_WORKERS})")
    args = parser.parse_args()
    main(args)
