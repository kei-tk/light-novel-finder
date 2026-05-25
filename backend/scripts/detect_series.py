"""
著者+タイトル類似で作品単位 (単巻含む) を検出し openbd_test_series へ書き込む。
openbd_test_books の series_id / volume_number もこのスクリプトで埋める。

検出ロジック (テンプレートベース):
  1. 全書籍を取得 (openbd_test_books)
  2. 各タイトルを (template, [numbers]) に分解
     例: "スライム倒して300年、…14" → template="スライム倒して<NUM>年、…<NUM>" numbers=[300,14]
  3. (著者, template) で完全一致するもの同士をグループ化
  4. グループ内で「値が異なる位置」を巻番号位置として特定
     - 複数本: 値が異なる位置 (複数あれば最右)
     - 単本: 一番右の数字を巻番号と仮定
     - 全位置で値が一致: 巻番号判定不能
  5. 巻番号が異なる位置 = 真の巻番号、同一値の位置 = タイトルの一部 (例: "300年")
  6. メタデータ集約 (代表書影=vol1, 発売日範囲 etc.) して upsert
  7. is_series = (グループサイズ>=2) で複数巻/単巻を区別

使い方:
  python -m scripts.detect_series                    # 全書籍を処理 → DB更新
  python -m scripts.detect_series --dry-run          # 検出結果のみ表示
  python -m scripts.detect_series --imprint 電撃文庫  # レーベル絞り込み
  python -m scripts.detect_series --show-all         # 検出を全件表示
  python -m scripts.detect_series --series-only      # 複数巻シリーズだけ処理
"""
import sys
import os
import re
import argparse
import unicodedata
from collections import defaultdict, Counter
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase


# ---------------------------------------------------------------------------
# 表記揺れ吸収用の正規化 (グループキー専用、表示用フィールドには適用しない)
# ---------------------------------------------------------------------------
_WS_PATTERN = re.compile(r'\s+')  # 半角/全角空白を含む全空白


# 「著」相当として扱う contributors の role_label / role_code
# A01=著, A23=原作, A24=原著 を同等に扱う
_AUTHOR_ROLE_LABELS = frozenset({'著', '原著', '原作'})
_AUTHOR_ROLE_CODES = frozenset({'A01', 'A23', 'A24'})

# 特装版/限定版/復刻版を識別するキーワードパターン
# タイトルにこれらが含まれていれば「特装版」と判定し is_special_edition=true にする
_SPECIAL_EDITION_PATTERN = re.compile(
    r'特装版|限定版|豪華版|復刻版|限定セット|プレミアム版|デラックス版'
    r'|【[^】]*?(?:版|セット)】'
    r'|ドラマ\s*CD\s*付|画集付|小冊子付|設定資料集付|アクリルスタンド付'
    r'|スペシャル音声|ジオラマ\s*BOX|フィギュア付|限定版同梱'
)


def _is_special_edition_title(title: str | None) -> bool:
    """書籍タイトルが特装版/限定版を示すか判定"""
    if not title:
        return False
    return bool(_SPECIAL_EDITION_PATTERN.search(title))


def _series_is_special_edition(books: list[dict]) -> bool:
    """シリーズ内の書籍タイトル過半数が特装版なら、シリーズ自体を特装版とみなす"""
    if not books:
        return False
    sp = sum(1 for b in books
             if _is_special_edition_title(b.get('title') or ''))
    return sp > 0 and sp * 2 >= len(books)


def _author_for_matching(b: dict) -> str:
    """series マッチング用の著者を解決する。
    優先順位: A01 (著) > A23/A24 (原作/原著)
    book の author 列が空でも contributors に原著があればそちらを使う。
    """
    author = (b.get('author') or '').strip()
    if author:
        return author
    contributors = b.get('contributors') or []
    candidates: list[str] = []
    for c in contributors:
        if not isinstance(c, dict):
            continue
        if (c.get('role_label') in _AUTHOR_ROLE_LABELS
                or c.get('role_code') in _AUTHOR_ROLE_CODES):
            name = (c.get('name') or '').strip()
            if name:
                candidates.append(name)
    return ' / '.join(candidates)


def _normalize_for_match(s: str | None) -> str:
    """マッチング用に文字列を正規化する。

    - NFKC: 全角ASCII/数字/記号 → 半角、半角カナ → 全角カナ など互換正規化
    - 全空白 (半角/全角空白、タブ等) を削除

    例:
      "アクセル ワールド"  → "アクセルワールド"
      "アクセル　ワールド" → "アクセルワールド"  (全角空白)
      "ＡＢＣ"              → "ABC"
      "川原 礫"             → "川原礫"

    元の表示用フィールド (series_title, author 等) には適用しない。
    """
    if not s:
        return ''
    s = unicodedata.normalize('NFKC', s)
    s = _WS_PATTERN.sub('', s)
    return s

# ---------------------------------------------------------------------------
# テンプレート化 (タイトル中の数字を placeholder に置換)
# ---------------------------------------------------------------------------
_TOKEN_PATTERN = re.compile(
    r'[0-9０-９]+'        # 数字
    r'|[上中下]巻?'        # 上 / 中 / 下 (巻は任意。境界条件なし)
)
_FW_TO_HW = str.maketrans('０１２３４５６７８９', '0123456789')
_LETTER_TOKENS = frozenset('上中下')
# サブ巻として常時扱う括弧ペア (中身が数字でも letter でもサブ巻扱い)
# 例: 〈上〉〈下〉〈1〉 → template から除外し sub_tokens へ
_SUB_BRACKET_PAIRS_ALWAYS = {
    '〈': '〉',
    '《': '》',
}
# letter (上/中/下) のみサブ巻扱いする括弧ペア
# 例: (上) (下) はサブ巻、(1) (2) は通常の巻番号
_SUB_BRACKET_PAIRS_LETTER_ONLY = {
    '(': ')',
    '(': ')',
}


