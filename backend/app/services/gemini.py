import json
import re
import time
import threading
import httpx
from google import genai
from google.genai import types
from app.config import settings


# =====================================================================
# Gemini API 使用量アキュムレータ (コスト計測用)
#   全 _single_generate 呼び出しの usage_metadata をモデル別に集計する。
#   reset_usage() でリセット、get_usage() でスナップショット取得。
#   スレッドセーフ (ThreadPoolExecutor 並列下でも正しく加算)。
# =====================================================================
_USAGE_LOCK = threading.Lock()
_USAGE_ACCUM: dict = {"calls": 0, "by_model": {}}


def reset_usage() -> None:
    with _USAGE_LOCK:
        _USAGE_ACCUM["calls"] = 0
        _USAGE_ACCUM["by_model"] = {}


def get_usage() -> dict:
    """使用量スナップショット {calls, by_model:{model:{calls,input,output,thinking}}} を返す。"""
    with _USAGE_LOCK:
        return {
            "calls": _USAGE_ACCUM["calls"],
            "by_model": {m: dict(v) for m, v in _USAGE_ACCUM["by_model"].items()},
        }


def _record_usage(model: str, response) -> None:
    um = getattr(response, "usage_metadata", None)
    if um is None:
        return
    inp = getattr(um, "prompt_token_count", 0) or 0
    out = getattr(um, "candidates_token_count", 0) or 0
    think = getattr(um, "thoughts_token_count", 0) or 0
    with _USAGE_LOCK:
        _USAGE_ACCUM["calls"] += 1
        d = _USAGE_ACCUM["by_model"].setdefault(
            model, {"calls": 0, "input": 0, "output": 0, "thinking": 0})
        d["calls"] += 1
        d["input"] += inp
        d["output"] += out
        d["thinking"] += think

client = genai.Client(api_key=settings.gemini_api_key)


# PROHIBITED_CONTENT に当たった時のフォールバック順。
# 3.x 系は 2.5 系より prohibited フィルタが格段に厳しく、grounding がスニペットを引いてくると
# 一見無害な作品まで弾かれる。そのため最終段は 2.5-pro + grounding OFF にして救済率を上げる。
PROHIBITED_FALLBACK_CHAIN = [
    ("gemini-3.1-pro-preview", True),   # 1段目: 3.x の上位モデル
    ("gemini-2.5-pro", False),           # 2段目: 2.5 + grounding OFF (検索由来の prohibited 回避)
]


# =====================================================================
# 重要度 (importance) 機能のオンオフ。
# False の場合:
#   - プロンプトに [重要度★★★] / [重要度★★] のマーカーを付けない
#   - 重要度に関連する「厳密に判定せよ」等の追加指示も省く
#   - tags.importance カラムや set_tag_importance.py スクリプトは温存
# True に戻せば、importance 列のデータをそのまま使って機能が復活する。
# =====================================================================
IMPORTANCE_ENABLED = False

_IMPORTANCE_MAIN_RULES = (
    "- **[重要度★★★] のタグは特に検索品質に影響するため、該当判定を厳密に行うこと。**\n"
    "  確信が持てない場合は付与しない。誤付与は厳禁。\n"
    "- [重要度★★] のタグも通常より一段慎重に判定すること。"
)
_IMPORTANCE_PHASE1_RULE = (
    "- [重要度★★★] のタグは慎重に — 該当の確証がない場合は含めないこと"
)
_IMPORTANCE_PHASE2_RULES = (
    "- [重要度★★★] のタグは特に厳密に。確信が持てない場合は必ず除外\n"
    "- [重要度★★] のタグも一段慎重に"
)


def _imp_main_rules() -> str:
    return _IMPORTANCE_MAIN_RULES if IMPORTANCE_ENABLED else ""

def _imp_phase1_rule() -> str:
    return _IMPORTANCE_PHASE1_RULE if IMPORTANCE_ENABLED else ""

def _imp_phase2_rules() -> str:
    return _IMPORTANCE_PHASE2_RULES if IMPORTANCE_ENABLED else ""


def _single_generate(
    prompt: str,
    model: str,
    use_grounding: bool,
    max_retries: int,
) -> tuple[str, str | None]:
    """1モデルでの送信処理。戻り値: (status, text)
    status: "ok" | "prohibited" | "empty" | "error"
    """
    safety_settings = [
        types.SafetySetting(category=cat, threshold="BLOCK_NONE")
        for cat in (
            "HARM_CATEGORY_HARASSMENT",
            "HARM_CATEGORY_HATE_SPEECH",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT",
            "HARM_CATEGORY_DANGEROUS_CONTENT",
        )
    ]
    config_kwargs = {"safety_settings": safety_settings}
    if use_grounding:
        config_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]

    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(**config_kwargs),
            )
            _record_usage(model, response)
            text = (response.text or "").strip()
            if text:
                return "ok", text
            # 空 → 原因を診断
            block_reason = None
            try:
                pf = getattr(response, "prompt_feedback", None)
                if pf is not None:
                    block_reason = str(getattr(pf, "block_reason", "") or "")
                fr = getattr(response.candidates[0], "finish_reason", None) if response.candidates else None
                print(f"  [diag] model={model} finish_reason={fr} prompt_feedback={pf}")
            except Exception as _e:
                print(f"  [diag] response inspect failed: {_e}")
            if block_reason and "PROHIBITED" in block_reason:
                return "prohibited", None
            return "empty", None
        except Exception as e:
            err = str(e)
            if "429" in err or "quota" in err.lower():
                wait = 60
                m = re.search(r'retry in (\d+\.?\d*)s', err)
                if m:
                    wait = int(float(m.group(1))) + 5
                print(f"[Gemini] レート制限 → {wait}秒待機 (試行 {attempt + 1}/{max_retries})")
                print(f"  err: {err[:300]}")
                time.sleep(wait)
            else:
                print(f"[Gemini] エラー: {e}")
                return "error", None
    print("[Gemini] リトライ上限到達")
    return "error", None


def _generate_with_retry(
    prompt: str,
    model: str,
    use_grounding: bool = False,
    max_retries: int = 5,
) -> str | None:
    """Geminiにリクエスト送信。レート制限時は自動リトライ。
    PROHIBITED_CONTENT で弾かれた場合は PROHIBITED_FALLBACK_CHAIN を順に試す。
    """
    status, text = _single_generate(prompt, model, use_grounding, max_retries)
    if status == "ok":
        return text
    if status != "prohibited":
        return text  # 空文字 or None

    tried = {model}
    for fb_model, fb_grounding in PROHIBITED_FALLBACK_CHAIN:
        if fb_model in tried:
            continue
        tried.add(fb_model)
        grounding_label = "grounding=ON" if fb_grounding else "grounding=OFF"
        print(f"  [fallback] PROHIBITED_CONTENT → {fb_model} ({grounding_label}) でリトライ")
        status, text = _single_generate(prompt, fb_model, fb_grounding, max_retries)
        if status == "ok":
            return text
        if status != "prohibited":
            return text
    print(f"  [fallback] 全モデルで PROHIBITED_CONTENT → スキップ")
    return None


def extract_book_from_tweet(tweet_text: str) -> dict:
    """
    Gemini 2.5 Proでツイートから書籍発売情報を判定・抽出する。
    """
    prompt = f"""
以下はライトノベルレーベル「講談社ラノベ文庫」の公式Xアカウントのツイートです。

【判定基準】
書籍の発売・予約・重版・書影公開など、書籍の販売に関するツイートかどうか判定してください。
対象外: イベント・キャンペーン・アニメ化・作者へのメッセージのみ のツイート

ツイート:
{tweet_text}

出力形式（JSONのみ）:
{{
  "is_book_related": true または false,
  "title": "書籍タイトル（不明ならnull）",
  "author": "著者名（不明ならnull）",
  "published_date": "YYYY-MM-DD形式（不明ならnull）"
}}
"""
    text = _generate_with_retry(prompt, model=settings.gemini_analysis_model)
    if text:
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return {"is_book_related": False}


def _format_tag_dict(tag_dict: dict[str, list[dict]] | None,
                     section_title: str = "優先タグリスト") -> str:
    """
    タグ辞書をプロンプト用の優先タグリストに整形する。
    tag_dict 例:
      {
        "atmosphere": [{"name": "胸キュン", "description": "...", "importance": 3}, ...],
        "genre":      [...],
        "sub_genre":  [...],
      }
    importance は 1-3 (NULL は 1 扱い)。2 以上なら ★ マーカーを付け、
    Gemini に「該当判定を厳密に行う」旨を示す。
    """
    if not tag_dict:
        return ""
    label_map = {
        "atmosphere": "atmosphere_tags（雰囲気）",
        "genre":      "genre_tags（ジャンル）",
        "sub_genre":  "sub_genre_tags（サブジャンル・属性）",
    }
    lines: list[str] = []
    has_any = False
    for key, label in label_map.items():
        items = tag_dict.get(key) or []
        if not items:
            continue
        has_any = True
        lines.append(f"\n■ {label}：")
        # 重要度が ON のときのみ降順ソート (OFF は元順序を尊重)
        if IMPORTANCE_ENABLED:
            items_sorted = sorted(items, key=lambda it: -(it.get("importance") or 1))
        else:
            items_sorted = items
        for it in items_sorted:
            name = (it.get("name") or "").strip()
            if not name:
                continue
            desc = (it.get("description") or "").strip()
            if IMPORTANCE_ENABLED:
                importance = it.get("importance") or 1
                if importance >= 3:
                    marker = " [重要度★★★]"
                elif importance >= 2:
                    marker = " [重要度★★]"
                else:
                    marker = ""
            else:
                marker = ""
            head = f"  - {name}{marker}"
            lines.append(f"{head}: {desc}" if desc else head)
    return "\n".join(lines) if has_any else ""


