"""
retag_genre.py — ジャンルタグだけを Gemini 3.1 Pro + grounding で再評価する。

- 対象: 全シリーズ (親シリーズ・非特装)
- 候補: tags テーブルの genre タグ全件
- 効果: 各シリーズの genre_tags 配列を AI 判定で完全置換 (atmosphere_tags / sub_genre_tags は触らない)
- 複数シリーズを 1 call にまとめる「バッチモード」で grounding コストを削減
  (grounding 料金は call 数 × $0.035 なので、call 数を減らすと直接効く)
- 全 call で Gemini API の usage_metadata を集計し、実測トークンに基づくコスト推定を表示

4 モード:
  --estimate           : DB だけ読んで件数とコストを概算 (API は呼ばない、料金 0)
  --calibrate N        : N シリーズだけ実 API call して実測トークンで全件外挿
  --dry-run            : API を呼ぶが DB は保存しない (品質確認 + 実測)
  --apply              : 本実行 (DB 保存)

使い方:
  python -m scripts.retag_genre --estimate --batch-size 10
  python -m scripts.retag_genre --calibrate 30 --batch-size 10 --workers 1
  python -m scripts.retag_genre --apply --batch-size 10 --workers 3
"""
import sys
import os
import argparse
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.genai import types

from app.services.supabase_client import get_supabase
from app.services.gemini import (
    client,
    _format_books_for_prompt,
    _parse_json_object,
)


# Gemini 3.1 Pro pricing (2026-06 時点の公開値、変動の可能性あり)
# thinking tokens は output と同じ単価で課金される想定 (Google 公式仕様)
PRICING = {
    "input_per_1m_usd":      1.25,    # Input tokens $/1M
    "output_per_1m_usd":     5.00,    # Output + thinking tokens $/1M
    "grounding_per_call_usd": 0.035,  # Google Search grounding ($35/1000 call)
}
# 構造ベースの粗い推定 (--estimate で使用、--calibrate なしのとき)
TOKEN_ESTIMATE = {
    "input_per_series":  1500,
    "output_per_series": 50,
    "fixed_overhead":    600,
}
SEC_PER_CALL_SINGLE = 6
SEC_PER_CALL_BATCH_FACTOR = 0.6


# 全 worker 共有の usage 集計
_USAGE = {
    "calls": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "thinking_tokens": 0,
    "grounded_calls": 0,
    "fallback_calls": 0,
}
_USAGE_LOCK = threading.Lock()


def _accumulate_usage(input_tokens: int, output_tokens: int,
                      thinking_tokens: int = 0, grounded: bool = False):
    with _USAGE_LOCK:
        _USAGE["calls"] += 1
        _USAGE["input_tokens"] += input_tokens or 0
        _USAGE["output_tokens"] += output_tokens or 0
        _USAGE["thinking_tokens"] += thinking_tokens or 0
        if grounded:
            _USAGE["grounded_calls"] += 1


def _call_gemini_with_usage(prompt: str, model: str, use_grounding: bool,
                             max_retries: int = 5) -> tuple[str | None, dict]:
    """Gemini を呼び、テキストと usage_metadata を返す。
    429 (quota) は指数待機リトライ。他のエラーは raise。"""
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
            # usage_metadata 抽出 (SDK バージョン差を吸収)
            usage_meta = getattr(response, "usage_metadata", None)
            usage = {
                "input_tokens":    getattr(usage_meta, "prompt_token_count", 0) or 0,
                "output_tokens":   getattr(usage_meta, "candidates_token_count", 0) or 0,
                "thinking_tokens": getattr(usage_meta, "thoughts_token_count", 0) or 0,
                "total_tokens":    getattr(usage_meta, "total_token_count", 0) or 0,
            }
            text = (response.text or "").strip()
            _accumulate_usage(
                usage["input_tokens"], usage["output_tokens"],
                usage["thinking_tokens"], grounded=use_grounding,
            )
            return text, usage
        except Exception as e:
            err = str(e)
            if "429" in err or "quota" in err.lower():
                wait = 60
                m = re.search(r'retry in (\d+\.?\d*)s', err)
                if m:
                    wait = int(float(m.group(1))) + 5
                print(f"  [429] {wait}秒待機 (試行 {attempt + 1}/{max_retries})")
                time.sleep(wait)
            else:
                # 非 429 はエラーをそのまま返す
                print(f"  [Gemini エラー] {err[:200]}")
                return None, {}
    print(f"  [Gemini] リトライ上限到達")
    return None, {}