def _matched_token_is_sub(title: str, m) -> bool:
    """マッチした位置がサブ巻括弧で囲まれているか判定。
    - 〈X〉《X》: 数字でも上中下でもサブ巻
    - (X) (X): 上中下のときのみサブ巻 ((1) のような巻番号は通常扱い)
    """
    prev_char = title[m.start() - 1] if m.start() > 0 else ''
    next_char = title[m.end()] if m.end() < len(title) else ''
    if (prev_char in _SUB_BRACKET_PAIRS_ALWAYS
            and next_char == _SUB_BRACKET_PAIRS_ALWAYS[prev_char]):
        return True
    if (prev_char in _SUB_BRACKET_PAIRS_LETTER_ONLY
            and next_char == _SUB_BRACKET_PAIRS_LETTER_ONLY[prev_char]):
        # letter content (上/中/下) のときのみサブ巻
        matched = m.group(0)
        return bool(matched) and matched[0] in _LETTER_TOKENS
    return False
# placeholder は normalized_title 列に保存される。タイトル本文に出現しない
# 制御文字を選び、後続の display 整形で取り除けるようにする。
_PLACEHOLDER = ''


def _parse_token_value(s: str):
    """マッチ文字列を int (数字) または str (上/中/下) に変換"""
    if s and s[0] in _LETTER_TOKENS:
        return s[0]
    return int(s.translate(_FW_TO_HW))


def _make_template(title: str) -> tuple[str, list, list]:
    """title を (template, tokens, sub_tokens) に分解。

    - 通常のトークン (4, '上' 等) → placeholder に置換、tokens に追加
    - 〈〉《》 で囲まれたトークン (例: 〈下〉) → 括弧ごと template から除外し
      sub_tokens に追加。これにより 「4〈上〉」と「5」が同じテンプレートとして
      グループ化できる。

    例:
      "魔王学院の不適合者4〈上〉 ～史上最強～"
        → ("魔王学院の不適合者<P> ～史<P>最強～", [4, '上'], ['上'])
      "魔王学院の不適合者5 ～史上最強～"
        → ("魔王学院の不適合者<P> ～史<P>最強～", [5, '上'], [])
      "スライム倒して300年14" → ("スライム倒して<P>年<P>", [300, 14], [])
    """
    parts: list[str] = []
    tokens: list = []
    sub_tokens: list = []
    last = 0
    for m in _TOKEN_PATTERN.finditer(title):
        is_sub = _matched_token_is_sub(title, m)
        value = _parse_token_value(m.group(0))
        if is_sub:
            # template からは括弧ごと除外
            parts.append(title[last:m.start() - 1])
            sub_tokens.append(value)
            last = m.end() + 1  # 閉じ括弧もスキップ
        else:
            parts.append(title[last:m.start()])
            parts.append(_PLACEHOLDER)
            tokens.append(value)
            last = m.end()
    parts.append(title[last:])
    return ''.join(parts), tokens, sub_tokens


def _detect_volume_position(group_numbers: list[list[int]]) -> int | None:
    """グループ内の数字位置から巻番号の位置を特定。

    - 同一テンプレートの複数本: 値が異なる位置のうち最右
    - 単本: 数字があれば最右位置を巻番号と仮定
    - 全位置で値が一致 (例: 1冊しかない or 完全な重複): None
    """
    if not group_numbers or not group_numbers[0]:
        return None
    n_pos = len(group_numbers[0])
    if len(group_numbers) == 1:
        return n_pos - 1
    differing = [pos for pos in range(n_pos)
                 if len({nums[pos] for nums in group_numbers}) > 1]
    return differing[-1] if differing else None


def _strip_number_at_position(title: str, position: int) -> str:
    """title 中の (sub-bracket を除く) `position` 番目のトークンと
    その周辺の括弧/区切りを除去。series_title (表示用) を生成するために使用。"""
    main_matches = [m for m in _TOKEN_PATTERN.finditer(title)
                    if not _matched_token_is_sub(title, m)]
    if position >= len(main_matches):
        return title
    m = main_matches[position]
    before = title[:m.start()]
    after = title[m.end():]
    # 数字直前の括弧/区切り/句読点を除去
    before = re.sub(r'[\s\.\[\(（【〈《「『：:、,]+$', '', before)
    # 数字直後の閉じ括弧を除去
    after = re.sub(r'^[\]\)）】〉》」』]', '', after)
    after = after.lstrip()
    result = (before + ' ' + after).strip() if after else before.strip()
    # サブ巻括弧の残骸を series_title から除去:
    # - 〈上〉《下》: 常時除去
    # - (上) (下) (中) (上巻) 等: letter content の場合のみ除去
    result = re.sub(r'[〈《][^〉》]*[〉》]', '', result)
    result = re.sub(r'[\(\(]\s*[上中下]巻?\s*[\)\)]', '', result)
    result = re.sub(r'\s+', ' ', result)
    result = re.sub(r'[\s\.、,。:：]+$', '', result)
    return result or title


def _series_title_from_template(template: str, fallback: str) -> str:
    """全位置でvol_pos未確定時のフォールバック: placeholder を消して整形。"""
    s = template.replace(_PLACEHOLDER, '')
    s = re.sub(r'\s+', ' ', s).strip()
    s = re.sub(r'[\s\.、,。:：]+$', '', s)
    return s or fallback


def _rebuild_template_with_fixed(template: str, numbers: list[int],
                                 vol_pos: int | None) -> str:
    """template の vol_pos 以外の placeholder を実値で埋めた文字列を返す。
    normalized_title (ユニークキー) として使用。

    例: template='第\x01部[\x01]', numbers=[5,6], vol_pos=1
        → '第5部[\x01]'
    """
    parts = template.split(_PLACEHOLDER)
    if len(parts) - 1 != len(numbers):
        return template
    out = [parts[0]]
    for i, n in enumerate(numbers):
        if i == vol_pos:
            out.append(_PLACEHOLDER)
        else:
            out.append(str(n))
        out.append(parts[i + 1])
    return ''.join(out)


