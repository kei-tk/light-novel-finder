"""
6観点 (facet) ベクトルによるハイブリッド類似検索サービス (インメモリ + numpy)。

各 series は 6つの観点ベクトルを持つ (migration 030):
  plot   : あらすじ・設定・舞台・ギミック
  cast   : 主人公像・キャラ配置・関係性
  theme  : テーマ・葛藤・問題意識
  mood   : 雰囲気・読後感・感情曲線
  reader : どんな読者/気分に向くか
  review : レビュー傾向・評価/不満点

■ 自然言語検索 semantic(query)
  - クエリを 1回だけ embedding。
  - 各 facet の埋め込み行列に当てて facet 別 cosine を出す。
  - クエリ内容で facet 重みを動的に変える: クエリ埋め込みと各 facet の「説明文ベクトル」の
    近さを softmax して重みにする (例: 「泣ける」系クエリは mood が重くなる)。
  - score = Σ_facet weight_facet · cosine_facet

■ 類似検索 similar(series_id)
  - 基準作品との facet 別 cosine を出し、facet ごとに percentile 正規化 (facet 間の
    スケール差を吸収)。
  - 線形 + 二乗の混合で合成: score = Σ_facet (α·p + β·p²)。
    二乗項が「ある facet で非常に近い」作品を強く押し上げる。

埋め込みは正規化保持し cosine = 内積。キャッシュは TTL で自動リフレッシュ。
"""
import time
import threading

import numpy as np
from openai import OpenAI

from app.config import settings
from app.services.supabase_client import get_supabase

# facet キー → DB の embedding 列名
FACETS = ["plot", "cast", "theme", "mood", "reader", "review"]
_FACET_COL = {
    "plot": "plot_embedding",
    "cast": "cast_embedding",
    "theme": "theme_embedding",
    "mood": "mood_embedding",
    "reader": "reader_embedding",
    "review": "review_aspect_embedding",
}

# 各 facet が「何を表すか」の説明文。クエリとの近さで動的重みを決めるのに使う。
_FACET_DESCRIPTOR = {
    "plot": "物語のあらすじ、設定、舞台、世界観、ギミック、どんな展開や出来事が起きるか",
    "cast": "主人公や登場人物のキャラクター像、性格、関係性、キャラ配置、勢力図",
    "theme": "作品のテーマ、主題、葛藤、問題意識、メッセージ性",
    "mood": "雰囲気、読後感、感情、トーン、切なさやコメディやシリアスなどの作風",
    "reader": "どんな読者やどんな気分のときに向くか、刺さる層、読みたいシチュエーション",
    "review": "読者のレビューや評価、感想、好評な点や不満点、口コミの傾向",
}

# 動的重みのパラメータ
_WEIGHT_TEMP = 0.10      # softmax 温度 (小さいほどクエリに合う facet へ重みが集中)
_WEIGHT_UNIFORM_MIX = 0.35  # 一様重みとのブレンド率 (全 facet が最低限寄与するように)

# 類似検索の線形+二乗 混合係数
_SIM_ALPHA = 1.0   # 線形項
_SIM_BETA = 1.0    # 二乗項

_CACHE_TTL_SEC = 1800
_lock = threading.Lock()
_ids: list[str] = []
_mats: dict[str, np.ndarray] = {}        # facet -> (N,1536) 正規化済
_genre_sets: list[set[str]] = []
_genre_only: list[list[str]] = []
_genre_df: dict[str, int] = {}
_facet_desc_vecs: dict[str, np.ndarray] | None = None  # facet -> 正規化済 説明文ベクトル
_loaded_at: float = 0.0

_openai = OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None


def _parse_vec(emb):
    if emb is None:
        return None
    if isinstance(emb, str):
        try:
            return [float(x) for x in emb.strip("[]").split(",") if x.strip()]
        except ValueError:
            return None
    return emb


def _normalize_rows(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


import os
import json

# ローカルキャッシュ (DB から毎回 300MB 転送するのを避ける)
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "cache", "vector_cache")
_NPZ_PATH = os.path.join(_CACHE_DIR, "facets.npz")
_META_PATH = os.path.join(_CACHE_DIR, "meta.json")
_DISK_TTL_SEC = 24 * 3600  # ディスクキャッシュの有効期限 (新データ取込のため)


def _save_cache():
    os.makedirs(_CACHE_DIR, exist_ok=True)
    np.savez(_NPZ_PATH, **{f: _mats[f] for f in FACETS})
    meta = {
        "ids": _ids,
        "genre_only": _genre_only,
        "genre_df": _genre_df,
        "built_at": time.time(),
    }
    with open(_META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)