def print_actual_usage(target_count: int, batch_size: int):
    """実測 _USAGE から実コスト + 全件への外挿を表示"""
    with _USAGE_LOCK:
        u = dict(_USAGE)
    if u["calls"] == 0:
        return
    input_cost = u["input_tokens"] / 1_000_000 * PRICING["input_per_1m_usd"]
    output_cost = (u["output_tokens"] + u["thinking_tokens"]) / 1_000_000 * PRICING["output_per_1m_usd"]
    grounding_cost = u["grounded_calls"] * PRICING["grounding_per_call_usd"]
    total = input_cost + output_cost + grounding_cost

    # 1 call あたり平均
    avg_in = u["input_tokens"] / u["calls"]
    avg_out = u["output_tokens"] / u["calls"]
    avg_think = u["thinking_tokens"] / u["calls"]
    series_processed_per_call = batch_size
    avg_cost_per_call = total / u["calls"]

    print("\n========== 実測 (このバッチ run) ==========")
    print(f"  完了 call:       {u['calls']}")
    print(f"  grounded call:   {u['grounded_calls']}")
    print(f"  input tokens:    {u['input_tokens']:,}   ({avg_in:,.0f}/call)")
    print(f"  output tokens:   {u['output_tokens']:,}   ({avg_out:,.0f}/call)")
    print(f"  thinking tokens: {u['thinking_tokens']:,}   ({avg_think:,.0f}/call)  ← Pro系の追加分")
    print()
    print(f"  Input cost:      ${input_cost:>7.2f}")
    print(f"  Output+Think:    ${output_cost:>7.2f}")
    print(f"  Grounding cost:  ${grounding_cost:>7.2f}")
    print(f"  ───────")
    print(f"  この run の実コスト: ${total:.2f}")
    print()
    # 全件外挿
    if target_count > 0 and target_count > batch_size:
        total_calls = (target_count + batch_size - 1) // batch_size
        scale = total_calls / u["calls"]
        ext_in = u["input_tokens"] * scale
        ext_out = u["output_tokens"] * scale
        ext_think = u["thinking_tokens"] * scale
        ext_grounded = u["grounded_calls"] * scale
        ext_input_cost = ext_in / 1_000_000 * PRICING["input_per_1m_usd"]
        ext_output_cost = (ext_out + ext_think) / 1_000_000 * PRICING["output_per_1m_usd"]
        ext_grounding_cost = ext_grounded * PRICING["grounding_per_call_usd"]
        ext_total = ext_input_cost + ext_output_cost + ext_grounding_cost
        print(f"========== 全件外挿 ({target_count:,} シリーズ) ==========")
        print(f"  予想 call 数:      {total_calls:,}")
        print(f"  予想 input cost:   ${ext_input_cost:>7.2f}")
        print(f"  予想 output+think: ${ext_output_cost:>7.2f}")
        print(f"  予想 grounding:    ${ext_grounding_cost:>7.2f}")
        print(f"  ───────")
        print(f"  予想合計:          ${ext_total:.2f}  (1 シリーズあたり ${ext_total/target_count:.4f})")
    print("==================================\n")


def _paginate(query_factory, page_size: int = 1000) -> list[dict]:
    rows = []
    offset = 0
    while True:
        q = query_factory()
        rs = q.range(offset, offset + page_size - 1).execute()
        batch = rs.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def fetch_genre_candidates(sb) -> list[dict]:
    rs = sb.table("tags").select(
        "name, description, is_ai_generated"
    ).eq("tag_type", "genre").execute()
    return rs.data or []