# ---------------------------------------------------------------------------
# プレフィックスベースのフォールバック検出
# (テンプレートで非シリーズ判定された本に適用)
# ---------------------------------------------------------------------------
_DIGIT_CHARS = frozenset('0123456789０-９')  # 半角/全角数字


def _is_digit_char(c: str) -> bool:
    return ('0' <= c <= '9') or ('０' <= c <= '９')


def _common_prefix(strings: list[str]) -> str:
    """文字列リストの最長共通プレフィックスを返す。
    連続する数字 (例: 11, 12) は1つのトークンとして扱い、桁数や値が違えば
    数字列の手前で切る。これにより「黒の召喚士11」と「黒の召喚士12」の共通
    プレフィックスが「黒の召喚士1」ではなく「黒の召喚士」になる。
    """
    if not strings:
        return ''
    if len(strings) == 1:
        return strings[0]
    s0 = strings[0]
    others = strings[1:]
    prefix_end = 0
    i = 0
    while i < len(s0):
        if _is_digit_char(s0[i]):
            # s0 の数字ランの末尾を探す
            j = i
            while j < len(s0) and _is_digit_char(s0[j]):
                j += 1
            digit_run = s0[i:j]
            # 全ての他文字列が同じ数字ランを持つかチェック
            all_match = True
            for s in others:
                # 数字ランが完全一致 + 直後に数字が続かないこと (= 桁数が一致)
                if s[i:j] != digit_run:
                    all_match = False
                    break
                if j < len(s) and _is_digit_char(s[j]):
                    all_match = False
                    break
            if all_match:
                prefix_end = j
                i = j
            else:
                break
        else:
            c = s0[i]
            all_match = True
            for s in others:
                if i >= len(s) or s[i] != c:
                    all_match = False
                    break
            if all_match:
                prefix_end = i + 1
                i += 1
            else:
                break
    return s0[:prefix_end]


def _word_based_series(books: list[dict], prefix_len: int = 4) -> list[dict]:
    """テンプレート検出で非シリーズだった本に対して、
    著者+イラストレーター一致 + タイトル先頭 prefix_len 文字一致でシリーズ化する。

    巻番号 = 発売日順
    series_title = クラスタの最長共通プレフィックス
    """
    if prefix_len < 1:
        return []
    # 正規化したキーでグループ化 (表記揺れ吸収)
    by_key: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for b in books:
        # A01 著者が無い場合は 原著 (A23/A24) で代替
        author = _author_for_matching(b)
        illustrator = (b.get('illustrator') or '').strip()
        if not author or not illustrator:
            continue
        title = (b.get('title') or '').strip()
        title_norm = _normalize_for_match(title)
        if len(title_norm) < prefix_len:
            continue
        prefix_key = title_norm[:prefix_len]
        author_key = _normalize_for_match(author)
        illustrator_key = _normalize_for_match(illustrator)
        b['_match_author'] = author
        by_key[(author_key, illustrator_key, prefix_key)].append(b)

    series_list: list[dict] = []
    for (author_key, illustrator_key, prefix), pair_books in by_key.items():
        # 表示用に元データから最頻値を採用
        author = (_most_common_value([b.get('author') for b in pair_books])
                  or _most_common_value([b.get('_match_author') for b in pair_books])
                  or author_key)
        illustrator = _most_common_value([b.get('illustrator') for b in pair_books]) or illustrator_key
        if len(pair_books) < 2:
            continue

        # 巻番号 = 発売日昇順 (発売日不明は末尾)
        sorted_cluster = sorted(
            pair_books,
            key=lambda x: (x.get('published_date') or '9999-12-31', x.get('isbn') or ''),
        )
        for idx, b in enumerate(sorted_cluster, 1):
            b['_volume_number'] = idx
            b['_volume_number_sub'] = None

        # series_title: タイトルの最長共通プレフィックス
        # (見栄えのため、末尾の区切り/句読点/単独空白を除去)
        all_titles = [b.get('title') or '' for b in pair_books]
        series_title = _common_prefix(all_titles)
        series_title = re.sub(r'[\s\.、,。:：・~～\-―ー]+$', '', series_title) or prefix

        agg = _aggregate_series_metadata(sorted_cluster)

        # normalized_title: \x02 prefix で word-based を区別 + 正規化キーで一意化
        normalized_title = f'\x02{prefix}\x02{author_key}\x02{illustrator_key}'

        series_list.append({
            'author': author,
            'series_title': series_title,
            'normalized_title': normalized_title,
            'is_series': True,
            'is_special_edition': _series_is_special_edition(sorted_cluster),
            'books': sorted_cluster,
            **agg,
        })
    return series_list