def _format_books_for_prompt(books: list[dict] | None,
                              max_books: int = 6,
                              max_desc_chars: int = 350) -> str:
    """シリーズ内の書籍リストをプロンプト用に整形。
    巻数の小さい順で max_books 冊を抜粋。各 description は max_desc_chars で切り詰め。
    """
    if not books:
        return ""
    sorted_books = sorted(books, key=lambda b: (
        b.get("volume_number") if b.get("volume_number") is not None else 9999,
        b.get("published_date") or "9999",
    ))
    lines: list[str] = []
    for b in sorted_books[:max_books]:
        vol = b.get("volume_number")
        vol_str = f"vol{vol}" if vol is not None else "  ?"
        title = (b.get("title") or "").strip()
        desc = (b.get("description") or "").strip().replace("\n", " ")
        if len(desc) > max_desc_chars:
            desc = desc[:max_desc_chars] + "…"
        lines.append(f"  [{vol_str}] {title}")
        if desc:
            lines.append(f"        {desc}")
    if len(sorted_books) > max_books:
        lines.append(f"  ... (他 {len(sorted_books) - max_books} 冊)")
    return "\n".join(lines)


def _format_score_anchors(anchors: list[dict] | None,
                          include_atmosphere: bool = False) -> str:
    """既評価シリーズを Gemini プロンプト用に整形 (calibration の参考)"""
    if not anchors:
        return ""
    lines = [
        "【参考: 既に評価済みシリーズ — score の calibration に使用】",
        "他作品の判定との一貫性を保つため、これらを基準に新シリーズを評価してください。",
    ]
    for a in anchors:
        title = (a.get("series_title") or "")[:40]
        author = a.get("author") or "?"
        vc = a.get("volume_count")
        pop = a.get("popularity_score")
        mom = a.get("momentum_score")
        atmo_str = ""
        if include_atmosphere:
            atmo_bits = []
            for k, label in [
                ("comedy_score", "comedy"),
                ("serious_score", "serious"),
                ("heartwarming_score", "heart"),
                ("bittersweet_score", "bitter"),
                ("romantic_score", "rom"),
                ("uplifting_score", "uplift"),
            ]:
                v = a.get(k)
                if v is not None:
                    atmo_bits.append(f"{label}={v}")
            if atmo_bits:
                atmo_str = "  " + " ".join(atmo_bits)
        lines.append(
            f"  - 「{title}」 著={author} ({vc}冊)  "
            f"popularity={pop} momentum={mom}{atmo_str}"
        )
    return "\n".join(lines)


def enrich_openbd_series(
    series_title: str,
    author: str | None = None,
    imprint: str | None = None,
    volume_count: int | None = None,
    books: list[dict] | None = None,
    tag_dict: dict[str, list[dict]] | None = None,
    ai_tag_dict: dict[str, list[dict]] | None = None,
    model: str | None = None,
    use_grounding: bool = False,
    score_anchors: list[dict] | None = None,
) -> dict:
    """
    openbd_test_series 用のシリーズエンリッチ。
    synopsis + tags + 7軸スコア を Gemini で 1回の API call で生成する。

    引数:
      series_title : シリーズ名 (例: "ソードアート・オンライン")
      author       : 著者
      imprint      : レーベル (例: "電撃文庫")。プロンプトの文脈に注入される
      volume_count : 巻数
      books        : シリーズ内書籍 [{volume_number, title, description, published_date}]
                     Gemini が誤認しないよう実書籍を渡す
      tag_dict     : 優先タグ辞書

    戻り値の例:
      {
        "synopsis": "...",
        "atmosphere_tags": [...],
        "genre_tags": [...],
        "sub_genre_tags": [...],
        "target_audience": "...",
        "comedy_score": 70,
        "serious_score": 30,
        ...,
        "popularity_score": 85,
        "rationale": "..."
      }
    """
    author_info = f"著者: {author}" if author else ""
    imprint_info = f"レーベル: {imprint}" if imprint else ""
    volume_info = f"既刊巻数: {volume_count}" if volume_count else ""
    book_section = _format_books_for_prompt(books)
    book_block = f"""
【シリーズ内の書籍 (代表巻のタイトル＋あらすじ抜粋)】
以下は実際のシリーズ内容を確認するためのサンプルです。Web 検索結果と
組み合わせて作品を正確に把握してください。
{book_section}
""" if book_section else ""

    master_tag_section = _format_tag_dict(tag_dict)
    ai_tag_section = _format_tag_dict(ai_tag_dict)
    if master_tag_section or ai_tag_section:
        master_block = f"""
【優先タグリスト (公式辞書) — 以下から最優先で選ぶこと】
- 下のリストにあるタグを**1つずつ確認し、当てはまるものを漏れなく全て選ぶ**こと
- 当てはまる表記をそのまま使うこと（同じ意味でも別表記にしない／表記揺れを作らない）
- 各タグの説明文と作品内容を照合し、**確実に該当**するもののみ選ぶ
  （ストーリーや設定に明確に存在する要素だけ。推測・連想で付けない）
{_imp_main_rules()}
{master_tag_section}
""" if master_tag_section else ""
        ai_block = f"""
【既出タグ (過去に AI が追加したもの・第2優先) — 公式辞書に該当が無い時に検討】
- 公式辞書で当てはまるタグが無い場合のみ、以下のリストから検討する
- 表記は完全一致で使うこと (新規に似た表記を作らない)
- 該当しなければ無視してよい
{ai_tag_section}
""" if ai_tag_section else ""
        new_tag_rule = """
【新規タグ作成ルール — 原則禁止 / 厳格条件】
- **新規タグ作成は原則禁止**。まず公式辞書 + 既出 AI タグから当てはまるものを徹底的に探すこと
- 以下の **全ての条件を満たした場合のみ**、1作品あたり**最大 1 個まで**新規追加してよい:
  1. 作品を**象徴する核心的な特徴**であり、既存のどのタグでも代替できない
  2. 同レーベル・同ジャンルの**他作品でも 5 件以上で使い回せる**確信がある (固有名詞・本作専用は不可)
  3. 既存タグの言い換え・微妙な表記揺れ・既存タグの細分化ではない
  4. 既存タグの組み合わせでカバーできない独立した概念である
- 上記いずれかに不確かさがあれば**作らない**。1個でも怪しければ 0 個でよい (精度優先)
- 新規タグ名は「具体的・短く・他作品でも使い回せる」名前にする
"""
        tag_instruction = master_block + ai_block + new_tag_rule
    else:
        tag_instruction = ""

    # anchor (既評価シリーズの参考データ) — score の一貫性向上に使う
    anchor_block = _format_score_anchors(score_anchors, include_atmosphere=True)
    anchor_section = f"\n{anchor_block}\n" if anchor_block else ""

    prompt = f"""
以下のライトノベル**シリーズ**について、提供される書籍情報と Web 検索を元に、
- 検索用タグ
- 雰囲気スコア (0〜100)
を生成してください。

シリーズ名: 「{series_title}」
{imprint_info}
{author_info}
{volume_info}
{book_block}{anchor_section}
【タグ作成方針 — 重要】
- atmosphere_tags / genre_tags / sub_genre_tags は**シリーズ全体**を表すものを選ぶ
- 巻ごとの内容に依存せず、作品全体に通底する雰囲気・属性を選ぶ
- atmosphere_tags: 雰囲気（胸キュン／ダーク／ほのぼの 等）
- genre_tags: ジャンル（ファンタジー／ラブコメ／ミステリー 等）
- sub_genre_tags: サブジャンル・属性タグ（ざまぁ／共依存／身長差／百合／異世界転生／悪役令嬢 等）

【タグ付与の量と精度 — 厳守】
- **検索性を最大化するため、当てはまるタグは漏れなく全て付ける**こと
- 1〜2個で済ませず、作品が持つ要素を多角的にカバーする
- ただし**確実に該当する**もののみ。推測・連想・「なんとなく合いそう」で付けない
- 不明・判断つかない場合は付けない（精度優先 > 量）
{tag_instruction}
【スコア評価 — 0〜100 の整数 or null、精度重視】
基準値 (参考):
- 0   = 全く該当しない
- 30  = やや該当
- 50  = 中程度に該当
- 70  = 強く該当
- 100 = 突出して該当 (その軸の代表作レベル)

【スコアの粒度 — 厳守】
- **1 刻みで細かく**評価すること
- 0, 5, 10, 20, 30, 50, 70, 90, 100 のような**丸い数字に偏らない**
- 例: 47, 63, 78, 92, 24 のような中間値を積極的に使う
- 「やや該当」も書籍によって 28 / 33 / 38 と差を付ける
- 全シリーズでスコア分布が **0-100 に広く散らばる**よう、隣接シリーズを比べて微差を表現
- 判断材料不足・確信が持てない軸は **null** を返す (0 と null を明確に区別)

【雰囲気スコアの定義】
- comedy_score        : 笑えるシーン・軽快な掛け合い・コメディ要素の濃さ
- serious_score       : 重いテーマ・葛藤・社会性・真剣さの濃さ
- heartwarming_score  : 穏やかで安心感のある描写・ほのぼの度
- bittersweet_score   : 報われない想い・別れ・喪失感・切なさ
- romantic_score      : 胸キュン・甘い恋愛描写・恋愛のときめき度
- uplifting_score     : 高揚感 (感動 / 爽快 / 熱血 / カタルシス / 読後の昂り)

【人気度スコアの定義 — 2軸で別々に評価】

▼ popularity_score: **累計知名度** (作品の歴史的な認知度)
  考慮要素: アニメ化／コミカライズ／映像化の有無、長期連載 (巻数の多さ)、
            累計発行部数、受賞歴、シリーズの一般的認知度
  時間軸:    シリーズ発刊全期間を通じての総合的な実績
  基準値 (参考、実際の値は 1 刻みで細かく振る):
  -  95 前後: 超メジャー (SAO、リゼロ、ハルヒ等) ─ 個別作品でも 92, 96, 99 等で差
  -  75 前後: 人気作・アニメ化済み ─ 同レベルでも 71, 76, 82 と差
  -  55 前後: 中堅・固定ファン
  -  35 前後: マイナー
  -  15 前後: ほぼ無名
  -   0     : 情報なし
  特徴: 古典名作は高くなる、最近発売の新作は累計実績がまだ無いので低く出やすい

▼ momentum_score: **直近の話題度** (今この作品が話題か)
  考慮要素: 直近1年程度の SNS / X (旧 Twitter) での言及量、最新巻の売れ行き、
            現在のファン活動度、現在連載中か完結済みか、最新刊・関連メディアの動向
  時間軸:    過去1年〜現在の話題性
  基準値 (参考、実際の値は 1 刻みで細かく振る):
  -  95 前後: 現在大バズり中・話題沸騰
  -  75 前後: 直近で話題・SNS で頻繁に言及
  -  55 前後: 一定の現役感あり
  -  35 前後: 細々とファン支持
  -  15 前後: 直近の話題はほぼない
  -   0     : 現在事実上忘れられている
  特徴: 最近発売の話題作は高い、古典作品でも現役活動中なら高い、
        終わって時間が経った作品は低い

【厳守 — 2軸を必ず別々に評価すること】
- 古典名作で現在も人気: popularity 高 (90+) かつ momentum も高 (70+) になる
- 古典名作で現在は下火: popularity 高 (90+) だが momentum 低 (10-30)
- **最近発売の話題作: popularity が低くても (30-50) momentum を高く (80-100) 付ける**
- 最近発売だが話題なし: popularity も momentum も低い
- 「最近の本だから popularity 低い」だけで momentum も低くしない

【厳守】
- 該当しない軸は 0 にせず **null** にする
- 推測で適当な値を入れない (精度優先)
- スコアは整数 (小数点なし)
- **5/10 刻みに丸めず**、1 刻みで作品ごとの微差を表現する
  (例: 70 ではなく 68 や 73 のような値を積極的に使う)

以下のJSON形式で返してください:

{{
  "atmosphere_tags": ["..."],
  "genre_tags": ["..."],
  "sub_genre_tags": ["..."],
  "comedy_score": 0-100 or null,
  "serious_score": 0-100 or null,
  "heartwarming_score": 0-100 or null,
  "bittersweet_score": 0-100 or null,
  "romantic_score": 0-100 or null,
  "uplifting_score": 0-100 or null,
  "popularity_score": 0-100 or null,
  "momentum_score": 0-100 or null,
  "rationale": "1-2文で評価の判断根拠 (短く)"
}}

JSONのみ返すこと。
"""
    text = _generate_with_retry(
        prompt,
        model=model or settings.gemini_parse_model,  # デフォルトは Flash
        use_grounding=use_grounding,
    )
    if text:
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return {}