def _try_load_cache() -> bool:
    """ディスクキャッシュから読み込む。成功で True。"""
    global _ids, _mats, _genre_sets, _genre_only, _genre_df, _loaded_at
    if not (os.path.exists(_NPZ_PATH) and os.path.exists(_META_PATH)):
        return False
    try:
        with open(_META_PATH, encoding="utf-8") as f:
            meta = json.load(f)
        if time.time() - meta.get("built_at", 0) > _DISK_TTL_SEC:
            return False
        npz = np.load(_NPZ_PATH)
        _mats = {f: npz[f] for f in FACETS}
        _ids = meta["ids"]
        _genre_only = meta["genre_only"]
        _genre_df = meta["genre_df"]
        _genre_sets = [set(g) for g in _genre_only]  # 検索の絞り込みは genre_tags ベースで十分
        _loaded_at = time.time()
        return True
    except Exception:
        return False


def _fetch_from_db() -> None:
    global _ids, _mats, _genre_sets, _genre_only, _genre_df, _loaded_at
    sb = get_supabase()
    select_cols = "id, genre_tags, sub_genre_tags, " + ", ".join(_FACET_COL[f] for f in FACETS)
    ids: list[str] = []
    raw: dict[str, list] = {f: [] for f in FACETS}
    gsets, gonly = [], []
    df: dict[str, int] = {}
    # 1行に 6ベクトル (≈60KB) 乗るため、ページは小さめにしないと転送がタイムアウトする
    offset, page = 0, 150
    while True:
        def _fetch():
            return sb.table("openbd_test_series").select(select_cols) \
                .not_.is_("plot_embedding", "null") \
                .range(offset, offset + page - 1).execute()
        rs = None
        for attempt in range(4):
            try:
                rs = _fetch()
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(1.5 * (attempt + 1))
        rows = rs.data or []
        for r in rows:
            vecs = {f: _parse_vec(r.get(_FACET_COL[f])) for f in FACETS}
            if any(v is None or len(v) != 1536 for v in vecs.values()):
                continue
            ids.append(r["id"])
            for f in FACETS:
                raw[f].append(vecs[f])
            genre = r.get("genre_tags") or []
            sub = r.get("sub_genre_tags") or []
            gsets.append(set(genre) | set(sub))
            gonly.append(genre)
            for t in genre:
                df[t] = df.get(t, 0) + 1
        if len(rows) < page:
            break
        offset += page
    _ids = ids
    _mats = {f: (_normalize_rows(np.asarray(raw[f], dtype=np.float32))
                 if raw[f] else np.zeros((0, 1536), dtype=np.float32))
             for f in FACETS}
    _genre_sets = gsets
    _genre_only = gonly
    _genre_df = df
    _loaded_at = time.time()


def _load(force: bool = False) -> None:
    """6 facet の埋め込み行列 + ジャンル索引をロード。
    ディスクキャッシュ (.npz) があればそこから即ロード。無ければ DB から取得して保存。"""
    global _ids, _mats, _genre_sets, _genre_only, _genre_df, _loaded_at
    with _lock:
        if not force and _mats and (time.time() - _loaded_at) < _CACHE_TTL_SEC:
            return
        if not force and _try_load_cache():
            return
        _fetch_from_db()
        try:
            _save_cache()
        except Exception:
            pass


def rebuild_cache() -> int:
    """DB から強制再取得してキャッシュを作り直す。戻り値: 件数。"""
    _load(force=True)
    return len(_ids)


def _ensure_facet_descriptors() -> dict[str, np.ndarray]:
    """facet 説明文を embedding (初回のみ)。クエリの動的重み計算に使う。"""
    global _facet_desc_vecs
    if _facet_desc_vecs is not None:
        return _facet_desc_vecs
    if not _openai:
        return {}
    out = {}
    for f in FACETS:
        v = np.asarray(_embed(_FACET_DESCRIPTOR[f]), dtype=np.float32)
        n = np.linalg.norm(v)
        out[f] = v / n if n else v
    _facet_desc_vecs = out
    return out


def _embed(text: str) -> list[float]:
    return _openai.embeddings.create(model=settings.openai_embedding_model, input=text).data[0].embedding


def ready() -> bool:
    _load()
    return bool(_mats) and _mats[FACETS[0]].shape[0] > 0


