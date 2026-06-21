"""
openBD（https://openbd.jp）から指定レーベルの書籍を収集する試行スクリプト

openBD には「レーベルで絞る検索API」が無いため、以下の流れで集める:
  1. /v1/coverage で全ISBNリストを取得（数百万件）
  2. ISBN出版社プレフィックスで KADOKAWA系（例: 978-4-04...）に粗く絞る
  3. /v1/get に1000件ずつPOSTでバッチ問い合わせ
  4. onix.PublishingDetail.Imprint.ImprintName が指定レーベルと一致するものだけ保存

データは別テーブル `openbd_test_books` に保存される（試行用、いつでも DROP TABLE で削除可）。

使い方:
  python -m scripts.discover_openbd                       # デフォルト 電撃文庫
  python -m scripts.discover_openbd --imprint 電撃文庫
  python -m scripts.discover_openbd --imprint 電撃文庫 --limit 100  # 試しに100件
  python -m scripts.discover_openbd --dry-run             # 件数だけ表示
  python -m scripts.discover_openbd --prefix 9784048      # ISBNプレフィックスを限定
"""
import sys
import os
import time
import argparse
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from app.services.supabase_client import get_supabase

OPENBD_COVERAGE_URL = "https://api.openbd.jp/v1/coverage"
OPENBD_GET_URL = "https://api.openbd.jp/v1/get"
HANMOTO_ISBN_URL = "https://www.hanmoto.com/bd/isbn/{isbn}"

# KADOKAWA系（電撃・MF・富士見・角川スニーカー等）に絞る粗いプレフィックス
DEFAULT_PREFIXES = ["978404"]

BATCH_SIZE = 1000           # /v1/get に一度に投げるISBN数
HTTP_TIMEOUT = 60
SLEEP_BETWEEN_BATCH = 0.3   # openBD バッチ間の礼儀的な間隔
SLEEP_BETWEEN_HANMOTO = 0.5 # hanmoto 直列処理時の間隔 (並列処理時は使用しない)
HANMOTO_CONCURRENCY = 5     # hanmoto への並列接続数 (詳細取得を高速化)

# 共有 HTTP クライアント (Keep-Alive コネクションプール)
# 毎リクエスト TLS 再ハンドシェイクを避けるため重要
_HTTP_CLIENT: "httpx.Client | None" = None