def fetch_targets(sb, limit: int | None, imprint: str | None,
                  skip_updated_since: str | None = None,
                  exclude_title_prefixes: set[str] | None = None,
                  series_id: str | None = None,
                  created_before: str | None = None,
                  series_ids: list[str] | None = None) -> list[dict]:
    def factory():
        q = sb.table("openbd_test_series").select(
            "id, series_title, author, imprint, volume_count, genre_tags, updated_at"
        ).is_("parent_series_id", "null").eq("is_special_edition", False)
        if series_ids:
            q = q.in_("id", series_ids)
        if series_id:
            q = q.eq("id", series_id)
        if created_before:
            q = q.lt("created_at", created_before)
        if imprint:
            q = q.eq("imprint", imprint)
        if skip_updated_since:
            # この時刻より前に更新された (= 今回 run で触っていない) ものだけ対象
            q = q.lt("updated_at", skip_updated_since)
        # ID 順で固定 → 中断したときも同じ順序で再開できる
        q = q.order("id", desc=False)
        return q
    rows = _paginate(factory)
    # ログから抽出したタイトル前 40 文字とマッチする series は除外
    if exclude_title_prefixes:
        before = len(rows)
        rows = [r for r in rows
                if (r.get("series_title") or "")[:40] not in exclude_title_prefixes]
        print(f"  → ログ照合スキップ: {before - len(rows):,} 件除外 (残り {len(rows):,})")
    if limit:
        rows = rows[:limit]
    return rows


_LOG_CHANGED_RE = re.compile(
    r"^\s*✓\s+(?P<title>.+?)\s\s+\[.*?\]\s*→\s*\[.*?\]\s*$"
)


def parse_processed_titles_from_log(log_path: str) -> set[str]:
    """retag_genre.py のログから「処理済み」タイトル前 40 文字を抽出する。
    変更ありの ✓→ 行のみログに残るので、変更なしの series は検出不能 (= 再処理になる)。
    """
    titles: set[str] = set()
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            if "→" not in line or "✓" not in line:
                continue
            m = _LOG_CHANGED_RE.match(line.rstrip("\n"))
            if m:
                titles.add(m.group("title").strip())
    return titles


def fetch_books(sb, sid: str) -> list[dict]:
    rs = sb.table("openbd_test_books").select(
        "isbn, title, description, volume_number"
    ).eq("series_id", sid).order("volume_number").limit(50).execute()
    return rs.data or []


def _series_block(row: dict, sb) -> str:
    """1 シリーズ分の情報ブロックを組み立て (バッチ内に複数並べる)"""
    title = row["series_title"]
    author = row.get("author") or "?"
    imprint = row.get("imprint") or ""
    vc = row.get("volume_count") or "?"
    books = fetch_books(sb, row["id"])
    book_section = _format_books_for_prompt(books, max_books=4, max_desc_chars=250) if books else "(書籍情報なし)"
    return f"""── series_id: {row['id']}
  シリーズ名: 「{title}」
  著者: {author}  レーベル: {imprint}  既刊巻数: {vc}
  書籍情報:
{book_section}"""


def build_batch_prompt(series_blocks: list[str], candidates: list[dict]) -> str:
    """N シリーズ分の判定プロンプト"""
    cand_lines = []
    for it in candidates:
        name = it.get("name")
        desc = (it.get("description") or "").strip()
        if desc:
            cand_lines.append(f"  - {name}: {desc}")
        else:
            cand_lines.append(f"  - {name}")
    cand_block = "\n".join(cand_lines)
    series_block = "\n\n".join(series_blocks)
    return f"""
以下の {len(series_blocks)} 件のライトノベルシリーズについて、それぞれのジャンルタグ (genre_tags) を、
下記候補リストの中から「明確に該当するもののみ」選んでください。
Web 検索も活用して、作品の正確なジャンルを判定してください。

【ジャンル候補タグ — この中からのみ選ぶ】
{cand_block}

【シリーズ一覧】
{series_block}

【判定ルール — 厳守】
- 候補リストに無いタグは絶対に追加しない
- 作品の特徴として明確に該当するものだけ (推測・連想・「なんとなく」は不可)
- 該当が無ければ空配列を返す
- 複数該当する場合は全て選ぶ
- **必ず全 {len(series_blocks)} 件について返すこと** (順序は問わないが series_id は必須)

【出力 (JSON のみ、コードフェンス禁止)】
{{
  "series": [
    {{"series_id": "<UUID>", "genre_tags": ["..."]}},
    ...
  ]
}}
"""


