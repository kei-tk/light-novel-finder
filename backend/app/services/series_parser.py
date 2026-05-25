"""
書籍タイトルから「シリーズ名」と「巻数」を分離するユーティリティ。

例:
  「○○の物語 3」       → ("○○の物語", 3)
  「○○の物語(3)」     → ("○○の物語", 3)
  「○○の物語（３）」   → ("○○の物語", 3)
  「○○の物語 第3巻」   → ("○○の物語", 3)
  「○○の物語 III」     → ("○○の物語", 3)
  「○○の物語」          → ("○○の物語", None)
"""
import re
import unicodedata


_VOL_PATTERNS = [
    # 末尾の「 第3巻」「 3巻」「 第3」
    re.compile(r"^(.+?)[\s　]+(?:第)?\s*([0-9０-９]+)\s*巻\s*$"),
    # 末尾の「(3)」「（3）」
    re.compile(r"^(.+?)\s*[\(（]\s*([0-9０-９]+)\s*[\)）]\s*$"),
    # 末尾の「 3」「 03」（半角/全角スペース区切り）
    re.compile(r"^(.+?)[\s　]+([0-9０-９]+)\s*$"),
    # ローマ数字（II, III, IV, V, VI, VII, VIII, IX, X）
    re.compile(r"^(.+?)[\s　]+(II|III|IV|V|VI|VII|VIII|IX|X)\s*$"),
]

_ROMAN_MAP = {
    "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5,
    "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10,
}


def _to_int(s: str) -> int | None:
    """全角/半角数字 or ローマ数字を int に"""
    if s in _ROMAN_MAP:
        return _ROMAN_MAP[s]
    try:
        return int(unicodedata.normalize("NFKC", s))
    except ValueError:
        return None


def parse_title(volume_title: str) -> tuple[str, int | None]:
    """
    巻タイトルから (シリーズ名, 巻数) を返す。
    巻数が抽出できない場合は (元タイトル, None)。
    """
    if not volume_title:
        return volume_title, None
    title = volume_title.strip()
    for pat in _VOL_PATTERNS:
        m = pat.match(title)
        if not m:
            continue
        base = m.group(1).strip()
        num = _to_int(m.group(2))
        if base and num is not None:
            return base, num
    return title, None