# =====================================================================
# Multipass enrich: サブカテゴリ毎に Phase1 (候補広め) + Phase2 (検証) で精度を上げる
# =====================================================================

SUB_GENRE_SUBCATEGORY_ORDER = [
    "relationship", "character", "profession", "setting",
    "plot", "format",
]
SUB_GENRE_SUBCATEGORY_LABELS = {
    "relationship": "関係性 — 主要人物同士の関係 (バディ・三角関係・身分差恋愛・師弟・幼なじみ 等)",
    "character":    "キャラ — 主要人物の性格や特徴 (吸血鬼・お嬢様・最強主人公・陰キャ・天才 等)",
    "profession":   "職業 — 主要人物の職業 (騎士・冒険者・教師・医者・剣士・忍者 等)",
    "setting":      "世界観 — その物語がどのような世界か (異世界・後宮・近未来・ダンジョン・現代ファンタジー 等)",
    "plot":         "ストーリー — ストーリーの重要な要素 (復讐・成り上がり・記憶喪失・群像劇 等)",
    "format":       "形式 — 作品の形式 (短編集・ノベライズ・TRPGリプレイ・単巻完結 等)",
}

# 全 sub_genre タグに共通する適用ルール。Phase1/Phase2 のプロンプトに注入する。
SUB_GENRE_GLOBAL_POLICY = (
    "そのラノベの**特徴として挙げられる場合のみ**採用すること。"
    "端役・一時的な要素・微細な要素は対象外。"
    "主要人物の関係・職業・性格、物語の核となる世界観・要素、作品形式に限る。"
)


def _format_tag_list_compact(items: list[dict] | None) -> str:
    """Phase1/2 用のコンパクトなタグ一覧。
    IMPORTANCE_ENABLED=True のときだけ重要度マーカーを付け、降順で並べる。"""
    if not items:
        return ""
    if IMPORTANCE_ENABLED:
        items_sorted = sorted(items, key=lambda it: -(it.get("importance") or 1))
    else:
        items_sorted = items
    lines = []
    for it in items_sorted:
        name = (it.get("name") or "").strip()
        if not name:
            continue
        desc = (it.get("description") or "").strip()
        if IMPORTANCE_ENABLED:
            importance = it.get("importance") or 1
            if importance >= 3:
                marker = " [重要度★★★]"
            elif importance >= 2:
                marker = " [重要度★★]"
            else:
                marker = ""
        else:
            marker = ""
        head = f"  - {name}{marker}"
        lines.append(f"{head}: {desc}" if desc else head)
    return "\n".join(lines)


def _build_series_meta_block(series_title: str,
                              author: str | None,
                              imprint: str | None,
                              volume_count: int | None,
                              book_section: str) -> str:
    """全 Phase 共通の「シリーズ情報」ブロック。"""
    return f"""シリーズ名: 「{series_title}」
{f'レーベル: {imprint}' if imprint else ''}
{f'著者: {author}' if author else ''}
{f'既刊巻数: {volume_count}' if volume_count else ''}

【シリーズ内書籍 (代表巻のタイトル + あらすじ抜粋)】
{book_section}""".strip()


def _parse_json_object(text: str | None) -> dict:
    if not text:
        return {}
    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        return {}
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return {}


def _phase1_wide_net(category_label: str,
                     candidates: list[dict],
                     series_meta: str,
                     model: str,
                     use_grounding: bool,
                     extra_policy: str = "") -> list[str]:
    """Phase 1: recall 重視で「該当しそうな」タグを広めに拾う。
    extra_policy はカテゴリ固有のルール (例: sub_genre 共通ルール) を注入する。
    """
    if not candidates:
        return []
    tag_list = _format_tag_list_compact(candidates)
    extra_block = f"\n【このカテゴリの適用ルール】\n{extra_policy}\n" if extra_policy else ""
    prompt = f"""
以下のライトノベル**シリーズ**について、「{category_label}」カテゴリの候補タグを「広めに」拾ってください。

{series_meta}

【候補タグリスト ({category_label})】
{tag_list}
{extra_block}
【方針 — recall (拾い漏れ無し) を優先】
- 確実ではなくても「該当しそうな気配がある」レベルは含めること
- 主要要素だけでなく、副次的な要素も漏らさず候補に入れる
- 後段で別途厳密判定するので、ここでは絞らない (precision はその時)
- ただしリストにない名前は提案しない (新規タグは作らない)
{_imp_phase1_rule()}

【出力 (JSON のみ、コードフェンス禁止)】
{{
  "candidates": ["タグ名1", "タグ名2", ...]
}}
"""
    text = _generate_with_retry(prompt, model=model, use_grounding=use_grounding)
    data = _parse_json_object(text)
    raw = data.get("candidates") or []
    valid_names = {(it.get("name") or "").strip() for it in candidates}
    return [n for n in raw if isinstance(n, str) and n in valid_names]