def _merge_singles_into_multi(series_list: list[dict],
                              prefix_len: int = 4) -> tuple[list[dict], int]:
    """is_series=true のシリーズに、同 author で
    series_title を prefix とする単巻があれば吸収する。

    ケース例:
      多巻シリーズ「…の俺1人」 (vol 2, vol 3) と
      単巻「…の俺1人」 (vol 1, タイトルに巻数なし) を1シリーズに統合。

      多巻「お嬢様、いけないことをたくらんでいます！」 (vol 2, 3, ...) と
      単巻「お嬢様、いけないことをたくらんでいます！」 (vol 1) を統合。

    マッチ条件:
      - 著者完全一致 (illustrator は問わない: vol 1 の illustrator が
        欠落 / 多巻側と異なるケースに対応)
      - 単巻のタイトルが多巻 series_title で始まる
      - series_title の長さが prefix_len 文字以上 (短すぎる誤マッチ防止)

    巻番号は発売日順で再採番。
    Returns: (new_series_list, merge_count)
    """
    # 著者の正規化形でインデックス化
    by_author_key: dict[str, list[dict]] = defaultdict(list)
    for s in series_list:
        if not s.get('is_series'):
            continue
        author = (s.get('author') or '').strip()
        if not author:
            continue
        by_author_key[_normalize_for_match(author)].append(s)

    if not by_author_key:
        return series_list, 0

    to_remove_indices: set[int] = set()
    merge_count = 0
    for i, s in enumerate(series_list):
        if s.get('is_series'):
            continue
        if not s.get('books'):
            continue
        single_book = s['books'][0]
        author = (s.get('author') or '').strip()
        if not author:
            continue
        title = (single_book.get('title') or '').strip()
        if not title:
            continue
        author_key = _normalize_for_match(author)
        title_key = _normalize_for_match(title)
        candidates = by_author_key.get(author_key, [])
        # 単巻のタイトル(正規化) が series_title(正規化) で始まる中で最長の親を選ぶ
        best = None
        best_len = 0
        for m in candidates:
            st = (m.get('series_title') or '').strip()
            if len(st) < prefix_len:
                continue
            st_key = _normalize_for_match(st)
            if title_key.startswith(st_key) and len(st_key) > best_len:
                best = m
                best_len = len(st_key)
        if best is None:
            continue

        # マージ: 発売日順で再採番
        all_books = list(best['books']) + [single_book]
        sorted_books = sorted(
            all_books,
            key=lambda b: (b.get('published_date') or '9999-12-31',
                           b.get('isbn') or ''),
        )
        for idx, b in enumerate(sorted_books, 1):
            b['_volume_number'] = idx
            b['_volume_number_sub'] = None
        best['books'] = sorted_books
        best['is_special_edition'] = _series_is_special_edition(sorted_books)
        agg = _aggregate_series_metadata(sorted_books)
        for k, v in agg.items():
            best[k] = v
        to_remove_indices.add(i)
        merge_count += 1

    new_list = [s for idx, s in enumerate(series_list)
                if idx not in to_remove_indices]
    return new_list, merge_count


def _most_common_value(values: list):
    """最頻値を返す (None/空文字は除外)。同数なら最初に出現したもの。"""
    filtered = [v for v in values if v]
    if not filtered:
        return None
    return Counter(filtered).most_common(1)[0][0]


def _aggregate_series_metadata(books: list[dict]) -> dict:
    """シリーズに属する書籍リストから、シリーズレベルのメタデータを集約。
    cover/description/contributors は vol 1 (なければ最古) を代表値とする。
    """
    sorted_books = sorted(
        books,
        key=lambda b: (b.get('_volume_number') if b.get('_volume_number') is not None else 9999,
                       b.get('published_date') or '9999')
    )
    vol1 = sorted_books[0]

    pub_dates = [b.get('published_date') for b in books if b.get('published_date')]

    return {
        'illustrator': _most_common_value([b.get('illustrator') for b in books]),
        'contributors': vol1.get('contributors'),
        'publisher': _most_common_value([b.get('publisher') for b in books]),
        'imprint': _most_common_value([b.get('imprint') for b in books]),
        'cover_image_url': vol1.get('cover_image_url'),
        'description': vol1.get('description'),
        'volume_count': len(books),
        'first_published_date': min(pub_dates) if pub_dates else None,
        'latest_published_date': max(pub_dates) if pub_dates else None,
    }


def _consolidate_parent_chains(series_list: list[dict]) -> tuple[list[dict], int]:
    """Phase 3 で「親=単巻 + 子=単巻 (複数)」となった連鎖を、1つの多巻シリーズに統合。

    例: 14冊の「魔女の結婚 : 〇〇」(全冊 illustrator NULL、巻数なし) は
    Phase 1/2/2.5 ではグループ化されないが、Phase 3 で
      - 親「魔女の結婚」 (1冊, single)
      - 子「魔女の結婚 : 〇〇」(13冊, all single) が検出される
    ここでまとめて 14冊の multi-series に昇格させる。

    巻番号は発売日順で再採番。
    Returns: (new_series_list, converted_count)
    """
    # _parent_normalized_title → 子エントリ群
    child_map: dict[str, list[dict]] = defaultdict(list)
    for s in series_list:
        pnt = s.get('_parent_normalized_title')
        if pnt:
            child_map[pnt].append(s)

    by_norm_title: dict[str, dict] = {s['normalized_title']: s for s in series_list}

    to_remove_indices: set[int] = set()
    converted = 0
    for parent_nt, children in child_map.items():
        parent = by_norm_title.get(parent_nt)
        if parent is None:
            continue
        # 親が既に multi-series の場合は Phase 2.5 で処理済み (ここでは触らない)
        if parent.get('is_series'):
            continue
        # 単巻の子だけを統合対象にする (multi-series の子は Phase 3 の link を維持)
        single_children = [c for c in children if not c.get('is_series')]
        if not single_children:
            continue
        all_books: list[dict] = list(parent.get('books') or [])
        for c in single_children:
            all_books.extend(c.get('books') or [])
        if len(all_books) < 2:
            continue
        sorted_books = sorted(
            all_books,
            key=lambda b: (b.get('published_date') or '9999-12-31', b.get('isbn') or ''),
        )
        for idx, b in enumerate(sorted_books, 1):
            b['_volume_number'] = idx
            b['_volume_number_sub'] = None
        parent['books'] = sorted_books
        parent['is_series'] = True
        parent['is_special_edition'] = _series_is_special_edition(sorted_books)
        agg = _aggregate_series_metadata(sorted_books)
        for k, v in agg.items():
            parent[k] = v
        # 親自身の parent 紐付けがあった場合は (子に統合されるべき子供がいる時点で) 解除
        parent.pop('_parent_normalized_title', None)

        child_ids = {id(c) for c in single_children}
        for i, s in enumerate(series_list):
            if id(s) in child_ids:
                to_remove_indices.add(i)
        converted += 1

    new_list = [s for idx, s in enumerate(series_list) if idx not in to_remove_indices]
    return new_list, converted


