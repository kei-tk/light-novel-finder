"""
ハイブリッド類似検索 CLI (ジャンル絞り込み + 物語ベクトル順位付け)。

設計:
  - 物語内容・テーマの類似は review_embedding (Grok レビュー要約由来) で測る。
    あらすじ由来 embedding は「学園恋愛もの」等に丸まり百合などの特異性が消えるため、
    レビュー要約ベースの review_embedding を主軸にする。
  - ジャンルの希釈を防ぐため、ジャンルタグ (genre_tags / sub_genre_tags) で
    先に絞り込む (フィルタ)。--similar-to では対象作品のジャンルタグで自動絞り込み、
    --query では --genre で明示指定する。
  - スコア軸フィルタ (例: コメディ度>=60) も任意で併用できる。

モード:
  1. --query "..."  [--genre 百合 ...]   自然言語クエリ + 任意ジャンル絞り込み
  2. --similar-to <UUID>                 対象作品に似た作品 (対象のジャンルで自動絞り込み)
  3. --centroid <UUID>...                複数作品の重心に近い作品 (おすすめ用)

オプション:
  --genre TAG...        ジャンル/サブジャンルで絞り込み (複数指定で OR)
  --rare-tags N         --similar-to 時、対象の希少タグ上位 N 個で絞り込む (default 2)
  --no-genre-filter     ジャンル絞り込みを無効化 (純ベクトル検索)
  --min-comedy 等       スコア軸の下限フィルタ

使い方:
  python -m scripts.search_series --similar-to <UUID> --limit 10
  python -m scripts.search_series --query "感動して泣ける青春物" --genre 百合 --limit 10
  python -m scripts.search_series --query "ダークな異世界バトル" --genre ファンタジー バトル
  python -m scripts.search_series --centroid <UUID1> <UUID2> --limit 10
"""
import sys
import os
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai import OpenAI
from app.config import settings
from app.services.supabase_client import get_supabase

_openai_client = OpenAI(api_key=settings.openai_api_key)

# スコア軸フィルタで使えるカラム
_SCORE_FIELDS = [
    "comedy_score", "serious_score", "heartwarming_score",
    "bittersweet_score", "romantic_score", "uplifting_score",
    "popularity_score", "momentum_score",
]


def embed_query(text: str) -> list[float]:
    resp = _openai_client.embeddings.create(
        model=settings.openai_embedding_model,
        input=text,
    )
    return resp.data[0].embedding


def _paginate(qf, ps: int = 1000) -> list[dict]:
    rows = []
    off = 0
    while True:
        rs = qf().range(off, off + ps - 1).execute()
        if not rs.data:
            break
        rows.extend(rs.data)
        if len(rs.data) < ps:
            break
        off += ps
    return rows


def fetch_all(supabase) -> list[dict]:
    """review_embedding を持つ series を全件取得"""
    print("[全 series 取得 (review_embedding あり)]")
    cols = ("id, series_title, author, popularity_score, momentum_score, "
            "atmosphere_tags, genre_tags, sub_genre_tags, review_embedding, "
            + ", ".join(_SCORE_FIELDS))
    rows = _paginate(lambda: supabase.table("openbd_test_series").select(cols)
                     .not_.is_("review_embedding", "null"))
    print(f"  → {len(rows):,}件")
    # ベクトルを一度だけパース
    for r in rows:
        r["_vec"] = _parse_vec(r.get("review_embedding"))
    return rows


def _parse_vec(e):
    if isinstance(e, str):
        try:
            return [float(x) for x in e.strip("[]").split(",")]
        except Exception:
            return None
    return e


def cosine(a, b) -> float:
    if not a or not b:
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


def _all_genre_tags(r: dict) -> set[str]:
    return set((r.get("genre_tags") or []) + (r.get("sub_genre_tags") or []))


def _tag_document_frequency(rows: list[dict]) -> dict[str, int]:
    """各 genre_tag が何件の series に出現するか (希少ジャンル判定用)。
    サブジャンル (キャラ属性/トロープ) は識別軸にしないため genre_tags のみ集計する。"""
    df: dict[str, int] = {}
    for r in rows:
        for t in (r.get("genre_tags") or []):
            df[t] = df.get(t, 0) + 1
    return df


def _rarest_genres(target: dict, df: dict[str, int], top_n: int) -> set[str]:
    """対象作品の genre_tags のうち、コーパスで最も希少な (= 識別力が高い) top_n 個。
    「学園/恋愛」のような汎用ジャンルではなく「百合」のような特異ジャンルで絞るため。"""
    tags = target.get("genre_tags") or []
    ranked = sorted(tags, key=lambda t: df.get(t, 0))
    return set(ranked[:top_n])


def apply_filters(rows: list[dict], genres: list[str] | None,
                  required_genres: set[str] | None, match_genre: int,
                  score_mins: dict[str, int]) -> list[dict]:
    """ジャンル絞り込み + スコア下限フィルタを適用"""
    out = []
    for r in rows:
        rtags = _all_genre_tags(r)
        # --genre 指定: いずれか含む (OR)
        if genres and not (set(genres) & rtags):
            continue
        # --similar-to の自動絞り込み: 対象ジャンルと match_genre 個以上一致
        if required_genres is not None:
            if len(required_genres & rtags) < match_genre:
                continue
        # スコア下限
        ok = True
        for field, lo in score_mins.items():
            v = r.get(field)
            if v is None or v < lo:
                ok = False
                break
        if not ok:
            continue
        out.append(r)
    return out