def _phase2_verify(category_label: str,
                   candidate_names: list[str],
                   all_candidates: list[dict],
                   series_meta: str,
                   model: str,
                   use_grounding: bool,
                   extra_policy: str = "") -> list[str]:
    """Phase 2: Phase1 の候補を厳密に判定し、根拠が確かなものだけ残す。
    extra_policy はカテゴリ固有のルール (例: sub_genre 共通ルール) を注入する。
    """
    if not candidate_names:
        return []
    # 候補について description + importance を表示
    by_name = {(it.get("name") or "").strip(): it for it in all_candidates}
    items_for_prompt = [by_name[n] for n in candidate_names if n in by_name]
    if not items_for_prompt:
        return []
    tag_list = _format_tag_list_compact(items_for_prompt)
    extra_block = f"\n【このカテゴリの適用ルール】\n{extra_policy}\n" if extra_policy else ""
    prompt = f"""
以下のライトノベル**シリーズ**について、Phase1 で抽出された「{category_label}」候補タグを**厳密に判定**してください。

{series_meta}

【判定対象 (Phase1 抽出結果)】
{tag_list}
{extra_block}
【方針 — precision (確実性) 優先】
- 各タグについて、書籍情報に「明確な根拠」があるかを確認する
- 根拠が薄い・推測・連想・「なんとなく合う」レベルなら除外する
{_imp_phase2_rules()}
- 量より質。確信できるものだけ残す
- 候補リストに無いタグは加えないこと

【出力 (JSON のみ、コードフェンス禁止)】
{{
  "tags": ["確定タグ1", "確定タグ2", ...]
}}
"""
    text = _generate_with_retry(prompt, model=model, use_grounding=use_grounding)
    data = _parse_json_object(text)
    raw = data.get("tags") or []
    valid = set(candidate_names)
    return [n for n in raw if isinstance(n, str) and n in valid]


def _phase2_verify_atmo_genre_with_scores(
    atmosphere_candidates: list[str],
    genre_candidates: list[str],
    all_atmosphere: list[dict],
    all_genre: list[dict],
    series_meta: str,
    score_anchors: list[dict] | None,
    model: str,
    use_grounding: bool,
) -> dict:
    """atmosphere + genre の Phase2 に 7軸スコア (popularity/momentum 除く) も乗せる。"""
    atmo_by = {(it.get("name") or "").strip(): it for it in all_atmosphere}
    genre_by = {(it.get("name") or "").strip(): it for it in all_genre}
    atmo_items = [atmo_by[n] for n in atmosphere_candidates if n in atmo_by]
    genre_items = [genre_by[n] for n in genre_candidates if n in genre_by]
    atmo_list = _format_tag_list_compact(atmo_items)
    genre_list = _format_tag_list_compact(genre_items)
    anchor_block = _format_score_anchors(score_anchors, include_atmosphere=True)
    anchor_section = f"\n{anchor_block}\n" if anchor_block else ""

    prompt = f"""
以下のライトノベル**シリーズ**について、Phase1 で抽出された atmosphere / genre 候補を厳密に判定し、
同時に 6 軸の雰囲気スコア (0-100 or null) を生成してください。

{series_meta}
{anchor_section}
【判定対象 — atmosphere】
{atmo_list or '(なし)'}

【判定対象 — genre】
{genre_list or '(なし)'}

【タグ判定方針 — precision 優先】
- 各タグについて書籍情報に明確な根拠があるかを確認
- 根拠が薄い・連想・「なんとなく合う」レベルなら除外
{_imp_phase2_rules()}

【雰囲気スコア — 0-100 の整数 or null】
- comedy_score        : 笑える・軽快・コメディ要素の濃さ
- serious_score       : 重いテーマ・葛藤・真剣さの濃さ
- heartwarming_score  : 穏やかで安心感のある描写・ほのぼの度
- bittersweet_score   : 報われない想い・別れ・切なさ
- romantic_score      : 胸キュン・甘い恋愛描写
- uplifting_score     : 高揚感 (感動 / 爽快 / 熱血 / カタルシス)

【スコア粒度 — 厳守】
- **1 刻みで細かく**評価 (47, 63, 78 など)
- 0/5/10/20/50/70/90/100 のような丸い数字に偏らない
- 判断材料不足・確信が持てない軸は **null** (0 と null を区別)

【出力 (JSON のみ、コードフェンス禁止)】
{{
  "atmosphere_tags": ["..."],
  "genre_tags": ["..."],
  "comedy_score": 0-100 or null,
  "serious_score": 0-100 or null,
  "heartwarming_score": 0-100 or null,
  "bittersweet_score": 0-100 or null,
  "romantic_score": 0-100 or null,
  "uplifting_score": 0-100 or null
}}
"""
    text = _generate_with_retry(prompt, model=model, use_grounding=use_grounding)
    data = _parse_json_object(text)
    # 出力に含まれるタグだけ valid に絞る
    valid_atmo = set(atmosphere_candidates)
    valid_genre = set(genre_candidates)
    out: dict = {
        "atmosphere_tags": [n for n in (data.get("atmosphere_tags") or [])
                             if isinstance(n, str) and n in valid_atmo],
        "genre_tags":     [n for n in (data.get("genre_tags") or [])
                             if isinstance(n, str) and n in valid_genre],
    }
    for k in ("comedy_score", "serious_score", "heartwarming_score",
              "bittersweet_score", "romantic_score", "uplifting_score"):
        out[k] = data.get(k)
    return out


def enrich_series_multipass(
    series_title: str,
    author: str | None = None,
    imprint: str | None = None,
    volume_count: int | None = None,
    books: list[dict] | None = None,
    atmosphere_candidates: list[dict] | None = None,
    genre_candidates: list[dict] | None = None,
    sub_genre_by_subcategory: dict[str, list[dict]] | None = None,
    score_anchors: list[dict] | None = None,
    model: str | None = None,
    use_grounding: bool = False,
    atmo_genre_model: str | None = None,
    atmo_genre_grounding: bool | None = None,
    sub_genre_model: str | None = None,
    sub_genre_grounding: bool | None = None,
) -> dict:
    """multipass エンリッチ。
    atmosphere+genre 1 ペア + sub_genre 各サブカテゴリ毎に Phase1+Phase2 で
    精度を上げる。popularity/momentum はここでは扱わない (別 Phase 推奨)。

    atmo+genre と sub_genre で別モデル・別 grounding を指定可能。
    atmo_genre_model / sub_genre_model が None なら model にフォールバック
    (それも None なら settings.gemini_parse_model)。

    戻り値は enrich_openbd_series と同じ shape:
      atmosphere_tags / genre_tags / sub_genre_tags /
      comedy_score..uplifting_score / (popularity_score / momentum_score は None)
    """
    book_section = _format_books_for_prompt(books)
    series_meta = _build_series_meta_block(
        series_title, author, imprint, volume_count, book_section
    )
    fallback_mdl = model or settings.gemini_parse_model
    atmo_mdl = atmo_genre_model or fallback_mdl
    sub_mdl = sub_genre_model or fallback_mdl
    atmo_grd = use_grounding if atmo_genre_grounding is None else atmo_genre_grounding
    sub_grd = use_grounding if sub_genre_grounding is None else sub_genre_grounding

    result: dict = {
        "atmosphere_tags": [],
        "genre_tags": [],
        "sub_genre_tags": [],
        "comedy_score": None, "serious_score": None,
        "heartwarming_score": None, "bittersweet_score": None,
        "romantic_score": None, "uplifting_score": None,
        "popularity_score": None, "momentum_score": None,
    }

    # ---- atmosphere + genre (1ペア × 2 phase, atmo_mdl + atmo_grd) ----
    atmo = atmosphere_candidates or []
    genr = genre_candidates or []
    if atmo or genr:
        # Phase 1: atmosphere / genre をそれぞれ wide-net
        p1_atmo = _phase1_wide_net(
            "atmosphere (雰囲気)", atmo, series_meta, atmo_mdl, atmo_grd,
        ) if atmo else []
        p1_genre = _phase1_wide_net(
            "genre (ジャンル)", genr, series_meta, atmo_mdl, atmo_grd,
        ) if genr else []
        # Phase 2: 候補をまとめて 1 call で検証 + スコア生成
        p2 = _phase2_verify_atmo_genre_with_scores(
            atmosphere_candidates=p1_atmo,
            genre_candidates=p1_genre,
            all_atmosphere=atmo,
            all_genre=genr,
            series_meta=series_meta,
            score_anchors=score_anchors,
            model=atmo_mdl,
            use_grounding=atmo_grd,
        )
        result["atmosphere_tags"] = p2.get("atmosphere_tags", [])
        result["genre_tags"] = p2.get("genre_tags", [])
        for k in ("comedy_score", "serious_score", "heartwarming_score",
                  "bittersweet_score", "romantic_score", "uplifting_score"):
            result[k] = p2.get(k)

    # ---- sub_genre: サブカテゴリ毎に 2 phase (sub_mdl + sub_grd) ----
    sub_genre_final: list[str] = []
    by_subcat = sub_genre_by_subcategory or {}
    for subcat_key in SUB_GENRE_SUBCATEGORY_ORDER:
        cands = by_subcat.get(subcat_key) or []
        if not cands:
            continue
        label = f"sub_genre / {SUB_GENRE_SUBCATEGORY_LABELS.get(subcat_key, subcat_key)}"
        p1 = _phase1_wide_net(label, cands, series_meta, sub_mdl, sub_grd,
                              extra_policy=SUB_GENRE_GLOBAL_POLICY)
        if not p1:
            continue
        p2 = _phase2_verify(label, p1, cands, series_meta, sub_mdl, sub_grd,
                            extra_policy=SUB_GENRE_GLOBAL_POLICY)
        sub_genre_final.extend(p2)
    # 重複除去 (異なるサブカテゴリで同名が出ることは無いはずだが念のため)
    seen = set()
    dedup = []
    for n in sub_genre_final:
        if n in seen:
            continue
        seen.add(n)
        dedup.append(n)
    result["sub_genre_tags"] = dedup
    return result