def _normed_query(text: str) -> np.ndarray | None:
    if not _openai:
        return None
    v = np.asarray(_embed(text), dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n else v


def _facet_weights_for_query(qvec: np.ndarray) -> dict[str, float]:
    """クエリ内容に応じた facet 重み。クエリと各 facet 説明文の近さを softmax。"""
    descs = _ensure_facet_descriptors()
    if not descs:
        return {f: 1.0 / len(FACETS) for f in FACETS}
    sims = np.array([float(qvec @ descs[f]) for f in FACETS])
    # softmax(温度付き)
    z = sims / _WEIGHT_TEMP
    z -= z.max()
    e = np.exp(z)
    soft = e / e.sum()
    uniform = np.ones(len(FACETS)) / len(FACETS)
    mixed = _WEIGHT_UNIFORM_MIX * uniform + (1 - _WEIGHT_UNIFORM_MIX) * soft
    return {f: float(w) for f, w in zip(FACETS, mixed)}


def _all_genre_tags_at(i: int) -> set[str]:
    return _genre_sets[i]


def _index_of(series_id: str) -> int | None:
    _load()
    try:
        return _ids.index(series_id)
    except ValueError:
        return None


def _genre_mask(genres: list[str] | None, required: set[str] | None) -> np.ndarray | None:
    """ジャンル絞り込み用の bool マスク (None なら絞り込みなし)。"""
    if not genres and not required:
        return None
    gset = set(genres) if genres else None
    mask = np.zeros(len(_ids), dtype=bool)
    for i in range(len(_ids)):
        tags = _genre_sets[i]
        if gset is not None and not (gset & tags):
            continue
        if required is not None and not (required & tags):
            continue
        mask[i] = True
    return mask


def _topk_from_scores(scores: np.ndarray, limit: int, exclude: set[str],
                      mask: np.ndarray | None) -> list[tuple[str, float]]:
    order = np.argsort(-scores)
    out = []
    for idx in order:
        if mask is not None and not mask[idx]:
            continue
        sid = _ids[idx]
        if sid in exclude:
            continue
        out.append((sid, float(scores[idx])))
        if len(out) >= limit:
            break
    return out


# ============================ semantic ============================
def semantic(query: str, limit: int = 12,
             genres: list[str] | None = None) -> list[tuple[str, float]]:
    """自然言語クエリ → 各 facet に当て、クエリ内容で重みを変えて合成。"""
    _load()
    if not _mats or _mats[FACETS[0]].shape[0] == 0:
        return []
    q = _normed_query(query)
    if q is None:
        return []
    weights = _facet_weights_for_query(q)
    scores = np.zeros(len(_ids), dtype=np.float32)
    for f in FACETS:
        scores += weights[f] * (_mats[f] @ q)  # facet 別 cosine の加重和
    mask = _genre_mask(genres, None)
    return _topk_from_scores(scores, limit, set(), mask)


# ============================ similar ============================
def _percentile_rank(sims: np.ndarray) -> np.ndarray:
    """各要素の percentile 順位 (0〜1) に正規化。facet 間のスケール差を吸収。"""
    order = sims.argsort()
    ranks = np.empty(len(sims), dtype=np.float32)
    ranks[order] = np.arange(len(sims), dtype=np.float32)
    denom = max(len(sims) - 1, 1)
    return ranks / denom


def similar(series_id: str, limit: int = 12,
            auto_genre: bool = True) -> list[tuple[str, float]]:
    """基準作品との facet 別類似を percentile 正規化 → 線形+二乗 混合で合成。"""
    i = _index_of(series_id)
    if i is None:
        return []
    scores = np.zeros(len(_ids), dtype=np.float32)
    for f in FACETS:
        sims = _mats[f] @ _mats[f][i]          # 基準との cosine
        p = _percentile_rank(sims)             # facet 内 percentile 正規化
        scores += _SIM_ALPHA * p + _SIM_BETA * (p ** 2)   # 線形+二乗 混合
    # 対象の最希少ジャンルで自動絞り込み (百合作品→百合作品)
    required = None
    if auto_genre:
        tags = _genre_only[i]
        if tags:
            rarest = min(tags, key=lambda t: _genre_df.get(t, 0))
            required = {rarest}
    mask = _genre_mask(None, required)
    return _topk_from_scores(scores, limit, {series_id}, mask)


# ============================ recommend ============================
def recommend(series_ids: list[str], limit: int = 12) -> list[tuple[str, float]]:
    """複数作品の重心に近い作品 (facet 別 percentile 平均の合成)。"""
    _load()
    idxs = [_index_of(s) for s in series_ids]
    idxs = [i for i in idxs if i is not None]
    if not idxs:
        return []
    scores = np.zeros(len(_ids), dtype=np.float32)
    for f in FACETS:
        # 各 source との cosine を平均してから percentile
        acc = np.zeros(len(_ids), dtype=np.float32)
        for i in idxs:
            acc += _mats[f] @ _mats[f][i]
        acc /= len(idxs)
        p = _percentile_rank(acc)
        scores += _SIM_ALPHA * p + _SIM_BETA * (p ** 2)
    mask = None
    return _topk_from_scores(scores, limit, set(series_ids), mask)