def _detect_parent_relationships(series_list: list[dict],
                                  min_match_len: int = 4) -> int:
    """同著者で series_title 間の包含関係 (親子) を検出。
    各 series_list エントリに `_parent_normalized_title` を設定する。

    親候補の優先順:
      - series_title が他のseries_title の strict substring で min_match_len 文字以上
      - 該当する親が複数あれば、最も長い series_title を持つ親を選択 (=より具体的な親)

    例:
      親「本好きの下剋上」 vs 子「本好きの下剋上 第一部」
      親「ソードアート・オンライン」 vs 子「ソードアート・オンライン プログレッシブ」

    Returns: 紐付けた件数
    """
    # 著者の正規化キーで突き合わせ ("川原 礫" と "川原礫" を同一視)
    by_author_key: dict[str, list[dict]] = defaultdict(list)
    for s in series_list:
        author_key = _normalize_for_match(s.get('author') or '')
        by_author_key[author_key].append(s)

    linked = 0
    for author_key, group in by_author_key.items():
        if len(group) < 2:
            continue
        # 各シリーズの series_title 正規化形を事前計算
        norm_cache = {id(s): _normalize_for_match(s.get('series_title') or '')
                      for s in group}
        for child in group:
            child_norm = norm_cache[id(child)]
            if not child_norm:
                continue
            best_parent = None
            best_parent_len = 0
            for parent in group:
                if parent is child:
                    continue
                p_norm = norm_cache[id(parent)]
                if len(p_norm) < min_match_len:
                    continue
                if len(p_norm) >= len(child_norm):
                    continue
                if p_norm not in child_norm:
                    continue
                if len(p_norm) > best_parent_len:
                    best_parent = parent
                    best_parent_len = len(p_norm)
            if best_parent is not None:
                child['_parent_normalized_title'] = best_parent['normalized_title']
                linked += 1
    return linked


def _link_special_to_main(series_list: list[dict]) -> int:
    """特装版シリーズを同タイトルの本編シリーズ (非特装版) に親子紐付け。

    series_title が同じ + 著者が同じ で特装版/通常版 が分かれている場合、
    特装版側に parent_series_id を設定する。
    例: 「魔女の旅々」M/26 (通常) + 「魔女の旅々」M/5 (特装版) → 後者を前者に紐付け
    """
    # (author_key, title_key) で兄弟を集める
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in series_list:
        a_key = _normalize_for_match(s.get('author') or '')
        t_key = _normalize_for_match(s.get('series_title') or '')
        if a_key and t_key:
            by_key[(a_key, t_key)].append(s)

    linked = 0
    for (a_key, t_key), siblings in by_key.items():
        if len(siblings) < 2:
            continue
        specials = [s for s in siblings if s.get('is_special_edition')]
        mains = [s for s in siblings if not s.get('is_special_edition')]
        if not specials or not mains:
            continue
        # 親は最も多巻の本編を採用 (最も「メイン」っぽいもの)
        parent = max(mains, key=lambda x: (x.get('volume_count', 0), x.get('is_series', False)))
        for sp in specials:
            if sp is parent:
                continue
            # 既に他の親があっても上書き (特装版にとっては本編が一番自然な親)
            sp['_parent_normalized_title'] = parent['normalized_title']
            linked += 1
    return linked