def parse_detect_response(text: str | None) -> dict:
    """detect_series_with_gemini の応答テキストを {groups, standalone} にパース。"""
    if not text:
        return {"groups": [], "standalone": []}
    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        return {"groups": [], "standalone": []}
    try:
        data = json.loads(m.group())
        if not isinstance(data, dict):
            return {"groups": [], "standalone": []}
        data.setdefault("groups", [])
        data.setdefault("standalone", [])
        return data
    except json.JSONDecodeError:
        return {"groups": [], "standalone": []}


def detect_series_with_gemini(books: list[dict],
                              shared_names: list[str] | None = None,
                              imprint_hint: str | None = None,
                              model: str | None = None,
                              use_grounding: bool = True,
                              build_prompt_only: bool = False):
    """
    関連する書籍 (=同じ人物が役職問わず関わっている書籍) を Gemini に渡し、
    シリーズ構造に分類させる。

    分類カテゴリ:
      - main: メインシリーズの巻
      - sub_series: 主人公または視点キャラが大きく異なるサブシリーズ
      - special_edition: 特装版・限定版 (本編書籍に attach する想定)
      - standalone: シリーズに属さない単独作品

    Args:
      books: [{isbn, title, author, illustrator, contributors,
               published_date, description, ...}]
      shared_names: クラスタ内で複数書籍に共通する人物名のリスト (プロンプト補助)
      imprint_hint: 主要レーベル

    Returns:
      {
        "groups": [
          {
            "main_series_title": "...",
            "books": [
              {"isbn": "X", "role": "main", "volume_number": 1, "reason": "..."},
              {"isbn": "Y", "role": "sub_series",
               "sub_series_title": "プログレッシブ", "volume_number": 1, "reason": "..."},
              {"isbn": "Z", "role": "special_edition",
               "attached_to_isbn": "X", "reason": "..."}
            ]
          },
          ...
        ],
        "standalone": [
          {"isbn": "S", "title": "...", "reason": "..."}
        ]
      }
    """
    if not books:
        return {"groups": [], "standalone": []}

    # 書籍リストをコンパクトに整形 (description は 250字程度に切り詰め)
    def _short_desc(s: str | None, n: int = 250) -> str:
        if not s:
            return ""
        s = s.strip().replace("\n", " ")
        return s[:n] + "…" if len(s) > n else s

    def _format_contribs(b: dict) -> str:
        """各書籍の contributors を 役職: 名前(複数) で表示"""
        parts = []
        au = (b.get("author") or "").strip()
        il = (b.get("illustrator") or "").strip()
        if au:
            parts.append(f"著: {au}")
        if il:
            parts.append(f"絵: {il}")
        # contributors から他役職
        seen_pairs = set()
        for c in (b.get("contributors") or []):
            if not isinstance(c, dict):
                continue
            label = (c.get("role_label") or "").strip()
            name = (c.get("name") or "").strip()
            if not name or not label:
                continue
            if label in ("著", "イラスト"):  # 既に au/il で表示済み
                continue
            key = (label, name)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            parts.append(f"{label}: {name}")
        return "  ".join(parts) if parts else "(contributors 不明)"

    lines = []
    for b in books:
        title = (b.get("title") or "").strip()
        isbn = b.get("isbn") or "?"
        pub = b.get("published_date") or "?"
        contribs = _format_contribs(b)
        desc = _short_desc(b.get("description"))
        lines.append(f"  - ISBN={isbn} | 発売={pub} | 「{title}」")
        lines.append(f"      {contribs}")
        if desc:
            lines.append(f"      あらすじ抜粋: {desc}")
    book_block = "\n".join(lines)
    imprint_info = f"\n主レーベル: {imprint_hint}" if imprint_hint else ""
    shared_info = ""
    if shared_names:
        names_disp = ", ".join(shared_names[:10]) + ("…" if len(shared_names) > 10 else "")
        shared_info = f"\n【複数書籍に共通する人物】 {names_disp}"

    prompt = f"""
以下の書籍リストは、著者・原作・原著のいずれかの役職で共通する人物がいる
書籍クラスタです (イラストレーターは含めていません)。
Web 検索と各書籍のあらすじを参考に、これらを**物語の繋がり**に基づいて
シリーズ構造に分類してください。
{imprint_info}{shared_info}

【書籍リスト】 ({len(books)}冊)
{book_block}

【分類ルール — 厳守】

1. **main (メインシリーズの巻)**
   - 物語の進行 (時系列・主人公・世界観・舞台) が前作から繋がっている書籍
   - vol 1, 2, 3, ... のように連続している巻

2. **sub_series (サブシリーズ)**
   - 主人公または視点キャラクターが**大きく異なる**書籍
   - 例: 「本編 + アナザーキャラ視点の番外編シリーズ」「本編 + 過去編シリーズ」
   - メイン本編とは別の連続性を持つが、世界観/設定は共有している
   - sub_series_title を別途付ける (例: 「プログレッシブ」「ガンゲイル・オンライン」)
   - sub_series 同士で巻数が独立 (sub_series 内で 1, 2, 3, ...)

3. **special_edition (特装版・限定版)**
   - 「特装版」「限定版」「ドラマCD付き」「設定資料集付き」「画集付き」
     「アクリルスタンド付き」「豪華版」「復刻版」「初回限定」等
   - 本編のある巻と同内容＋付録の書籍
   - attached_to_isbn に **対応する通常版巻の ISBN** を指定する
   - main の books 配列に含めず、attached_to_isbn のみで参照させる

4. **standalone (単独作品)**
   - 連続性のない独立した単発作品
   - シリーズ第1巻だけ刊行されてその後が無い場合も含む (1冊しかない単独本)
   - これらは "standalone" 配列に入れる

【判断のポイント】
- 巻番号 (1, 2, ...) があれば物語連続のヒント
- タイトルの共通プレフィックスは強い信号
- あらすじから主人公・舞台・時系列を読み取る
- スピンオフは原則 sub_series (ただし完全に独立した世界観なら別 group)
- 同タイトルの「上巻/下巻」「〈上〉〈下〉」は同じ vol の前後編 → 同じ volume_number に
  分冊として保存する想定だが、まずは main として連番割り当てしてよい

【出力 JSON 形式 — 厳守】
JSON のみ、前後に説明文不要。

{{
  "groups": [
    {{
      "main_series_title": "メインシリーズの代表名 (巻数表記なし)",
      "books": [
        {{
          "isbn": "9784...",
          "role": "main" | "sub_series" | "special_edition",
          "volume_number": 1,
          "sub_series_title": null または "サブシリーズ名",
          "attached_to_isbn": null または "本編の ISBN",
          "reason": "短い判定理由 (1行)"
        }}
      ]
    }}
  ],
  "standalone": [
    {{ "isbn": "9784...", "reason": "..." }}
  ]
}}

【厳守】
- 全ての ISBN が groups または standalone に必ず 1 回だけ登場すること
- 漏れ・重複はエラー
- 確信がない書籍は standalone に入れる (推測でグループ化しない)
- special_edition の attached_to_isbn は同 groups 内の main の ISBN を指す必要がある
"""
    if build_prompt_only:
        return prompt
    text = _generate_with_retry(
        prompt,
        model=model or settings.gemini_enrichment_model,
        use_grounding=use_grounding,
    )
    return parse_detect_response(text)