def _get_http_client() -> "httpx.Client":
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        _HTTP_CLIENT = httpx.Client(
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (RanobeFinder data ingest)"},
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _HTTP_CLIENT

# hanmoto.com 側の og:title は「{title} {author}(著) - {publisher}」形式
# - author は数字始まり禁止 (巻数 "9" がauthorに入るのを防ぐ)
# - author は空白を許可 ("芝村 裕吏" のように)
# - 複数貢献者 "、椎名優(イラスト)" などを許容
_HANMOTO_OG_TITLE_RE = re.compile(
    r"^(?P<title>.+?)\s+(?P<author>[^()0-9０-９][^()]*?)\(著\)"
    r"(?:、[^()]+\([^()]+\))*\s*-\s*(?P<publisher>.+)$"
)
_HANMOTO_LABEL_RE = re.compile(
    # hanmoto は書籍によって book-label-title または book-series-title にレーベル名を入れる
    r'<a[^>]*class="book-(?:label|series)-title"[^>]*>(?:<ruby>)?<rb>([^<]+)</rb>',
    re.DOTALL,
)
# 完全版の紹介文 (内容紹介): hanmoto は <p itemprop="description"> に
# フル本文を入れる。og:description は SNS シェア用に ~100 文字で truncate されているため、
# こちらを優先して取得する。
_HANMOTO_DESCRIPTION_RE = re.compile(
    r'<(?P<tag>p|div)[^>]*itemprop=["\']description["\'][^>]*>([\s\S]*?)</(?P=tag)>',
    re.IGNORECASE,
)
# 初版年月日 (発行日): "2026年2月10日" のような日本語表記
_HANMOTO_PUBLISH_DATE_RE = re.compile(
    r'<dd[^>]*class="[^"]*book-dates-publish[^"]*"[^>]*>([^<]+)</dd>',
    re.DOTALL,
)
_JP_DATE_RE = re.compile(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日')
_JP_YEARMONTH_RE = re.compile(r'(\d{4})\s*年\s*(\d{1,2})\s*月')
# <a class="book-author" data-book-author-name="..." data-book-author-role="A01|A12|...">
#   ... <span class="book-author-role">著|絵|原作|...</span>
# 名前・ONIXコード・日本語ラベルの3つを同時に取得
_HANMOTO_CONTRIB_RE = re.compile(
    r'<a[^>]*class="book-author"[^>]*data-book-author-name="([^"]+)"'
    r'[^>]*data-book-author-role="([^"]*)"'
    r'.*?<span class="book-author-role">([^<]+)</span>',
    re.DOTALL,
)
_META_RE_TMPL = r'<meta\s+property="{prop}"\s+content="([^"]*)"'


def fetch_coverage() -> list[str]:
    print("[1/3] coverage 取得中（全ISBNリスト、数百万件・数百MB）...")
    r = httpx.get(OPENBD_COVERAGE_URL, timeout=HTTP_TIMEOUT * 5)
    r.raise_for_status()
    data = r.json()
    print(f"  → 取得 {len(data):,}件")
    return data


def filter_by_prefix(isbns: list[str], prefixes: list[str]) -> list[str]:
    pset = tuple(prefixes)
    out = [i for i in isbns if i and i.startswith(pset)]
    print(f"[2/3] プレフィックス {prefixes} で絞り込み → {len(out):,}件")
    return out


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def normalize_pubdate(s: str | None) -> str | None:
    if not s:
        return None
    s = str(s).strip()
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    if re.fullmatch(r"\d{6}", s):
        return f"{s[:4]}-{s[4:6]}-01"
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return f"{s}-01"
    # 「2026年2月10日」「2026年2月」を YYYY-MM-DD に
    m = _JP_DATE_RE.search(s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = _JP_YEARMONTH_RE.search(s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-01"
    return None


def _collect_imprint_candidates(item: dict) -> list[str]:
    """openBD ONIX 内で「レーベル名」が入っている可能性のあるパスを全て拾う"""
    out: list[str] = []
    onix = item.get("onix") or {}
    summary = item.get("summary") or {}

    # 1. PublishingDetail.Imprint.ImprintName
    imp = (onix.get("PublishingDetail") or {}).get("Imprint") or {}
    out.append(imp.get("ImprintName"))

    # 2. PublishingDetail.Publisher.PublisherName
    pub = (onix.get("PublishingDetail") or {}).get("Publisher") or {}
    out.append(pub.get("PublisherName"))

    # 3. DescriptiveDetail.Collection.TitleDetail.TitleElement.TitleText
    #    （ライトノベルのレーベル名はここに入っていることが多い）
    coll = (onix.get("DescriptiveDetail") or {}).get("Collection")
    coll_list = coll if isinstance(coll, list) else ([coll] if coll else [])
    for c in coll_list:
        if not isinstance(c, dict):
            continue
        td = c.get("TitleDetail") or {}
        tels = td.get("TitleElement") or []
        if isinstance(tels, dict):
            tels = [tels]
        for el in tels:
            if not isinstance(el, dict):
                continue
            tt = el.get("TitleText")
            if isinstance(tt, dict):
                out.append(tt.get("content"))
            elif isinstance(tt, str):
                out.append(tt)

    # 4. summary.series / summary.publisher
    out.append(summary.get("series"))
    out.append(summary.get("publisher"))

    return [s.strip() for s in out if s and isinstance(s, str)]


def _imprint_norm(s: str | None) -> str:
    """imprint 比較用の正規化 (NFKC + 全空白削除)。
    "GAノベル" と "ＧＡノベル" を同一視するため。"""
    if not s:
        return ''
    import unicodedata
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', s))


def matches_imprint(item: dict, imprint: str) -> bool:
    """指定レーベルが含まれるか（部分一致でも採用、NFKC 正規化で全角/半角を吸収）"""
    target = _imprint_norm(imprint)
    for c in _collect_imprint_candidates(item):
        if target in _imprint_norm(c):
            return True
    return False


def extract_imprint(item: dict, target: str | None = None) -> str | None:
    """記録用に最適な1つを返す（ターゲット完全一致を優先）"""
    cands = _collect_imprint_candidates(item)
    if target:
        for c in cands:
            if c == target:
                return c
        for c in cands:
            if target in c:
                return c
    return cands[0] if cands else None


def extract_description(item: dict) -> str | None:
    onix = item.get("onix") or {}
    cd = onix.get("CollateralDetail") or {}
    texts = cd.get("TextContent") or []
    # TextType: 02=Short description, 03=Description, 04=Table of contents...
    by_type: dict[str, str] = {}
    for tc in texts:
        ttype = str(tc.get("TextType") or "")
        text = (tc.get("Text") or "").strip()
        if text:
            by_type.setdefault(ttype, text)
    # 優先: 03（Description） → 02（Short）→ 何でも
    return by_type.get("03") or by_type.get("02") or next(iter(by_type.values()), None)


def extract_record(item: dict, target_imprint: str | None = None) -> dict | None:
    if not item:
        return None
    summary = item.get("summary") or {}
    return {
        "isbn": (summary.get("isbn") or "").strip() or None,
        "title": (summary.get("title") or "").strip() or None,
        "author": (summary.get("author") or "").strip() or None,
        "publisher": (summary.get("publisher") or "").strip() or None,
        "imprint": extract_imprint(item, target=target_imprint),
        "published_date": normalize_pubdate(summary.get("pubdate")),
        "cover_image_url": (summary.get("cover") or "").strip() or None,
        "description": extract_description(item),
        "raw": item,
    }


def fetch_metadata_batch(isbns: list[str]) -> list[dict]:
    """openBD /v1/get に POST で問い合わせ（GETだとURL長すぎる）"""
    r = httpx.post(OPENBD_GET_URL, data={"isbn": ",".join(isbns)}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json() or []


def _meta_content(html: str, prop: str) -> str | None:
    m = re.search(_META_RE_TMPL.format(prop=re.escape(prop)), html)
    return m.group(1) if m else None


def _strip_contributors_from_og_title(og_title: str,
                                       contributors: list[dict]) -> tuple[str | None, str | None]:
    """og:title から contributors の "{name}({role_label})" を削除して title と publisher を返す。

    og:title 例: "ガンパレード・マーチ 芝村 裕吏(著) - アスキー・メディアワークス : KADOKAWA"
                 "本好きの下剋上 第5部[6] 香月美夜(著)、椎名優(イラスト) - TOブックス"

    contributors の name は HTML の data-book-author-name 由来で正規形 ("芝村 裕吏" 等)。
    og:title 中に同じ文字列で含まれる前提で文字列置換する。
    """
    if not og_title:
        return None, None
    body, publisher = og_title, None
    if " - " in body:
        body, _, publisher = body.rpartition(" - ")
        publisher = publisher.strip() or None

    # 長い name から先に削除 (短い name が他の長い name の部分文字列の場合の誤削除を防ぐ)
    for c in sorted(contributors, key=lambda x: -len(x.get("name", ""))):
        name = c.get("name", "")
        label = c.get("role_label", "")
        if not name or not label:
            continue
        # 半角/全角括弧 + 区切り文字バリエーション
        for sep in ["、", "，", ",", " ", "　", ""]:
            for paren in [(f"({label})"), (f"({label})"), (f"[{label}]")]:
                pat = f"{sep}{name}{paren}"
                if pat in body:
                    body = body.replace(pat, "", 1)
                    break
            else:
                continue
            break

    body = body.strip()
    body = re.sub(r'^[、,\s]+|[、,\s]+$', '', body)
    return (body or None), publisher


def fetch_hanmoto_metadata(isbn: str, verbose: bool = False) -> dict | None:
    """hanmoto.com の個別書籍ページHTMLから取れるメタデータを全部返す。
    レーベルフィルタなし（target_imprintチェックはしない）。
    取得失敗時は None。"""
    url = HANMOTO_ISBN_URL.format(isbn=isbn)
    try:
        # 共有クライアント (Keep-Alive) を使うことで TLS 再ネゴをスキップし高速化
        r = _get_http_client().get(url)
    except Exception as e:
        if verbose:
            print(f"    [hanmoto] {isbn}: 例外 {type(e).__name__}: {e}")
        return None
    if r.status_code != 200 or not r.text:
        if verbose:
            print(f"    [hanmoto] {isbn}: status={r.status_code} bytes={len(r.text or '')}")
        return None

    html = r.text
    label_m = _HANMOTO_LABEL_RE.search(html)
    label = label_m.group(1).strip() if label_m else None
    og_title = _meta_content(html, "og:title") or ""
    og_image = _meta_content(html, "og:image")

    # 紹介文: HTML本文の <p itemprop="description"> から完全版を取得
    # 取れなければ og:description (約100文字に truncate されている) に fallback
    full_desc_m = _HANMOTO_DESCRIPTION_RE.search(html)
    if full_desc_m:
        raw = full_desc_m.group(2)
        raw = re.sub(r'<[^>]+>', ' ', raw)        # HTML タグ除去
        import html as _html_mod
        raw = _html_mod.unescape(raw)              # HTMLエンティティを decode
        og_desc = re.sub(r'\s+', ' ', raw).strip() or None
    else:
        og_desc = _meta_content(html, "og:description")
        if og_desc:
            # og:description の "...| - 引用：版元ドットコム" suffix を除去
            og_desc = re.sub(r'\s*[-‐ー―]\s*引用[:：]\s*版元ドットコム\s*$', '', og_desc).strip()
    # 書店発売日 (og:books:release_date)。なければ初版年月日 (book-dates-publish) で代替
    release_date = _meta_content(html, "books:release_date")
    if not release_date:
        publish_m = _HANMOTO_PUBLISH_DATE_RE.search(html)
        if publish_m:
            release_date = publish_m.group(1).strip()
            if verbose:
                print(f"    [hanmoto] {isbn}: 書店発売日なし、初版年月日 {release_date!r} を使用")

    # contributors: 全ONIXロールを {name, role_code, role_label(日本語)} で保存
    contributors: list[dict] = []
    for cm in _HANMOTO_CONTRIB_RE.finditer(html):
        name = (cm.group(1) or "").strip()
        role_code = (cm.group(2) or "").strip()
        role_label = (cm.group(3) or "").strip()
        if name:
            contributors.append({
                "name": name,
                "role_code": role_code,
                "role_label": role_label,
            })

    authors = [c["name"] for c in contributors if c["role_code"] == "A01"]
    illustrators = [c["name"] for c in contributors if c["role_code"] == "A12"]

    # og:title (= "title NameA(著)、NameB(イラスト) - publisher") から
    # contributors の "{name}({role_label})" を文字列削除してタイトル抽出。
    # 正規表現でtitle/authorの境界判定する方法は、author名に空白を含むケース
    # (例: "芝村 裕吏") やサブタイトル付きで判定不能になるため避ける。
    title, publisher = _strip_contributors_from_og_title(og_title.strip(), contributors)

    return {
        "isbn": isbn,
        "url": url,
        "label": label,
        "title": title,
        "author": " / ".join(authors) if authors else None,
        "illustrator": " / ".join(illustrators) if illustrators else None,
        "contributors": contributors,
        "publisher": publisher,
        "published_date": normalize_pubdate(release_date),
        "cover_image_url": og_image or None,
        "description": (og_desc or "").strip() or None,
        "og_title": og_title,
    }


def _make_record_from_meta(isbn: str, meta: dict) -> dict:
    """hanmoto meta dict を openbd_test_books スキーマのレコードに変換"""
    return {
        "isbn": isbn,
        "title": meta["title"],
        "author": meta["author"],
        "illustrator": meta["illustrator"],
        "contributors": meta["contributors"],
        "publisher": meta["publisher"],
        "imprint": meta["label"],
        "published_date": meta["published_date"],
        "cover_image_url": meta["cover_image_url"],
        "description": meta["description"],
        "raw": {
            "source": "hanmoto",
            "url": meta["url"],
            "label": meta["label"],
            "og_title": meta["og_title"],
        },
    }


def fetch_hanmoto_record(isbn: str, target_imprint: str, verbose: bool = False) -> dict | None:
    """hanmoto.com から取得し target_imprint に一致する書籍だけ openbd_test_books スキーマで返す"""
    meta = fetch_hanmoto_metadata(isbn, verbose=verbose)
    if not meta:
        return None
    label = meta["label"]
    if not label:
        if verbose:
            print(f"    [hanmoto] {isbn}: book-label-title が見つからない")
        return None
    # NFKC 正規化でレーベル比較 ("GAノベル" と "ＧＡノベル" を同一視)
    if _imprint_norm(target_imprint) not in _imprint_norm(label):
        if verbose:
            print(f"    [hanmoto] {isbn}: label='{label}' は '{target_imprint}' と不一致")
        return None
    return _make_record_from_meta(isbn, meta)


def filter_isbns_with_openbd(
    candidates: list[str], imprint: str
) -> tuple[list[str], set[str], dict[str, dict]]:
    """openBDを「対象レーベル or 不明」のフィルタとしてのみ使う。
    返り値: (hanmoto で詳細取得すべきISBN一覧, openBD確定ISBN集合, 確定ISBNのopenBDアイテム)
    ISBN一覧の内訳:
      - openBD レコード無し → 不明として残す
      - imprint候補が空 → 不明として残す
      - matches_imprint(target) → 残す
      - 別レーベルが明確 (例: 角川スニーカー文庫等) → 除外
    """
    print(f"[2/3] openBD でレーベル絞り込み (対象 '{imprint}' or 不明 を残す)")
    keep: list[str] = []
    # openBD が厳密一致 (matches_imprint) で確定したISBNと、その openBD アイテム。
    # hanmoto のラベルが短縮形 (例「富士見ファンタジア文庫」→hanmoto「ファンタジア文庫」)
    # でも、これらは確定済みとして救済する。
    confirmed_isbns: set[str] = set()
    confirmed_items: dict[str, dict] = {}
    queried = 0
    confirmed = 0
    unknown = 0
    rejected = 0

    for batch_idx, chunk in enumerate(chunked(candidates, BATCH_SIZE)):
        if batch_idx > 0:
            time.sleep(SLEEP_BETWEEN_BATCH)
        try:
            items = fetch_metadata_batch(chunk)
        except Exception as e:
            print(f"  ! batch {batch_idx+1} 取得失敗: {e} → 全件 不明扱い")
            keep.extend(chunk)
            unknown += len(chunk)
            continue
        queried += len(chunk)

        for req_isbn, it in zip(chunk, items):
            if not it:
                keep.append(req_isbn)
                unknown += 1
                continue
            if matches_imprint(it, imprint):
                keep.append(req_isbn)
                confirmed += 1
                confirmed_isbns.add(req_isbn)
                confirmed_items[req_isbn] = it
            elif not _collect_imprint_candidates(it):
                keep.append(req_isbn)
                unknown += 1
            else:
                rejected += 1

    print(f"  → 照会 {queried:,}件: 対象一致 {confirmed:,} / 不明 {unknown:,} / 別レーベル除外 {rejected:,}")
    print(f"  → hanmoto詳細取得対象: {len(keep):,}件")
    return keep, confirmed_isbns, confirmed_items


def fetch_all_from_hanmoto(isbns: list[str], imprint: str, limit: int | None,
                           other_target_labels: set[str] | None = None,
                           concurrency: int | None = None,
                           openbd_confirmed: set[str] | None = None,
                           openbd_items: dict[str, dict] | None = None
                           ) -> tuple[list[dict], list[dict], list[str]]:
    """hanmoto から各ISBNを並列取得。
    返り値: (target一致, opportunistic, attempted_isbns)

    other_target_labels: 「収集対象だが今回のレーベルではない」レーベル名集合。
    concurrency: 並列接続数 (デフォルト HANMOTO_CONCURRENCY=5)。
    openbd_confirmed: openBD が厳密一致で確定したISBN集合。これらは hanmoto の
      ラベルが短縮形でも (例: target「富士見ファンタジア文庫」/ hanmoto「ファンタジア文庫」)
      target として採用する。ただし hanmoto が「別の収集対象レーベル名」を明示した
      場合はそちらを優先 (opportunistic) して openBD 誤検出を救済する。
    openbd_items: 確定ISBNの openBD アイテム。hanmoto 取得失敗時の fallback 構築用。
    """
    if concurrency is None:
        concurrency = HANMOTO_CONCURRENCY
    print(f"\n[3/3] hanmoto.com で詳細取得 (対象 {len(isbns):,}件, 並列 {concurrency})")
    matched: list[dict] = []
    opportunistic: list[dict] = []
    attempted: list[str] = []
    others = set(other_target_labels or [])
    confirmed_set = set(openbd_confirmed or ())
    items_by_isbn = openbd_items or {}
    rescued = 0  # openBD確定だが hanmoto ラベル不一致/取得失敗を救済した数

    imprint_norm = _imprint_norm(imprint)
    others_norm = {other: _imprint_norm(other) for other in others}

    if not isbns:
        return matched, opportunistic, attempted

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        future_to_isbn = {
            ex.submit(fetch_hanmoto_metadata, isbn, False): isbn
            for isbn in isbns
        }
        completed = 0
        for fut in as_completed(future_to_isbn):
            completed += 1
            isbn = future_to_isbn[fut]
            attempted.append(isbn)
            is_confirmed = isbn in confirmed_set
            try:
                meta = fut.result()
            except Exception:
                meta = None
            if not meta or not meta.get("label"):
                # hanmoto 取得失敗。openBD 確定なら openBD データから救済構築。
                if is_confirmed and isbn in items_by_isbn:
                    orec = extract_record(items_by_isbn[isbn], target_imprint=imprint)
                    if orec and orec.get("title"):
                        orec["imprint"] = imprint
                        matched.append(orec)
                        rescued += 1
                continue
            label = meta["label"]
            label_norm = _imprint_norm(label)
            rec = _make_record_from_meta(isbn, meta)

            # hanmoto が「別の収集対象レーベル名」を明示しているか
            opp_label = None
            for other, other_norm in others_norm.items():
                if other_norm in label_norm:
                    opp_label = other
                    break

            if imprint_norm in label_norm:
                # openBD・hanmoto 双方が一致
                matched.append(rec)
                if len(matched) <= 5 or len(matched) % 50 == 0:
                    print(f"  [{completed}/{len(isbns)}] match: {isbn} 「{rec['title']}」 "
                          f"著={rec.get('author')} 絵={rec.get('illustrator')} "
                          f"(累計 {len(matched)})")
            elif opp_label and not is_confirmed:
                # openBD 未確定 + hanmoto が別レーベルを明示 → そのレーベル下に保存
                opportunistic.append(rec)
                if len(opportunistic) <= 5 or len(opportunistic) % 20 == 0:
                    print(f"  [{completed}/{len(isbns)}] opportunistic ({opp_label}): "
                          f"{isbn} 「{rec['title']}」 (累計 {len(opportunistic)})")
            elif opp_label and is_confirmed:
                # 競合 (openBD=本レーベル / hanmoto=別レーベル) → hanmoto を優先
                opportunistic.append(rec)
                if len(opportunistic) <= 5 or len(opportunistic) % 20 == 0:
                    print(f"  [{completed}/{len(isbns)}] opportunistic({opp_label}/openBD競合): "
                          f"{isbn} 「{rec['title']}」 (累計 {len(opportunistic)})")
            elif is_confirmed:
                # openBD 確定 + hanmoto は短縮形/不明ラベル → target として採用。
                # imprint は正規名 (target) に上書きして表記を統一。
                rec["imprint"] = imprint
                rec["raw"]["openbd_confirmed"] = True
                rec["raw"]["hanmoto_label"] = label
                matched.append(rec)
                rescued += 1
                if rescued <= 5 or rescued % 50 == 0:
                    print(f"  [{completed}/{len(isbns)}] rescue: {isbn} 「{rec['title']}」 "
                          f"(hanmoto='{label}' → '{imprint}') (救済 {rescued})")
            if limit and len(matched) >= limit:
                print(f"  → limit {limit} 到達 (残りの fetch は完了を待たずに中断)")
                # 中断 (残り Future はガベージコレクションで破棄される)
                ex.shutdown(wait=False, cancel_futures=True)
                break
    if rescued:
        print(f"  → openBD確定の救済: {rescued}件 (hanmotoラベル不一致でも target として保存)")
    return matched, opportunistic, attempted


def save_records(supabase, records: list[dict]) -> int:
    saved = 0
    for rec in records:
        try:
            supabase.table("openbd_test_books").upsert(rec, on_conflict="isbn").execute()
            saved += 1
        except Exception as e:
            print(f"  ! 保存失敗 {rec['isbn']}: {e}")
    return saved


def process_specific_isbns(imprint: str, isbns: list[str], dry_run: bool) -> None:
    """指定ISBNだけを処理する診断モード。openBDはフィルタ用、データはhanmotoから"""
    supabase = get_supabase() if not dry_run else None
    print(f"[ISBN指定モード] {len(isbns)}件を処理 imprint='{imprint}'")

    try:
        items = fetch_metadata_batch(isbns)
    except Exception as e:
        print(f"  ! openBD 取得失敗: {e}")
        items = [None] * len(isbns)

    keep: list[str] = []
    for req_isbn, it in zip(isbns, items):
        if not it:
            print(f"  [openBD] {req_isbn}: レコード無し → 不明として残す")
            keep.append(req_isbn)
            continue
        cands = _collect_imprint_candidates(it)
        ok = matches_imprint(it, imprint)
        if ok:
            print(f"  [openBD] {req_isbn}: '{imprint}' 一致 候補={cands}")
            keep.append(req_isbn)
        elif not cands:
            print(f"  [openBD] {req_isbn}: imprint候補なし → 不明として残す")
            keep.append(req_isbn)
        else:
            print(f"  [openBD] {req_isbn}: 別レーベル({cands}) → 除外")

    matched: list[dict] = []
    for idx, isbn in enumerate(keep):
        if idx > 0:
            time.sleep(SLEEP_BETWEEN_HANMOTO)
        rec = fetch_hanmoto_record(isbn, target_imprint=imprint, verbose=True)
        if rec:
            print(f"  [hanmoto] {isbn}: match 「{rec['title']}」 "
                  f"著={rec.get('author')} 絵={rec.get('illustrator')} "
                  f"発売日={rec.get('published_date')}")
            matched.append(rec)
        else:
            print(f"  [hanmoto] {isbn}: 不一致または取得失敗")

    print(f"\n[結果] 一致 {len(matched)} / 入力 {len(isbns)}件")
    if dry_run:
        for r in matched:
            print(f"  - [{r['isbn']}] 「{r['title']}」 著={r['author']} 絵={r['illustrator']} {r['published_date']}")
        print("[DRY RUN] DB保存はスキップ")
        return
    saved = save_records(supabase, matched)
    print(f"\n[完了] {saved}件 保存")


def process_label(imprint: str, candidates: list[str], dry_run: bool,
                  hanmoto_max: int | None = None, limit: int | None = None,
                  skip_existing_set: set[str] | None = None,
                  other_target_labels: set[str] | None = None,
                  concurrency: int | None = None) -> dict:
    """1レーベル分の処理 (フィルタ → hanmoto取得 → 保存)。
    candidates は事前にプレフィックス絞り込み済みのISBNリスト。
    skip_existing_set: hanmoto取得をスキップしたいISBN集合 (DB既存 + セッション内既取得)。
    other_target_labels: 収集対象の他レーベル名集合。hanmoto取得で
      これらに該当した場合、その場で本来のレーベル下にDB保存する (opportunistic)。
    concurrency: hanmoto への並列接続数 (None で HANMOTO_CONCURRENCY デフォルト)。
    戻り値: matched/saved/opportunistic_*/attempted_isbns 含む dict
    """
    supabase = get_supabase() if not dry_run else None

    if skip_existing_set:
        before = len(candidates)
        candidates = [i for i in candidates if i not in skip_existing_set]
        print(f"  → DB既存+セッション既取得 {len(skip_existing_set):,}件を除外 ({before:,} → {len(candidates):,})")

    keep, confirmed_isbns, confirmed_items = filter_isbns_with_openbd(candidates, imprint)
    if hanmoto_max:
        keep = keep[:hanmoto_max]
        print(f"  → --hanmoto-max {hanmoto_max} 適用 → {len(keep):,}件")

    matched_records, opportunistic_records, attempted = fetch_all_from_hanmoto(
        keep, imprint, limit,
        other_target_labels=other_target_labels,
        concurrency=concurrency,
        openbd_confirmed=confirmed_isbns,
        openbd_items=confirmed_items,
    )
    print(f"\n[一致確認] {imprint}: 主一致 {len(matched_records)}件 / "
          f"opportunistic {len(opportunistic_records)}件 / hanmoto試行 {len(attempted):,}件")

    saved = 0
    saved_opp = 0
    if dry_run:
        print("[DRY RUN] DB保存はスキップ")
        for r in matched_records[:20]:
            print(f"  - [{r['isbn']}] 「{r['title']}」 著={r['author']} 絵={r['illustrator']} {r['published_date']}")
        if opportunistic_records:
            print(f"\n[DRY RUN] opportunistic ({len(opportunistic_records)}件): 別レーベル下に保存される予定")
            for r in opportunistic_records[:10]:
                print(f"  - [{r['isbn']}] 「{r['title']}」 imprint={r['imprint']}")
    else:
        print(f"\n[保存中: 主一致 {len(matched_records)}件]")
        saved = save_records(supabase, matched_records)
        if opportunistic_records:
            print(f"[保存中: opportunistic {len(opportunistic_records)}件 (実レーベル下)]")
            saved_opp = save_records(supabase, opportunistic_records)
        print(f"[完了] 主 {saved}件, opportunistic {saved_opp}件 を保存")

    return {
        "imprint": imprint,
        "candidates": len(candidates),
        "filtered": len(keep),
        "matched": len(matched_records),
        "saved": saved,
        "opportunistic_matched": len(opportunistic_records),
        "opportunistic_saved": saved_opp,
        "attempted_isbns": attempted,
    }


def main(imprint: str, prefixes: list[str], limit: int | None,
         dry_run: bool, start_offset: int, hanmoto_max: int | None,
         skip_existing: bool):
    print("[1/3] openBD coverage 取得 + プレフィックス絞り込み")
    isbns = fetch_coverage()
    candidates = filter_by_prefix(isbns, prefixes)
    if start_offset:
        candidates = candidates[start_offset:]
        print(f"  → offset {start_offset} を適用 → {len(candidates):,}件")

    skip_set = _fetch_existing_isbns(get_supabase()) if skip_existing else None
    process_label(imprint, candidates, dry_run, hanmoto_max, limit, skip_set)


def _fetch_existing_isbns(supabase) -> set[str]:
    existing: set[str] = set()
    page = 1000
    offset = 0
    while True:
        rows = (supabase.table("openbd_test_books").select("isbn")
                .range(offset, offset + page - 1).execute().data or [])
        if not rows:
            break
        for r in rows:
            if r.get("isbn"):
                existing.add(r["isbn"])
        if len(rows) < page:
            break
        offset += page
    return existing


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="指定レーベルの書籍を収集。openBDはフィルタ専用、データは全てhanmotoから")
    parser.add_argument("--imprint", default="電撃文庫", help="対象レーベル名")
    parser.add_argument("--prefix", action="append", default=None,
                        help="ISBN プレフィックス（複数指定可）。未指定なら 978404")
    parser.add_argument("--limit", type=int, default=None, help="hanmoto一致件数の上限")
    parser.add_argument("--offset", type=int, default=0, help="候補ISBNのスキップ件数")
    parser.add_argument("--dry-run", action="store_true", help="DBに書き込まない")
    parser.add_argument("--hanmoto-max", type=int, default=None,
                        help="hanmoto試行件数の上限（指定なしで全件）")
    parser.add_argument("--skip-existing", action="store_true",
                        help="DBに既に登録済みのISBNを処理対象から除外する")
    parser.add_argument("--isbns", default=None,
                        help="特定ISBNだけを処理（カンマ区切り）。詳細ログ出力の診断モード")
    args = parser.parse_args()

    if args.isbns:
        isbn_list = [s.strip() for s in args.isbns.split(",") if s.strip()]
        process_specific_isbns(args.imprint, isbn_list, args.dry_run)
    else:
        main(
            imprint=args.imprint,
            prefixes=args.prefix or DEFAULT_PREFIXES,
            limit=args.limit,
            dry_run=args.dry_run,
            start_offset=args.offset,
            hanmoto_max=args.hanmoto_max,
            skip_existing=args.skip_existing,
        )
