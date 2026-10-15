"""
今の検索 (3072次元・全件 cosine。vector_search.py と同じ計算) と、
新しい RPC (1536次元・HNSW。migration 034) の結果を比べる。DB は読むだけ。

比べるもの (受け入れ基準は ranobeNext/docs/requirements.md §8-4):
  自然言語検索: 評価用クエリ30本の上位20件の一致率 (平均 70% 以上)
  似ているシリーズ: ランダムな50作品の上位12件の一致率 (平均 60% 以上)

公平に比べるため:
  - 基準側は backups/aspect_3072_* (DB と照合済み) の 3072 ベクトルで計算する
  - クエリの分割 (gpt-5.5) は1回だけ行い、両方で同じ分割結果を使う
  - クエリの埋め込みは 3072 で1回だけ取り、新方式にはそれを切り詰めて正規化したものを渡す
    (dimensions=1536 で取り直すのと同じになることは migrate_aspect_1536 --check-truncation で確認済み)

OpenAI の使用量: 分割 30回 + 埋め込み 30回前後。

使い方:
  python -m scripts.compare_search_parity
  python -m scripts.compare_search_parity --similar 100 --skip-semantic
"""
import sys
import os
import json
import time
import random
import argparse
import datetime as dt
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from openai import OpenAI

from app.config import settings
from app.services.supabase_client import get_supabase
from app.services import query_splitter
from app.services.vector_search import _FACET_DESCRIPTOR, _WEIGHT_TEMP, _WEIGHT_UNIFORM_MIX
from scripts.backup_aspect_3072 import ASPECTS, TABLE, BACKUP_ROOT, _retry
from scripts.migrate_aspect_1536 import _latest_backup

NEW_DIM = 1536
QUERIES = [
    "疲れた時に読む、料理が美味しそうな異世界スローライフ",
    "主人公最強で無双する爽快バトル",
    "ヤンデレヒロインとの重い恋愛が読みたい",
    "百合の学園もので怪事件を解決するダークなミステリー",
    "泣ける青春恋愛もの",
    "女の子同士の恋愛",
    "悪役令嬢に転生して破滅を回避する話",
    "VRMMOで成り上がる",
    "ダンジョンで生き残るサバイバル",
    "陰キャ男子と陽キャギャルのラブコメ",
    "伏線回収がすごいと評判のSF",
    "ほのぼのした日常系で癒されたい",
    "復讐を描くダークファンタジー",
    "戦記もの、国同士の戦争と政治",
    "幼なじみとの甘々な恋愛",
    "ホラーでじわじわ怖い話",
    "異世界で内政チートして領地経営",
    "探偵が推理する本格ミステリー",
    "音楽やバンドに打ち込む青春",
    "兄妹のラブコメ",
    "ゲームの世界に閉じ込められるデスゲーム",
    "おっさん冒険者が活躍する話",
    "切ない読後感の短めの恋愛",
    "魔法学園で落ちこぼれが成り上がる",
    "グルメ、飯テロ",
    "ロボットやメカが出てくるSFバトル",
    "歴史もので戦国時代",
    "お仕事もの、会社員が主人公",
    "ハーレムで笑えるコメディ",
    "家族の絆を描いた感動作",
]


def _load_backup(backup_dir: str):
    with open(os.path.join(backup_dir, "ids.json"), encoding="utf-8") as f:
        ids = json.load(f)
    mats = {}
    for a in ASPECTS:
        m = np.load(os.path.join(backup_dir, f"{a}.npy")).astype(np.float32)
        n = np.linalg.norm(m, axis=1, keepdims=True)
        n[n == 0] = 1.0
        mats[a] = m / n
    return ids, mats