def enrich_series(series_title: str, author: str = None,
                  tag_dict: dict[str, list[dict]] | None = None) -> dict:
    """
    Gemini 3.1 Pro（Webグラウンディング）でシリーズ全体のあらすじ・タグを生成する。

    あらすじは「未読者向け」に書く：
      - 1巻のあらすじ、または シリーズ全体の物語の方向性
      - 重大なネタバレは含めない
      - その作品の雰囲気・テーマが未読者にも伝わる内容

    tag_dict を渡すと、その辞書のタグを「優先タグリスト」として
    プロンプトに含めて Gemini に最優先で適用させる。
    """
    author_info = f"著者: {author}" if author else ""
    tag_section = _format_tag_dict(tag_dict)
    tag_instruction = (
        f"""
【優先タグリスト — 以下の辞書から最優先で選ぶこと】
- 下のリストにあるタグを**1つずつ確認し、当てはまるものを漏れなく全て選ぶ**こと
- 当てはまる表記をそのまま使うこと（同じ意味でも別表記にしない／表記揺れを作らない）
- 各タグの説明文と作品内容を照合し、**確実に該当**するもののみ選ぶ
  （ストーリーや設定に明確に存在する要素だけ。推測・連想で付けない）
{_imp_main_rules()}
- **新規タグ作成は原則禁止**。リストにない名前は提案しないこと。
  ただし作品を象徴する核心的特徴で、他作品でも 5 件以上に使い回せる確信があり、
  かつ既存タグの言い換え・細分化ではない場合に限り、**最大 1 個まで**新規追加してよい。
  少しでも不確かなら作らない (精度優先)
{tag_section}
"""
        if tag_section
        else ""
    )

    prompt = f"""
以下のライトノベル**シリーズ**についてWeb検索で情報を収集し、未読者向けの紹介情報を作成してください。

シリーズ名: 「{series_title}」
レーベル: 講談社ラノベ文庫
{author_info}

【あらすじ作成方針 — 厳守】
- これから読む人（未読者）への紹介文として書くこと
- 内容は「1巻のあらすじ」または「シリーズ全体の物語の方向性・導入」のいずれかに統一
- **特定の巻の出来事（◯巻でこうなった など）は書かない**
- 重大なネタバレ（黒幕の正体、最終回の展開、主要キャラの死亡 等）は禁止
- 未読者でも世界観・雰囲気・主人公の立場・物語のフックがわかる内容にする
- 250〜350文字程度

【タグ作成方針 — 重要】
- atmosphere_tags / genre_tags / sub_genre_tags は**シリーズ全体**を表すものを選ぶ
- 巻ごとの内容に依存せず、作品全体に通底する雰囲気・属性を選ぶ
- atmosphere_tags: 雰囲気（胸キュン／ダーク／ほのぼの 等）
- genre_tags: ジャンル（ファンタジー／ラブコメ／ミステリー 等）
- sub_genre_tags: サブジャンル・属性タグ（ざまぁ／共依存／身長差／百合／異世界転生／悪役令嬢 等）

【タグ付与の量と精度 — 厳守】
- **検索性を最大化するため、当てはまるタグは漏れなく全て付ける**こと
- 1〜2個で済ませず、作品が持つ要素を多角的にカバーする
  （例: ラブコメ × 学園 × 幼なじみ × 義妹 × 三角関係 のように複数該当はむしろ普通）
- ただし**確実に該当する**もののみ。推測・連想・「なんとなく合いそう」で付けない
- 不明・判断つかない場合は付けない（精度優先 > 量）
{tag_instruction}
以下のJSON形式で返してください:

{{
  "synopsis": "シリーズ全体or1巻のあらすじ（未読者向け、ネタバレなし、250〜350文字）",
  "atmosphere_tags": ["（当てはまる雰囲気を全て、複数可）"],
  "genre_tags": ["（当てはまるジャンルを全て、複数可）"],
  "sub_genre_tags": ["（当てはまるサブジャンル・属性を全て、複数可）"],
  "target_audience": "対象読者（例: 10代向け、大人向けなど）",
  "author": "著者名（確認できた場合）"
}}

JSONのみ返すこと。
"""
    text = _generate_with_retry(
        prompt,
        model=settings.gemini_enrichment_model,  # 3.1 Pro
        use_grounding=True,
    )
    if text:
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return {}


def enrich_series_popularity(
    series_title: str,
    author: str | None = None,
    imprint: str | None = None,
    volume_count: int | None = None,
    books: list[dict] | None = None,
    model: str | None = None,
    use_grounding: bool = True,
    score_anchors: list[dict] | None = None,
) -> dict:
    """
    シリーズの popularity_score (累計知名度) と momentum_score (直近話題度) のみを
    Pro + Web grounding で精緻に評価する。

    通常の enrich_openbd_series (Flash, no grounding) で得た popularity/momentum を
    上書きする想定の 2段階エンリッチ Phase 2 専用関数。

    Args:
      series_title : シリーズ名
      author       : 著者
      imprint      : レーベル
      volume_count : 既刊巻数
      books        : シリーズ内代表書籍 (title + published_date 程度で十分)
      model        : デフォルトは settings.gemini_enrichment_model (Pro)
      use_grounding: デフォルト True (Web 検索を有効化)

    Returns:
      {
        "popularity_score": 0-100 or null,
        "momentum_score": 0-100 or null,
        "momentum_reason": "話題の根拠 or null",
        "rationale": "..."
      }
    """
    # 書籍リストはタイトルと発売日だけで十分 (検索キーワード用)
    sorted_books = sorted(
        books or [],
        key=lambda b: (b.get("volume_number")
                       if b.get("volume_number") is not None else 9999,
                       b.get("published_date") or "9999"),
    )
    book_lines = []
    for b in sorted_books[:6]:
        title = (b.get("title") or "").strip()
        pub = b.get("published_date") or "?"
        vol = b.get("volume_number")
        vol_str = f"vol{vol}" if vol is not None else ""
        book_lines.append(f"  [{vol_str}] {title}  ({pub})")
    if len(sorted_books) > 6:
        book_lines.append(f"  ... 他 {len(sorted_books) - 6}冊")
    book_block = "\n".join(book_lines) if book_lines else "(書籍情報なし)"

    author_info = f"著者: {author}" if author else ""
    imprint_info = f"レーベル: {imprint}" if imprint else ""
    volume_info = f"既刊巻数: {volume_count}" if volume_count else ""

    # anchor (calibration 用の既評価シリーズ)
    anchor_block = _format_score_anchors(score_anchors, include_atmosphere=False)
    anchor_section = f"\n{anchor_block}\n" if anchor_block else ""

    prompt = f"""
以下のライトノベルシリーズについて、Web 検索結果を参照しながら**人気度を 2 軸**
で評価してください。タグや他のスコアは不要、人気度の 2 軸のみを返します。

シリーズ名: 「{series_title}」
{author_info}
{imprint_info}
{volume_info}
書籍例:
{book_block}
{anchor_section}
【popularity_score: 累計知名度】
作品の歴史的・累積的な認知度を 0-100 で評価する。
考慮要素:
  - アニメ化／コミカライズ／映像化の有無
  - 長期連載 (巻数の多さ)
  - 累計発行部数 (公表されていれば)
  - 受賞歴 (電撃大賞、このラノ等)
  - シリーズの一般的認知度
時間軸: シリーズ発刊全期間
基準値 (参考、実際の値は 1 刻みで細かく振る):
  -  95 前後: 超メジャー (SAO、リゼロ、ハルヒ等) ─ 同レベルでも 92/96/99 差
  -  75 前後: 人気作・アニメ化済み ─ 71/76/82 と差
  -  55 前後: 中堅・固定ファンあり
  -  35 前後: マイナー
  -  15 前後: ほぼ無名
  -   0     : 情報なし

【momentum_score: 直近の話題度 — popularity に対する「相対的な話題増分」で評価】
過去1年程度の「今この作品が話題か」を 0-100 で評価する。
**最重要ルール: popularity (累計知名度) が高いほど、momentum を高く付ける条件を厳しくする。**

▽ popularity が高い作品 (おおむね 60 以上):
  - 「有名だから」「固定ファンがいるから」という理由だけでは momentum を上げない。
  - **わかりやすく具体的な直近の話題がある場合のみ**高くする。例:
    新規アニメ化/映像化の発表・放送、コミカライズの開始や新刊、
    電子/紙のセール・フェア実施中、新刊発売直後の大きな反響、
    重版・実売好調の公表 など。
  - **受賞 (新人賞/大賞/年間ランキング入賞/このラノ等) は、人気が高い作品でも
    momentum を上げる正当な理由として認める。** 直近の受賞は積極的に反映する。
  - 上記のような具体的な話題が検索で見つからなければ、有名でも momentum は
    低め (おおむね 30 以下) に抑える。

▽ popularity が低い作品 (おおむね 40 以下):
  - **小さな信号でも momentum を上げてよい**。例:
    X (旧 Twitter) で話題になった、売れ行きが良いと言われている、
    賞を受賞した、新刊が好評、口コミで伸びている など。
  - こうした兆候があれば popularity が低くても momentum を高め (70-90) に付ける。

考慮する直近の話題 (要 Web 検索):
  - アニメ化/映像化/コミカライズの新規発表・進行
  - 電子/紙のセール・フェア (期間があれば日付も特定する)
  - 受賞 (新人賞・年間ランキング等)
  - X 等 SNS でのバズ・言及の増加
  - 最新巻の売れ行き・重版
時間軸: 過去1年〜現在
基準値 (参考、1 刻みで微差を付ける):
  -  90 前後: 明確な大型話題 (アニメ化決定/大バズり) が今ある
  -  70 前後: 具体的な直近話題あり (セール中/受賞/コミカライズ新刊等)
  -  45 前後: 現役感はあるが決定的な話題は無い
  -  25 前後: 直近の目立つ話題はほぼ無い
  -   0     : 事実上忘れられている

【momentum_reason: 話題の根拠注釈】
momentum を高め (おおむね 60 以上) に付けた場合、その**具体的な根拠**を短く記述する。
例:「2026-07-31 まで電子書籍 50%OFF セール」「劇場アニメ化決定 (2026春)」
   「コミカライズ第3巻発売・好評」「○○大賞 2025 受賞」「X で新刊が話題」。
- 日付・回数・賞名など具体情報があれば必ず含める。
- momentum が低い / 目立つ話題が無い場合は **null**。
- Web 検索で裏が取れない憶測は書かない (確かな情報のみ)。

【厳守 — 粒度とゼロ尊重】
- 2軸を**別々に評価**する (popularity 高くても momentum 低い、その逆もあり得る)
- 最近発売の作品は popularity が低くても momentum を独立に評価
- 古典名作で現在話題でないものは popularity 高で momentum 低
- **5/10 刻みに丸めず**、1 刻みで作品ごとの微差を表現する
  (例: 70 ではなく 68 や 73、50 ではなく 47 や 53 のような値を使う)
- 該当しない・判断不能なら **null** を返す (推測しない)
- スコアは整数 (小数点なし)

出力 JSON のみ:
{{
  "popularity_score": 0-100 or null,
  "momentum_score": 0-100 or null,
  "momentum_reason": "話題の具体的根拠 (セール期限/映画化/受賞等)、無ければ null",
  "rationale": "1-2文で評価の判断根拠 (短く)"
}}

JSONのみ返すこと。
"""
    text = _generate_with_retry(
        prompt,
        model=model or settings.gemini_enrichment_model,  # Pro デフォルト
        use_grounding=use_grounding,
    )
    if not text:
        return {}
    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        return {}
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return {}