def detect_series(books: list[dict], min_group_size: int = 1,
                  enable_word_fallback: bool = True,
                  prefix_len: int = 4,
                  enable_parent_link: bool = True,
                  min_parent_len: int = 4) -> list[dict]:
    """書籍リストから作品単位 (単巻含む) を検出。

    Phase 1: テンプレートベース (数値置換による完全一致 → サブグループ化)
    Phase 2 (enable_word_fallback): Phase 1 で is_series=false だった本に対して
        著者+イラストレーター一致 + タイトル先頭 prefix_len 文字一致でシリーズ化
    Phase 3 (enable_parent_link): 同著者で series_title 同士に包含関係があれば
        子→親の紐付け (parent_series_id) を設定。データはマージせず関係のみ記録。

    returns: [{series_title, author, normalized_title, is_series, books,
               _parent_normalized_title?, ...集約フィールド}]
    """
    # (author_normalized, template_normalized) → list[(book, numbers)]
    # 表記揺れ ("川原 礫" vs "川原礫", "アクセル ワールド" vs "アクセルワールド")
    # を吸収するため正規化形をキーに使う
    groups: dict[tuple[str, str], list[tuple[dict, list[int]]]] = defaultdict(list)
    skipped_no_author = 0
    skipped_no_title = 0
    for b in books:
        # A01 著者が無い場合は原著 (A23/A24) で代替
        author = _author_for_matching(b)
        title = (b.get('title') or '').strip()
        if not author:
            skipped_no_author += 1
            continue
        if not title:
            skipped_no_title += 1
            continue
        template, numbers, sub_tokens = _make_template(title)
        b['_sub_tokens'] = sub_tokens
        b['_match_author'] = author  # 後段で再利用
        author_key = _normalize_for_match(author)
        template_key = _normalize_for_match(template)
        groups[(author_key, template_key)].append((b, numbers))

    print(f"  → 著者なしスキップ: {skipped_no_author}, タイトルなし: {skipped_no_title}")
    print(f"  → ユニーク (著者, テンプレート) グループ: {len(groups):,}")

    series_list: list[dict] = []
    for (author_key, template), entries in groups.items():
        if not entries:
            continue
        # 表示用の著者: 元の author 列の最頻値、無ければ matching 用 (原著)
        author = (_most_common_value([b.get('author') for b, _ in entries])
                  or _most_common_value([b.get('_match_author') for b, _ in entries])
                  or author_key)
        # Step 1: グループ全体で vol_pos を決定
        group_numbers = [nums for _, nums in entries]
        vol_pos = _detect_volume_position(group_numbers)

        # Step 2: vol_pos 以外の数値で更にサブグループ化
        # 例: 第5部[6], 第5部[7], 第6部[1] → vol_pos=1 (右端)
        #     位置0 (部番号) で分割: (5,) → 2冊, (6,) → 1冊
        if vol_pos is not None and group_numbers and len(group_numbers[0]) > 1:
            sub_groups: dict[tuple, list[tuple[dict, list[int]]]] = defaultdict(list)
            for b, nums in entries:
                fixed_key = tuple(n for i, n in enumerate(nums) if i != vol_pos)
                sub_groups[fixed_key].append((b, nums))
        else:
            sub_groups = {(): list(entries)}

        for fixed_key, sub_entries in sub_groups.items():
            if len(sub_entries) < min_group_size:
                continue

            # vol_pos 位置に上中下 (str) があれば発売日順で巻番号を振る
            vol_pos_has_letter = (
                vol_pos is not None
                and any(
                    vol_pos < len(nums) and isinstance(nums[vol_pos], str)
                    for _, nums in sub_entries
                )
            )

            if vol_pos_has_letter:
                date_sorted = sorted(
                    sub_entries,
                    key=lambda x: (x[0].get('published_date') or '9999-12-31',
                                   x[0].get('isbn') or ''),
                )
                for idx, (b, _) in enumerate(date_sorted, 1):
                    b['_volume_number'] = idx
            else:
                for b, nums in sub_entries:
                    if vol_pos is not None and vol_pos < len(nums):
                        b['_volume_number'] = nums[vol_pos]
                    else:
                        b['_volume_number'] = None

            # サブ巻番号 (volume_number_sub): 同じ volume_number に複数本が
            # 該当する場合 (例: 4〈上〉と 4〈下〉が同 vol 4)、発売日順で 1, 2, ...
            books_by_vol: dict = defaultdict(list)
            for b, _ in sub_entries:
                vol = b.get('_volume_number')
                if vol is None:
                    b['_volume_number_sub'] = None
                    continue
                books_by_vol[vol].append(b)
            for vol_num, vol_group in books_by_vol.items():
                if len(vol_group) <= 1:
                    vol_group[0]['_volume_number_sub'] = None
                else:
                    date_sorted_books = sorted(
                        vol_group,
                        key=lambda b: (b.get('published_date') or '9999-12-31',
                                       b.get('isbn') or ''),
                    )
                    for idx, b in enumerate(date_sorted_books, 1):
                        b['_volume_number_sub'] = idx

            # series_title 決定
            if vol_pos is not None:
                # 上中下の場合は最初に発売された本のタイトルを基準にする
                if vol_pos_has_letter:
                    sorted_sub = sorted(
                        sub_entries,
                        key=lambda x: (x[0].get('published_date') or '9999-12-31',
                                       x[0].get('isbn') or ''),
                    )
                else:
                    sorted_sub = sorted(
                        sub_entries,
                        key=lambda x: (x[1][vol_pos] if vol_pos < len(x[1]) else 9999),
                    )
                rep_title = sorted_sub[0][0]['title']
                series_title = _strip_number_at_position(rep_title, vol_pos)
            else:
                series_title = _series_title_from_template(
                    template, sub_entries[0][0]['title'])

            # normalized_title: 固定値を埋めて vol_pos のみ placeholder を残す
            # 同 template でも固定値が違えば別ユニーク key になる
            sample_numbers = sub_entries[0][1]
            normalized_title = _rebuild_template_with_fixed(
                template, sample_numbers, vol_pos)

            sorted_books = sorted(
                [b for b, _ in sub_entries],
                key=lambda x: (x.get('_volume_number') if x.get('_volume_number') is not None else 9999,
                               x.get('isbn')),
            )
            agg = _aggregate_series_metadata(sorted_books)

            series_list.append({
                'author': author,
                'series_title': series_title,
                'normalized_title': normalized_title,
                'is_series': len(sub_entries) >= 2,
                'is_special_edition': _series_is_special_edition(sorted_books),
                'books': sorted_books,
                **agg,
            })

    # Phase 2: 単語ベースのフォールバック (テンプレで非シリーズだった本に対して)
    if enable_word_fallback:
        single_entries = [s for s in series_list if not s['is_series']]
        single_books = [b for s in single_entries for b in s['books']]
        if single_books:
            print(f"  → Phase 2 (prefix-based, 先頭{prefix_len}文字一致): "
                  f"{len(single_books):,}冊の単本を再検査...")
            word_series = _word_based_series(single_books, prefix_len=prefix_len)
            in_word_cluster: set[str] = set()
            for ws in word_series:
                for b in ws['books']:
                    in_word_cluster.add(b.get('isbn'))
            print(f"  → {len(word_series):,}件のword-based シリーズを検出 "
                  f"({len(in_word_cluster):,}冊)")
            # Word-based クラスタに入った単本を series_list から除去
            series_list = [
                s for s in series_list
                if s['is_series'] or not any(b.get('isbn') in in_word_cluster for b in s['books'])
            ]
            series_list.extend(word_series)

    # Phase 2.5: 単巻 → 多巻シリーズ吸収
    # 例: タイトルに巻数のない vol 1 (単巻判定) を、巻数付きの vol 2/3 シリーズに統合
    if enable_word_fallback:
        before = len(series_list)
        series_list, merged = _merge_singles_into_multi(series_list, prefix_len=prefix_len)
        print(f"  → Phase 2.5 (single→multi 吸収): {merged:,}件の単巻を多巻シリーズに統合 "
              f"({before:,} → {len(series_list):,})")

    # Phase 3: 同著者で series_title 包含関係を検出 (親子関係)
    if enable_parent_link:
        linked = _detect_parent_relationships(series_list, min_match_len=min_parent_len)
        print(f"  → Phase 3 (parent link, ≥{min_parent_len}文字): {linked:,}件の親子紐付け")

        # Phase 3.5: 同タイトル+同著者の特装版を本編シリーズに紐付け
        sp_linked = _link_special_to_main(series_list)
        print(f"  → Phase 3.5 (特装版 → 本編 紐付け): {sp_linked:,}件")

        # Phase 4: 親子関係が「全部単巻」のチェーンを多巻シリーズに昇格
        # (例: illustrator NULL のため Phase 1/2/2.5 で漏れた多巻シリーズを救済)
        before = len(series_list)
        series_list, consolidated = _consolidate_parent_chains(series_list)
        print(f"  → Phase 4 (single chain → multi 昇格): {consolidated:,}件 "
              f"({before:,} → {len(series_list):,})")

    return series_list