def rank(target_vec, rows, exclude_ids: set[str], limit: int):
    scored = []
    for r in rows:
        if r["id"] in exclude_ids:
            continue
        if not r.get("_vec"):
            continue
        scored.append((r, cosine(target_vec, r["_vec"])))
    scored.sort(key=lambda x: -x[1])
    return scored[:limit]


def print_results(results):
    for r, sim in results:
        genre = ", ".join((r.get("genre_tags") or [])[:4])
        sub = ", ".join((r.get("sub_genre_tags") or [])[:4])
        print(f"  sim={sim:.4f}  pop={r.get('popularity_score')} mom={r.get('momentum_score')}  "
              f"{(r.get('series_title') or '')!r}  著={r.get('author')!r}")
        print(f"      ジャンル=[{genre}]  サブ=[{sub}]")


def main(args):
    if not settings.openai_api_key:
        print("[エラー] OPENAI_API_KEY が未設定")
        sys.exit(1)

    supabase = get_supabase()
    rows = fetch_all(supabase)
    if not rows:
        print("review_embedding 済み series がありません。先に collect_review_summaries を実行してください。")
        return

    score_mins = {f: getattr(args, f"min_{f.replace('_score','')}")
                  for f in _SCORE_FIELDS
                  if getattr(args, f"min_{f.replace('_score','')}", None) is not None}

    required_genres = None
    exclude: set[str] = set()

    if args.query:
        print(f"\n[自然言語検索] query={args.query!r} genre={args.genre}")
        target_vec = embed_query(args.query)
    elif args.similar_to:
        target = next((r for r in rows if r["id"] == args.similar_to), None)
        if not target:
            print(f"[エラー] series_id={args.similar_to} が見つからない (review_embedding 無し?)")
            sys.exit(1)
        target_vec = target["_vec"]
        exclude = {args.similar_to}
        print(f"\n[類似検索] base={target.get('series_title')!r}")
        if not args.no_genre_filter and not args.genre:
            # 対象タグのうち希少な top_n を「絞り込みキー」に使う (1つでも一致すれば残す)
            df = _tag_document_frequency(rows)
            required_genres = _rarest_genres(target, df, args.rare_tags)
            keys = sorted(required_genres, key=lambda t: df.get(t, 0))
            print(f"  絞り込みキー (希少タグ): {[(t, df.get(t,0)) for t in keys]}")
    elif args.centroid:
        sources = [r for r in rows if r["id"] in args.centroid]
        if not sources:
            print("[エラー] centroid 計算可能な source が 0件")
            sys.exit(1)
        dim = len(sources[0]["_vec"])
        target_vec = [sum(s["_vec"][i] for s in sources) / len(sources) for i in range(dim)]
        exclude = set(args.centroid)
        print(f"\n[centroid 推薦] {len(sources)}件の重心から近傍検索")
    else:
        print("[エラー] --query / --similar-to / --centroid のいずれかを指定")
        sys.exit(1)

    if args.no_genre_filter:
        required_genres = None

    # 希少タグ絞り込みは「1つでも一致すれば残す」 (match_genre=1)
    filtered = apply_filters(rows, args.genre, required_genres,
                             1, score_mins)
    print(f"  絞り込み後の候補: {len(filtered):,}件")
    results = rank(target_vec, filtered, exclude, args.limit)
    print(f"\n=== 上位 {len(results)} 件 ===")
    print_results(results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ハイブリッド類似検索 (ジャンル絞り込み + 物語ベクトル)")
    parser.add_argument("--query", type=str, help="自然言語クエリ")
    parser.add_argument("--similar-to", type=str, metavar="UUID", help="この series に似たものを検索")
    parser.add_argument("--centroid", type=str, nargs="+", metavar="UUID", help="複数 series の重心から近傍検索")
    parser.add_argument("--limit", type=int, default=10, help="返却件数")
    # ジャンル絞り込み
    parser.add_argument("--genre", type=str, nargs="+", help="ジャンル/サブジャンルで絞り込み (OR)")
    parser.add_argument("--rare-tags", type=int, default=1,
                        help="--similar-to 時、対象の希少ジャンル上位N個で絞り込む (default 1)")
    parser.add_argument("--no-genre-filter", action="store_true", help="ジャンル絞り込みを無効化")
    # スコア下限フィルタ
    parser.add_argument("--min-comedy", type=int)
    parser.add_argument("--min-serious", type=int)
    parser.add_argument("--min-heartwarming", type=int)
    parser.add_argument("--min-bittersweet", type=int)
    parser.add_argument("--min-romantic", type=int)
    parser.add_argument("--min-uplifting", type=int)
    parser.add_argument("--min-popularity", type=int)
    parser.add_argument("--min-momentum", type=int)
    args = parser.parse_args()
    main(args)