_EXCLUDE_TITLE_PATTERNS = [
    # 特典付き・限定版（通常版とほぼ同タイトルで重複保存される）
    r"特装版", r"限定版", r"初回限定", r"特典付", r"特典版",
    r"豪華版", r"特製版", r"完全版",
    r"ドラマ\s*CD", r"DL.*?シリアル", r"DL.*?コード", r"シリアル.*?コード",
    r"アクリル", r"ブロマイド", r"カレンダー\s*付",
    # 非ライトノベル
    r"画集", r"設定資料集", r"ファンブック", r"公式.*?ブック",
    r"アンソロジー", r"ガイドブック", r"パンフレット",
    r"コミック", r"漫画版",
]


def _is_excluded_title(title: str) -> bool:
    """特典付き・非ライトノベル等を除外する判定"""
    if not title:
        return True
    for p in _EXCLUDE_TITLE_PATTERNS:
        if re.search(p, title, re.IGNORECASE):
            return True
    return False


def _filter_books(books: list[dict]) -> tuple[list[dict], int]:
    """除外パターンに引っかかる書籍を捨てる。Returns (kept, dropped_count)"""
    kept = []
    dropped = 0
    for b in books or []:
        t = (b.get("title") or "").strip()
        if _is_excluded_title(t):
            dropped += 1
            continue
        kept.append(b)
    return kept, dropped


def _fetch_html(url: str) -> str | None:
    """公式サイトのHTMLを取得"""
    try:
        r = httpx.get(
            url,
            follow_redirects=True,
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
        )
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"[HTTP] 取得失敗: {url} → {e}")
        return None


def _strip_html(html: str, max_len: int = 200000) -> str:
    """script/style除去 + 余白圧縮（LLMに渡す前処理）"""
    html = re.sub(r"<script[\s\S]*?</script>", "", html, flags=re.IGNORECASE)
    html = re.sub(r"<style[\s\S]*?</style>", "", html, flags=re.IGNORECASE)
    html = re.sub(r"<!--[\s\S]*?-->", "", html)
    html = re.sub(r"\s+", " ", html)
    return html[:max_len]


def _extract_books_from_html(html: str, year: int, model: str | None = None) -> list[dict]:
    """HTMLから書籍リストを抽出（モデル指定可能）"""
    cleaned = _strip_html(html)
    prompt = f"""
以下は講談社ラノベ文庫公式サイトの{year}年の書籍一覧ページのHTMLです。
このHTMLに掲載されているライトノベルの**通常版のみ**を列挙してください。

【除外するもの — 厳守（出力 books 配列に含めない）】
- 特装版・限定版・初回限定版・特典付き版・豪華版・完全版（通常版と内容重複するため）
- ドラマCD付き・DLコード付き・シリアルコード付き等の特典セット
- アクリルスタンド／ブロマイド／カレンダー等のグッズ付き版
- 画集・設定資料集・ファンブック・公式ガイドブック・アンソロジー・パンフレット等の非小説書籍
- コミカライズ・漫画版

【含めるもの】
- 通常版のライトノベル（小説本体）のみ

【厳守】
- HTMLに含まれる**通常版ライトノベル**は省略せず全て出力（途中で打ち切らない）
- HTMLに書かれていない書籍を勝手に追加しないこと
- 巻数があれば正式名（例: 「○○ 3」）として記載
- 発売日はHTMLの記載通り（YYYY-MM-DD形式に正規化）

HTML:
{cleaned}

出力形式（JSONのみ。前後に説明文は不要）:
{{
  "books": [
    {{
      "title": "書籍タイトル",
      "author": "著者名（不明ならnull）",
      "published_date": "YYYY-MM-DD（不明ならnull）",
      "cover_image_url": "書影URL（絶対URLが望ましい。不明ならnull）"
    }}
  ]
}}
"""
    text = _generate_with_retry(prompt, model=model or settings.gemini_enrichment_model)
    if not text:
        return []
    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        return []
    try:
        data = json.loads(m.group())
        return data.get("books", []) or []
    except json.JSONDecodeError:
        return []


def discover_books_from_official_site(year: int, max_pages: int = 30, model: str | None = None) -> list[dict]:
    """
    講談社ラノベ文庫公式サイト( ?label=&year={year} )をHTTPで直接取得し、
    Gemini 3.1 Pro でHTMLをパースして書籍リストを返す。
    ページネーションがあれば全ページ巡回する。
    """
    base_url = f"https://lanove.kodansha.co.jp/books/?label=&year={year}"
    all_books: list[dict] = []
    seen_titles: set[str] = set()

    for page in range(1, max_pages + 1):
        url = base_url if page == 1 else f"{base_url}&page={page}"
        html = _fetch_html(url)
        if not html:
            break

        books = _extract_books_from_html(html, year, model=model)
        if not books:
            # 1ページ目で0件 → 該当なし、2ページ目以降で0件 → 終了
            break

        books, dropped = _filter_books(books)
        if dropped:
            print(f"  [page {page}] 特典付き・非小説を除外 {dropped}件")

        new_on_page = 0
        for b in books:
            t = (b.get("title") or "").strip()
            if not t or t in seen_titles:
                continue
            seen_titles.add(t)
            all_books.append(b)
            new_on_page += 1

        print(f"  [page {page}] 抽出 {len(books)}件 / 新規 {new_on_page}件")
        # 新規追加が0なら次ページに進んでも増えないのでループ終了
        if new_on_page == 0:
            break

    return all_books


def _extract_books_from_ga_html(html: str, model: str | None = None) -> list[dict]:
    """GA文庫公式サイトのHTMLから書籍を抽出（GA文庫/GAノベル両方含む）"""
    cleaned = _strip_html(html)
    prompt = f"""
以下はSBクリエイティブのGA文庫/GAノベル公式サイトの書籍一覧ページのHTMLです。
このページに掲載されている書籍のうち、**ライトノベル（GA文庫・GAノベル）のみ**を列挙してください。

【含めるもの】
- レーベル「GA文庫」（文庫サイズのライトノベル）
- レーベル「GAノベル」（単行本サイズのライトノベル／Web小説の書籍化）

【除外するもの — 厳守（出力 books 配列に含めない）】
- コミック・漫画（コミカライズ含む）
  例: レーベル名「GAコミック」「GAコミックス」「GAコミカライズ」、作画担当が表記、「○○（漫画）」
- 特装版・限定版・初回限定版・特典付き版・豪華版・完全版（通常版と内容重複するため）
- ドラマCD付き・DLコード付き・シリアルコード付き等の特典セット
- アクリルスタンド／ブロマイド／カレンダー等のグッズ付き版
- 画集・設定資料集・ファンブック・公式ガイドブック・アンソロジー・パンフレット等の非小説書籍
- label フィールドには **"GA文庫" または "GAノベル" 以外を絶対に出力しない**
- 上記に該当する書籍はそもそも books 配列から除外する

【厳守】
- HTMLに含まれるライトノベルは省略せず全て出力すること
- HTMLに書かれていない書籍を勝手に追加しないこと
- 各書籍が「GA文庫」か「GAノベル」かをHTMLから判別して label に入れる
- 発売日はHTMLの記載をYYYY-MM-DD形式に正規化（不明ならnull）
- 書影URLは絶対URLが望ましい

【title フィールドの作り方 — 厳守】
- 書影下や商品カードに書かれている「書籍名そのもの」だけを入れる
- title に**著者名・レーベル名（GA文庫/GAノベル）・「著」「イラスト」等の役職・発売日**を含めないこと
- 巻数表記（例「○○ 3」「○○ 第3巻」「○○ III」）は title にそのまま含めてよい
- サブタイトル・〜記号で囲まれた副題は title に含める（例「○○ 〜異世界編〜」）

HTML:
{cleaned}

出力形式（JSONのみ。前後に説明文は不要）:
{{
  "books": [
    {{
      "title": "書籍タイトル",
      "author": "著者名（不明ならnull）",
      "published_date": "YYYY-MM-DD（不明ならnull）",
      "cover_image_url": "書影URL（不明ならnull）",
      "label": "GA文庫" または "GAノベル"
    }}
  ]
}}
"""
    text = _generate_with_retry(prompt, model=model or settings.gemini_enrichment_model)
    if not text:
        return []
    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        return []
    try:
        data = json.loads(m.group())
        return data.get("books", []) or []
    except json.JSONDecodeError:
        return []