# ---------------------------------------------------------------------------
# DB I/O
# ---------------------------------------------------------------------------
_BOOK_COLUMNS = (
    'isbn, title, author, illustrator, contributors, publisher, '
    'imprint, cover_image_url, description, published_date, volume_number'
)


def fetch_all_books(supabase, imprint: str | None = None,
                    page_size: int = 1000) -> list[dict]:
    print("[1/3] DB から書籍取得中...")
    all_books: list[dict] = []
    offset = 0
    while True:
        q = supabase.table('openbd_test_books').select(_BOOK_COLUMNS)
        if imprint:
            q = q.eq('imprint', imprint)
        rs = q.range(offset, offset + page_size - 1).execute()
        if not rs.data:
            break
        all_books.extend(rs.data)
        if len(rs.data) < page_size:
            break
        offset += page_size
    print(f"  → {len(all_books):,}件取得")
    return all_books


# --- シリーズ行 (Gemini エンリッチ前) で書き込むカラム --------------
# Gemini が埋める synopsis/atmosphere_tags/genre_tags/sub_genre_tags/target_audience は
# 含めない (既存値を上書きしないため)
_SERIES_UPSERT_FIELDS = [
    'normalized_title',  # ユニーク識別子 (数字完全除去後の形)
    'series_title',      # 表示用 (共通プレフィックス。衝突可能)
    'author', 'illustrator', 'contributors', 'publisher',
    'imprint', 'cover_image_url', 'description',
    'volume_count', 'first_published_date', 'latest_published_date',
    'is_series',         # true: 複数巻のシリーズ / false: 単巻のみ
    'is_special_edition',  # true: 特装版/限定版シリーズ
]