def print_estimate(n_series: int, n_candidates: int, workers: int, batch_size: int):
    n_calls = (n_series + batch_size - 1) // batch_size
    input_tokens = n_calls * TOKEN_ESTIMATE["fixed_overhead"] + n_series * TOKEN_ESTIMATE["input_per_series"]
    output_tokens = n_series * TOKEN_ESTIMATE["output_per_series"]
    input_cost = input_tokens / 1_000_000 * PRICING["input_per_1m_usd"]
    output_cost = output_tokens / 1_000_000 * PRICING["output_per_1m_usd"]
    grounding_cost = n_calls * PRICING["grounding_per_call_usd"]
    total_cost = input_cost + output_cost + grounding_cost
    sec_per_call = SEC_PER_CALL_SINGLE + (batch_size - 1) * SEC_PER_CALL_BATCH_FACTOR
    total_sec = n_calls * sec_per_call / max(workers, 1)

    print("\n========== 事前見積もり ==========")
    print(f"対象シリーズ数:      {n_series:,}")
    print(f"ジャンル候補タグ数:    {n_candidates}")
    print(f"バッチサイズ:        {batch_size} シリーズ/call")
    print(f"並列ワーカー数:      {workers}")
    print()
    print("[リクエスト量]")
    print(f"  Gemini API call:    {n_calls:,}")
    print(f"  Input tokens 合計:  {input_tokens:>10,}")
    print(f"  Output tokens 合計: {output_tokens:>10,}")
    print()
    print("[コスト概算] (Gemini 3.1 Pro Preview + Google Search Grounding)")
    print(f"  Input cost:       ${input_cost:>7.2f}  (${PRICING['input_per_1m_usd']}/1M tokens)")
    print(f"  Output cost:      ${output_cost:>7.2f}  (${PRICING['output_per_1m_usd']}/1M tokens)")
    print(f"  Grounding cost:   ${grounding_cost:>7.2f}  (${PRICING['grounding_per_call_usd']}/call × {n_calls} call)")
    print(f"  -----")
    print(f"  合計:             ${total_cost:>7.2f}")
    print()
    print("[時間概算]")
    print(f"  1 call ~{sec_per_call:.1f}秒、workers={workers}")
    print(f"  推定総時間:    {total_sec/60:.0f} 分  ({total_sec/3600:.1f} 時間)")
    print()
    # バッチ化による節約 (参考)
    single_calls = n_series
    single_grounding = single_calls * PRICING["grounding_per_call_usd"]
    single_total = single_grounding + input_cost + output_cost  # tokens は微差 (overhead 増)
    savings = single_total - total_cost
    if batch_size > 1 and savings > 0.5:
        print(f"※ batch_size=1 の場合: 約 ${single_total:.2f}  (差額 ${savings:.2f} の節約)")
    print("※ 価格は 2026-06 時点の公開値ベース。実料金は Google AI Studio で要確認")
    print("==================================\n")


def process_batch(sb, batch_rows: list[dict], candidates: list[dict],
                  model: str, use_grounding: bool, dry_run: bool) -> list[dict]:
    """1 バッチを 1 call で処理。各 series ごとの info 配列を返す。"""
    infos = []
    for row in batch_rows:
        infos.append({
            "sid": row["id"], "title": row["series_title"],
            "before": list(row.get("genre_tags") or []),
            "after": None, "error": None,
        })

    # 各 series block を組み立て
    blocks = []
    for row in batch_rows:
        try:
            blocks.append(_series_block(row, sb))
        except Exception as e:
            # 個別エラー (書籍取得失敗等) は info に記録するが、バッチ自体は続行
            for i, br in enumerate(batch_rows):
                if br["id"] == row["id"]:
                    infos[i]["error"] = f"書籍取得失敗: {e}"
            return infos
    prompt = build_batch_prompt(blocks, candidates)
    valid_names = {it["name"] for it in candidates}

    try:
        text, _usage = _call_gemini_with_usage(prompt, model=model, use_grounding=use_grounding)
    except Exception as e:
        for info in infos:
            info["error"] = f"Gemini 呼び出し失敗: {e}"
        return infos
    if not text:
        for info in infos:
            info["error"] = "Gemini レスポンス空 (or 全リトライ失敗)"
        return infos

    data = _parse_json_object(text)
    if not data:
        for info in infos:
            info["error"] = "JSON parse 失敗"
        return infos

    # series_id でマッピング
    by_id = {info["sid"]: info for info in infos}
    items = data.get("series") or []
    for it in items:
        sid = it.get("series_id")
        info = by_id.get(sid)
        if not info:
            continue
        selected = [n for n in (it.get("genre_tags") or [])
                    if isinstance(n, str) and n in valid_names]
        info["after"] = selected

    # 漏れたシリーズはエラー扱い
    for info in infos:
        if info.get("after") is None and not info.get("error"):
            info["error"] = "Gemini 応答に series_id が含まれず"

    if dry_run:
        return infos

    # DB 保存 (エラーのないものだけ)
    for info in infos:
        if info.get("error") or info.get("after") is None:
            continue
        try:
            sb.table("openbd_test_series").update(
                {"genre_tags": info["after"]}
            ).eq("id", info["sid"]).execute()
        except Exception as e:
            info["error"] = f"保存失敗: {e}"
    return infos