def _load_genres(sb, ids: list[str]):
    rows, off = {}, 0
    while True:
        part = _retry(lambda o=off: sb.table(TABLE).select("id,genre_tags,sub_genre_tags")
                      .not_.is_("plot_embedding_1536", "null").order("id").range(o, o + 999).execute(),
                      "fetch genres").data or []
        for r in part:
            rows[r["id"]] = r
        if len(part) < 1000:
            break
        off += 1000
    gsets = [set(rows[i].get("genre_tags") or []) | set(rows[i].get("sub_genre_tags") or []) for i in ids]
    gonly = [rows[i].get("genre_tags") or [] for i in ids]
    df: dict[str, int] = {}
    for g in gonly:
        for t in g:
            df[t] = df.get(t, 0) + 1
    return gsets, gonly, df


def _trunc(v: np.ndarray) -> list[float]:
    t = v[:NEW_DIM]
    return (t / np.linalg.norm(t)).astype(np.float32).tolist()


def _norm(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def _mask(gsets, genres, mode):
    if not genres:
        return None
    g = set(genres)
    return np.array([(g <= s) if mode == "and" else bool(g & s) for s in gsets])


def _top(ids, scores, k, mask=None, exclude=None):
    out = []
    for i in np.argsort(-scores):
        if mask is not None and not mask[i]:
            continue
        if exclude is not None and i == exclude:
            continue
        out.append(ids[i])
        if len(out) >= k:
            break
    return out


def semantic(sb, client, ids, mats, gsets, k=20):
    print(f"[自然言語検索] {len(QUERIES)}本 × 上位{k}件")
    desc_vecs = None
    results, overlaps, lat = [], [], []
    for q in QUERIES:
        split = query_splitter.split(q)
        params = {"p_limit": k}
        if split:
            active = [(f, t) for f, t in split["facets"].items() if t]
            embs = client.embeddings.create(model=settings.openai_aspect_embedding_model,
                                            input=[t for _, t in active]).data
            scores = np.zeros(len(ids), dtype=np.float32)
            for (f, _), e in zip(active, embs):
                v = _norm(e.embedding)
                scores += mats[f] @ v
                params[f"q_{f}"] = _trunc(v)
            genres, mode = split.get("genres") or [], split.get("genre_mode") or "or"
            how = "分割"
        else:
            # 分割に失敗したとき: 説明文との近さで観点に重みを付ける (今の _scores_by_weight と同じ)
            if desc_vecs is None:
                d = client.embeddings.create(model=settings.openai_aspect_embedding_model,
                                             input=[_FACET_DESCRIPTOR[f] for f in ASPECTS]).data
                desc_vecs = {f: _norm(x.embedding) for f, x in zip(ASPECTS, d)}
            qv = _norm(client.embeddings.create(model=settings.openai_aspect_embedding_model,
                                                input=q).data[0].embedding)

            def weights(qvec, descs):
                sims = np.array([float(qvec @ descs[f]) for f in ASPECTS]) / _WEIGHT_TEMP
                e = np.exp(sims - sims.max())
                mixed = _WEIGHT_UNIFORM_MIX / len(ASPECTS) + (1 - _WEIGHT_UNIFORM_MIX) * e / e.sum()
                return dict(zip(ASPECTS, mixed))

            w_old = weights(qv, desc_vecs)
            # 新方式では説明文も 1536 で持つことになるので、重みも 1536 の空間で計算する
            q16 = np.asarray(_trunc(qv))
            w_new = weights(q16, {f: np.asarray(_trunc(v)) for f, v in desc_vecs.items()})
            scores = sum(w_old[f] * (mats[f] @ qv) for f in ASPECTS)
            for f in ASPECTS:
                params[f"q_{f}"] = q16.tolist()
                params[f"w_{f}"] = float(w_new[f])
            genres, mode = [], "or"
            how = "重み"
        base = _top(ids, scores, k, _mask(gsets, genres, mode))
        params.update({"p_genres": genres or None, "p_genre_mode": mode})
        t0 = time.time()
        new = [r["series_id"] for r in _retry(lambda: sb.rpc("search_semantic", params).execute(),
                                              "search_semantic").data or []]
        lat.append(time.time() - t0)
        ov = len(set(base) & set(new)) / max(1, len(base))
        overlaps.append(ov)
        results.append({"query": q, "mode": how, "genres": genres, "genre_mode": mode,
                        "baseline": base, "new": new, "overlap": ov})
        print(f"  {ov:5.0%}  ({how}{' ' + '/'.join(genres) + '(' + mode + ')' if genres else ''})  {q}")
    return results, overlaps, lat


def similar(sb, ids, mats, gsets, gonly, df, n, k=12):
    print(f"[似ているシリーズ] {n}作品 × 上位{k}件")
    random.seed(20261015)
    picks = random.sample(range(len(ids)), n)
    results, overlaps, lat = [], [], []
    for i in picks:
        scores = np.zeros(len(ids), dtype=np.float32)
        for f in ASPECTS:
            sims = mats[f] @ mats[f][i]
            order = sims.argsort()
            ranks = np.empty(len(sims), dtype=np.float32)
            ranks[order] = np.arange(len(sims), dtype=np.float32)
            p = ranks / max(len(sims) - 1, 1)
            scores += p + p ** 2
        mask = None
        if gonly[i]:
            rarest = min(gonly[i], key=lambda t: df.get(t, 0))
            mask = np.array([rarest in s for s in gsets])
        base = _top(ids, scores, k, mask, exclude=i)
        t0 = time.time()
        new = [r["series_id"] for r in _retry(
            lambda sid=ids[i]: sb.rpc("similar_series", {"p_series_id": sid, "p_limit": k}).execute(),
            "similar_series").data or []]
        lat.append(time.time() - t0)
        ov = len(set(base) & set(new)) / max(1, len(base))
        overlaps.append(ov)
        results.append({"series_id": ids[i], "baseline": base, "new": new, "overlap": ov})
    print(f"  一致率: 平均 {np.mean(overlaps):.0%} / 最小 {np.min(overlaps):.0%} / 中央値 {np.median(overlaps):.0%}")
    return results, overlaps, lat


def _lat(name, xs):
    if xs:
        print(f"  {name} の応答時間 (REST 往復込み): 中央値 {np.median(xs) * 1000:.0f}ms / p95 {np.percentile(xs, 95) * 1000:.0f}ms")


def main():
    ap = argparse.ArgumentParser(description="今の検索と新しい RPC の一致率を比べる (読み取り専用)")
    ap.add_argument("--backup-dir", help="基準に使うバックアップ (既定: 最新)")
    ap.add_argument("--similar", type=int, default=50, help="似ているシリーズで比べる作品数 (既定 50)")
    ap.add_argument("--skip-semantic", action="store_true", help="自然言語検索を比べない (OpenAI を使わない)")
    args = ap.parse_args()

    backup_dir = args.backup_dir or _latest_backup()
    print(f"[基準データ] {backup_dir}")
    ids, mats = _load_backup(backup_dir)
    sb = get_supabase()
    gsets, gonly, df = _load_genres(sb, ids)
    out = {"run_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "backup_dir": backup_dir}

    if not args.skip_semantic:
        client = OpenAI(api_key=settings.openai_api_key)
        res, ov, lat = semantic(sb, client, ids, mats, gsets)
        print(f"  一致率: 平均 {np.mean(ov):.0%} / 最小 {np.min(ov):.0%}  (基準 70% 以上)")
        _lat("search_semantic", lat)
        out["semantic"] = {"mean": float(np.mean(ov)), "results": res}
    res, ov, lat = similar(sb, ids, mats, gsets, gonly, df, args.similar)
    print("  (基準 60% 以上)")
    _lat("similar_series", lat)
    out["similar"] = {"mean": float(np.mean(ov)), "results": res}

    path = os.path.join(os.path.dirname(BACKUP_ROOT), "cache",
                        "parity_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S") + ".json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[保存] {path}")


if __name__ == "__main__":
    main()