def discover_books_from_ga_site(start_year: int, end_year: int,
                                 max_pages: int = 100,
                                 model: str | None = None) -> list[dict]:
    """
    GA文庫/GAノベル公式サイト( https://ga.sbcr.jp/product/page/{N}/ )を
    1ページずつ巡回し、[start_year, end_year] に該当する書籍を返す。

    サイトは新しい順に並んでいる前提で、ページ内の全書籍が start_year より
    古くなったら巡回を打ち切る。
    """
    base_url = "https://ga.sbcr.jp/product/page/{}/"
    all_books: list[dict] = []
    seen_titles: set[str] = set()

    for page in range(1, max_pages + 1):
        url = base_url.format(page)
        html = _fetch_html(url)
        if not html:
            break

        books = _extract_books_from_ga_html(html, model=model)
        if not books:
            break

        # コミック等の混入を防御的に弾く（label が GA文庫/GAノベル のみ採用）
        before = len(books)
        books = [b for b in books if (b.get("label") or "").strip() in ("GA文庫", "GAノベル")]
        if before != len(books):
            print(f"  [page {page}] コミック等を除外 {before - len(books)}件")

        # 特典付き・非小説をタイトル正規表現で除外
        books, dropped = _filter_books(books)
        if dropped:
            print(f"  [page {page}] 特典付き・非小説を除外 {dropped}件")

        # ページ上の年を集計（範囲判定用）
        years_on_page: list[int] = []
        for b in books:
            d = (b.get("published_date") or "").strip()
            ym = re.match(r"(\d{4})", d)
            if ym:
                years_on_page.append(int(ym.group(1)))

        in_range_count = 0
        for b in books:
            t = (b.get("title") or "").strip()
            if not t or t in seen_titles:
                continue
            d = (b.get("published_date") or "").strip()
            ym = re.match(r"(\d{4})", d)
            if ym:
                yr = int(ym.group(1))
                if not (start_year <= yr <= end_year):
                    continue
            else:
                # 日付不明はとりあえず採用（範囲ページの近辺なら拾う）
                if years_on_page and max(years_on_page) < start_year:
                    continue
            seen_titles.add(t)
            all_books.append(b)
            in_range_count += 1

        print(f"  [page {page}] HTML抽出 {len(books)}件 / 範囲内 {in_range_count}件 "
              f"(年: {min(years_on_page) if years_on_page else '-'}〜{max(years_on_page) if years_on_page else '-'})")

        # 全書籍が start_year より古い → これ以降のページも全て古いはずなので終了
        if years_on_page and max(years_on_page) < start_year:
            print(f"  → 全て {start_year}年より古い書籍 → 巡回終了")
            break

    return all_books


def parse_volume_titles(titles: list[str]) -> dict[str, tuple[str, int | None]]:
    """
    巻タイトルのリストを Gemini 2.5 Flash に渡してバッチで分解する。
    Returns: {"original_title": (series_title, volume_number)} の dict
    Geminiが応答しない/JSON不正の場合は元タイトルをシリーズ名・巻数Noneで返す。
    """
    titles = [t for t in (titles or []) if t]
    if not titles:
        return {}

    listed = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
    prompt = f"""
以下のライトノベルの巻タイトルから、**巻数（数字）だけを取り除いた文字列**と
**巻数（整数）**を返してください。

【厳守 — シリーズ名の作り方】
- 元のタイトルから「巻数表記の部分」だけを削除する
- それ以外の文字（サブタイトル・〜記号で囲まれた部分・スペース・記号など）は**絶対に削除しない**
- 巻数を抜いた後に余分な空白が連続したら1つに整える程度のみ許可

例:
  「俺と彼女の最終戦争 3」              → シリーズ名「俺と彼女の最終戦争」、巻数 3
  「追放魔術師のその後４ 新天地で始めるスローライフ」
                                       → シリーズ名「追放魔術師のその後 新天地で始めるスローライフ」、巻数 4
  「○○ 〜異世界転生編〜 5」            → シリーズ名「○○ 〜異世界転生編〜」、巻数 5
  「○○の物語 第3巻」                   → シリーズ名「○○の物語」、巻数 3
  「○○(III)」                          → シリーズ名「○○」、巻数 3
  「7人の魔女」                          → シリーズ名「7人の魔女」、巻数 null（タイトルの一部の数字は削らない）
  「○○ファンブック」                   → シリーズ名「○○ファンブック」、巻数 null

【巻数の認識基準】
- 半角数字／全角数字／ローマ数字（I〜X）／「第N巻」「N巻」表記
- 単独で書かれた数字（前後がスペース・括弧・記号）は巻数とみなしてよい
- タイトル本体に含まれる数字（例「7人」「3秒」など意味のある数字）は巻数として扱わない

タイトル一覧（番号付き）:
{listed}

出力形式（JSONのみ。前後に説明文不要）:
{{
  "results": [
    {{"index": 1, "series_title": "巻数を取り除いたタイトル", "volume_number": 3}},
    {{"index": 2, "series_title": "元のタイトルそのまま", "volume_number": null}}
  ]
}}
"""
    text = _generate_with_retry(prompt, model=settings.gemini_parse_model)
    out: dict[str, tuple[str, int | None]] = {}
    if text:
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            try:
                data = json.loads(m.group())
                for r in data.get("results", []) or []:
                    idx = r.get("index")
                    if not isinstance(idx, int) or not (1 <= idx <= len(titles)):
                        continue
                    original = titles[idx - 1]
                    series = (r.get("series_title") or "").strip() or original
                    vol = r.get("volume_number")
                    if vol is not None and not isinstance(vol, int):
                        try:
                            vol = int(vol)
                        except (ValueError, TypeError):
                            vol = None
                    out[original] = (series, vol)
            except json.JSONDecodeError:
                pass

    # 取れなかったタイトルは元タイトル＝シリーズ名、巻数Noneで埋める
    for t in titles:
        out.setdefault(t, (t, None))
    return out


def parse_volume_title(title: str) -> tuple[str, int | None]:
    """単一タイトルの分解（薄いラッパー）"""
    return parse_volume_titles([title]).get(title, (title, None))


def lookup_published_date(series_title: str, volume_title: str,
                          volume_number: int | None = None) -> str | None:
    """
    Gemini 3.1 Pro + Web検索で発売日を調べ直す。
    "YYYY-MM-DD" を返す。不明なら None。
    """
    vol_info = f"巻数: {volume_number}" if volume_number is not None else ""
    prompt = f"""
講談社ラノベ文庫から刊行された以下の書籍の正確な発売日を、Web検索で確認してください。

シリーズ名: {series_title}
巻タイトル: {volume_title}
{vol_info}

【厳守】
- レーベルが「講談社ラノベ文庫」の該当巻のみ
- 発売日は単行本（紙書籍）の発売日。電子書籍版の日付は採用しないこと
- 公式サイト・Amazon・honto・楽天ブックスなど複数ソースで一致する日付を採用
- 不明・自信がない場合は null
- 月までしか分からない場合も null（推測しないこと）

出力形式（JSONのみ）:
{{ "published_date": "YYYY-MM-DD" または null }}
"""
    text = _generate_with_retry(
        prompt,
        model=settings.gemini_parse_model,  # 2.5 Flash（軽量・高速）
        use_grounding=True,
    )
    if not text:
        return None
    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        return None
    try:
        data = json.loads(m.group())
        d = data.get("published_date")
        if d and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d).strip()):
            return d.strip()
    except json.JSONDecodeError:
        pass
    return None


def generate_recommendation_tags(user_preference: str) -> list[str]:
    """ユーザーの「読みたい雰囲気」テキストからタグリストを生成する"""
    prompt = f"""
ユーザーが読みたいライトノベルの雰囲気を説明した文章から、
検索に使えるタグリストをJSON配列で返してください。

ユーザー入力: {user_preference}

タグ候補の例: 胸キュン, ダークファンタジー, ほのぼの, バトルアクション, 学園コメディ,
泣ける, ハーレム, 異世界転生, ミステリー, SF, ホラー, 日常系, ラブコメ, バトル, 冒険

出力形式（JSON配列のみ）:
["タグ1", "タグ2", "タグ3"]
"""
    text = _generate_with_retry(prompt, model=settings.gemini_analysis_model)
    if text:
        m = re.search(r'\[.*\]', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return []