def main():
    parser = argparse.ArgumentParser(description="ジャンルタグだけ Pro+grounding で再評価 (バッチ化)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--estimate", action="store_true",
                      help="(デフォルト) 件数とコスト見積もりのみ。API は呼ばない")
    mode.add_argument("--calibrate", type=int, metavar="N",
                      help="N シリーズだけ実 API call して実トークン計測 → 全件外挿。DB は保存しない")
    mode.add_argument("--dry-run", action="store_true",
                      help="API は呼ぶが DB は保存しない")
    mode.add_argument("--apply", action="store_true",
                      help="本実行 (確認プロンプトあり)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--imprint", default=None)
    parser.add_argument("--series-id", default=None, help="特定シリーズ1件だけ")
    parser.add_argument("--series-ids-file", default=None,
                        help="シリーズID(1行1つ)のファイル。指定IDのみ対象")
    parser.add_argument("--created-before", default=None,
                        help="この日時より前に作成された series のみ (既存のみ対象用)")
    parser.add_argument("--model", default="gemini-3.1-pro-preview")
    parser.add_argument("--no-grounding", action="store_true",
                        help="grounding を OFF にする (デフォルト ON)")
    parser.add_argument("--workers", type=int, default=3,
                        help="並列ワーカー数 (デフォルト 3)")
    parser.add_argument("--batch-size", type=int, default=10,
                        help="1 call にまとめるシリーズ数 (デフォルト 10。"
                             "大きいほど grounding コスト削減、ただし精度低下リスク)")
    parser.add_argument("--skip-updated-since", default=None,
                        metavar="ISO_TIMESTAMP",
                        help="再開用: この日時 (ISO 8601, 例 '2026-06-13T03:00:00+00:00') "
                             "以降に updated_at が更新された series をスキップ。"
                             "中断した run の開始時刻を指定すれば、処理済みを飛ばして続行できる")
    parser.add_argument("--exclude-titles-from-log", default=None,
                        metavar="LOG_PATH",
                        help="再開用: 前回 run のログを読み、変更ありで記録された title を "
                             "fetch_targets から除外。Gemini call が減りコスト節約 "
                             "(updated_at トリガーが無い表でも使える)")
    args = parser.parse_args()
    if not (args.estimate or args.dry_run or args.apply or args.calibrate):
        args.estimate = True
    if args.batch_size < 1:
        args.batch_size = 1

    # calibrate モードは dry-run 同等の挙動 + limit を上書き
    calibrate_mode = args.calibrate is not None and args.calibrate > 0
    if calibrate_mode:
        args.dry_run = True
        # サンプル件数を制限 (--limit が指定されていればそちらを尊重)
        if args.limit is None or args.limit > args.calibrate:
            args.limit = args.calibrate

    sb = get_supabase()
    use_grounding = not args.no_grounding
    print(f"[Gemini] model={args.model}  grounding={use_grounding}  "
          f"batch_size={args.batch_size}")
    if args.skip_updated_since:
        print(f"[再開] updated_at >= {args.skip_updated_since} の series はスキップ")

    candidates = fetch_genre_candidates(sb)
    if not candidates:
        print("genre タグ候補なし")
        return
    print(f"[genre 候補] {len(candidates)} 件: "
          f"{', '.join(it['name'] for it in candidates)}")

    excluded_titles: set[str] = set()
    if args.exclude_titles_from_log:
        if not os.path.exists(args.exclude_titles_from_log):
            print(f"[エラー] ログファイルが存在しません: {args.exclude_titles_from_log}")
            return
        excluded_titles = parse_processed_titles_from_log(args.exclude_titles_from_log)
        print(f"[ログ照合] 既処理タイトル抽出: {len(excluded_titles):,} 件")
    sids_file = None
    if args.series_ids_file:
        with open(args.series_ids_file, encoding="utf-8") as f:
            sids_file = [ln.strip() for ln in f if ln.strip()]
    targets = fetch_targets(sb, args.limit, args.imprint,
                            skip_updated_since=args.skip_updated_since,
                            exclude_title_prefixes=excluded_titles or None,
                            series_id=args.series_id,
                            created_before=args.created_before,
                            series_ids=sids_file)
    print(f"[対象] {len(targets):,} シリーズ"
          + (f"  (calibrate: {args.calibrate} サンプル)" if calibrate_mode else ""))

    # 構造ベース推定 (calibrate モードでは外挿用に「全件数」も別途取得)
    full_target_count = len(targets)
    if calibrate_mode:
        # 外挿用: skip_updated_since と除外フィルタを適用した全件数
        full_target_count = fetch_targets(
            sb, None, args.imprint,
            skip_updated_since=args.skip_updated_since,
            exclude_title_prefixes=excluded_titles or None,
        )
        full_target_count = len(full_target_count)
        print(f"[全件 (外挿用)] {full_target_count:,} シリーズ")
    print_estimate(full_target_count, len(candidates), args.workers, args.batch_size)

    if args.estimate:
        print("(--estimate モード: ここで終了)")
        return
    if args.apply:
        confirm = input("\n上記の条件で実行しますか? [y/N]: ").strip().lower()
        if confirm != "y":
            print("中止")
            return

    # バッチに分割
    batches = [targets[i:i + args.batch_size]
               for i in range(0, len(targets), args.batch_size)]
    counters = {"ok": 0, "ng": 0, "empty": 0, "changed": 0}
    counters_lock = threading.Lock()
    print_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(process_batch, sb, batch, candidates,
                      args.model, use_grounding, args.dry_run): batch
            for batch in batches
        }
        done = 0
        for fut in as_completed(futures):
            batch = futures[fut]
            try:
                infos = fut.result()
            except Exception as e:
                with counters_lock:
                    counters["ng"] += len(batch)
                with print_lock:
                    print(f"  ! バッチ例外 ({len(batch)} 件): {e}")
                continue
            with counters_lock:
                done += len(infos)
            for info in infos:
                with counters_lock:
                    if info.get("error"):
                        counters["ng"] += 1
                    else:
                        counters["ok"] += 1
                        after = info["after"] or []
                        before = info["before"] or []
                        if not after:
                            counters["empty"] += 1
                        if set(before) != set(after):
                            counters["changed"] += 1
                with print_lock:
                    if info.get("error"):
                        print(f"  ! {info['title'][:40]}  {info['error']}")
                    else:
                        before = info["before"] or []
                        after = info["after"] or []
                        if set(before) != set(after):
                            print(f"  ✓ {info['title'][:40]}  {before} → {after}")
            with print_lock:
                print(f"  [{done}/{len(targets)}] バッチ完了 ({len(infos)} 件)")

    print("\n========== 完了サマリ ==========")
    print(f"成功:                {counters['ok']:,}")
    print(f"失敗:                {counters['ng']:,}")
    print(f"うちタグ変更:         {counters['changed']:,}")
    print(f"うちタグ空 (該当無):   {counters['empty']:,}")
    if args.dry_run:
        print("(DRY RUN: DB は更新されていません)")

    # 実測コスト + 全件外挿
    print_actual_usage(full_target_count, args.batch_size)


if __name__ == "__main__":
    main()