def update_db_batch(supabase, series_list: list[dict], batch_size: int = 500
                    ) -> tuple[int, int, int]:
    """openbd_test_series に upsert → series_id を取得 → openbd_test_books を更新。
    返り値: (saved_series_count, linked_books_count, parent_link_count)

    ユニーク制約は (normalized_title, author)。バッチ内重複は事前にチェック。
    """
    print(f"\n[3/3] DB更新中... (batch_size={batch_size})")

    # Step 0: バッチ内重複チェック (本来あってはならない)
    seen_keys: set[tuple[str, str]] = set()
    duplicates: list[tuple[str, str]] = []
    for s in series_list:
        key = (s['normalized_title'], s['author'])
        if key in seen_keys:
            duplicates.append(key)
        seen_keys.add(key)
    if duplicates:
        print(f"  ! 警告: series_list 内に (normalized_title, author) 重複 {len(duplicates)}件")
        for k in duplicates[:5]:
            print(f"      {k}")

    # Step 1: シリーズ行を upsert
    series_records = [
        {field: s.get(field) for field in _SERIES_UPSERT_FIELDS}
        for s in series_list
    ]
    print(f"  [series] upsert {len(series_records):,}件...")
    saved_series = 0
    series_id_map: dict[tuple[str, str], str] = {}

    for i in range(0, len(series_records), batch_size):
        batch = series_records[i:i+batch_size]
        try:
            rs = supabase.table('openbd_test_series').upsert(
                batch,
                on_conflict='normalized_title,author',
                returning='representation',
            ).execute()
            saved_series += len(batch)
            for row in rs.data or []:
                series_id_map[(row['normalized_title'], row['author'])] = row['id']
            print(f"    series batch {i // batch_size + 1}: +{len(batch)} (累計 {saved_series})")
        except Exception as e:
            print(f"    ! series batch {i // batch_size + 1} 失敗: {e}")

    # Step 2: openbd_test_books に series_id と volume_number を書き戻し
    book_records: list[dict] = []
    missing_series = 0
    for s in series_list:
        sid = series_id_map.get((s['normalized_title'], s['author']))
        if not sid:
            missing_series += 1
            continue
        for b in s['books']:
            book_records.append({
                'isbn': b['isbn'],
                'series_id': sid,
                'volume_number': b.get('_volume_number'),
                'volume_number_sub': b.get('_volume_number_sub'),
            })
    if missing_series:
        print(f"  ! {missing_series}シリーズ分の id 取得に失敗 → 該当書籍はリンク無し")

    print(f"  [books] series_id 紐付け {len(book_records):,}件...")
    linked = 0
    for i in range(0, len(book_records), batch_size):
        batch = book_records[i:i+batch_size]
        try:
            supabase.table('openbd_test_books').upsert(batch, on_conflict='isbn').execute()
            linked += len(batch)
            print(f"    books batch {i // batch_size + 1}: +{len(batch)} (累計 {linked})")
        except Exception as e:
            print(f"    ! books batch {i // batch_size + 1} 失敗: {e}")

    # Step 3: parent_series_id を設定 (Phase 3 で見つかった親子関係)
    parent_link_count = 0
    parent_targets: list[tuple[str, str]] = []  # (child_id, parent_id)
    for s in series_list:
        parent_norm = s.get('_parent_normalized_title')
        if not parent_norm:
            continue
        child_id = series_id_map.get((s['normalized_title'], s['author']))
        parent_id = series_id_map.get((parent_norm, s['author']))
        if child_id and parent_id and child_id != parent_id:
            parent_targets.append((child_id, parent_id))

    if parent_targets:
        print(f"  [series] parent_series_id 紐付け {len(parent_targets):,}件...")
        for idx, (child_id, parent_id) in enumerate(parent_targets):
            try:
                supabase.table('openbd_test_series').update(
                    {'parent_series_id': parent_id}
                ).eq('id', child_id).execute()
                parent_link_count += 1
            except Exception as e:
                print(f"    ! parent link {idx + 1} 失敗 (child={child_id}): {e}")
        print(f"    parent links saved: {parent_link_count}/{len(parent_targets)}")

    return saved_series, linked, parent_link_count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='作品単位検出 (単巻含む)')
    parser.add_argument('--dry-run', action='store_true',
                        help='DB更新せず検出結果のみ表示')
    parser.add_argument('--imprint', default=None,
                        help='レーベル絞り込み (例: 電撃文庫)')
    parser.add_argument('--min-group-size', type=int, default=1,
                        help='対象とする最小冊数 (デフォルト1=単巻含む)')
    parser.add_argument('--show-all', action='store_true',
                        help='検出した作品を全件表示')
    parser.add_argument('--show-top', type=int, default=20,
                        help='Top Nを表示 (デフォルト20)')
    parser.add_argument('--series-only', action='store_true',
                        help='シリーズ作品 (is_series=true) のみ処理')
    parser.add_argument('--no-word-fallback', action='store_true',
                        help='プレフィックスベースの2段階目検出を無効化')
    parser.add_argument('--prefix-len', type=int, default=4,
                        help='Phase 2 で同シリーズ判定するタイトル先頭一致文字数 (デフォルト4)')
    parser.add_argument('--no-parent-link', action='store_true',
                        help='シリーズ間 parent 紐付け (Phase 3) を無効化')
    parser.add_argument('--min-parent-len', type=int, default=4,
                        help='parent 紐付けに使う series_title 最小文字数 (デフォルト4)')
    args = parser.parse_args()

    supabase = get_supabase()
    books = fetch_all_books(supabase, imprint=args.imprint)

    print(f"\n[2/3] 作品検出中... (最小{args.min_group_size}冊, "
          f"prefix-fallback={'OFF' if args.no_word_fallback else f'ON 先頭{args.prefix_len}文字一致'}, "
          f"parent紐付け={'OFF' if args.no_parent_link else f'ON ≥{args.min_parent_len}文字'})")
    series_list = detect_series(
        books,
        min_group_size=args.min_group_size,
        enable_word_fallback=not args.no_word_fallback,
        prefix_len=args.prefix_len,
        enable_parent_link=not args.no_parent_link,
        min_parent_len=args.min_parent_len,
    )
    if args.series_only:
        before = len(series_list)
        series_list = [s for s in series_list if s.get('is_series')]
        print(f"  → --series-only: {before:,} → {len(series_list):,} (シリーズのみ)")

    multi = [s for s in series_list if s.get('is_series')]
    single = [s for s in series_list if not s.get('is_series')]
    multi_books = sum(len(s['books']) for s in multi)
    single_books = sum(len(s['books']) for s in single)
    print(f"  → シリーズ (is_series=true):  {len(multi):,}件 ({multi_books:,}冊)")
    print(f"  → 単巻   (is_series=false): {len(single):,}件 ({single_books:,}冊)")

    # サンプル表示 (シリーズだけ表示。単巻は数だけ)
    sorted_multi = sorted(multi, key=lambda s: -len(s['books']))
    n_show = len(sorted_multi) if args.show_all else min(args.show_top, len(sorted_multi))
    if n_show:
        print(f"\n[Top {n_show} シリーズ (is_series=true)]")
        for series in sorted_multi[:n_show]:
            print(f"\n  ★ 「{series['series_title']}」 著={series['author']} "
                  f"絵={series.get('illustrator')} ({series['volume_count']}冊)")
            print(f"      レーベル={series.get('imprint')}  "
                  f"発売日={series.get('first_published_date')}〜{series.get('latest_published_date')}")
            for b in series['books'][:5]:
                vol = b.get('_volume_number')
                sub = b.get('_volume_number_sub')
                vol_disp = f"{vol}-{sub}" if sub is not None else f"{vol}"
                print(f"      [{b['isbn']}] vol={vol_disp}: {b['title'][:60]}")
            if len(series['books']) > 5:
                print(f"      ... 他 {len(series['books']) - 5}冊")

    if single and (args.show_all or args.show_top):
        print(f"\n[単巻サンプル (is_series=false) 5件]")
        for series in single[:5]:
            b = series['books'][0]
            print(f"  - 「{series['series_title']}」 著={series['author']} "
                  f"レーベル={series.get('imprint')} [{b['isbn']}]")

    if args.dry_run:
        print("\n[DRY RUN] DB更新はスキップ")
        return

    if not series_list:
        print("\n更新対象なし")
        return

    saved_series, linked_books, parent_links = update_db_batch(supabase, series_list)
    print(f"\n[完了] openbd_test_series: {saved_series:,}件 upsert "
          f"(シリーズ {len(multi):,} + 単巻 {len(single):,}) / "
          f"openbd_test_books: {linked_books:,}件 series_id 紐付け / "
          f"parent_series_id: {parent_links:,}件 紐付け")


if __name__ == '__main__':
    main()
