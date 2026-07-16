"""
facet 重み生成モデルのファインチューニング用データセットを作る (蒸留)。

手順:
  1. 多様な検索クエリを N 件生成 (gpt-5.4)
  2. 各クエリに 6観点 (plot/cast/theme/mood/reader/review) の重み (合計1) を
     gpt-5.4 で付与 → これを教師ラベルとする。OpenAI Batch API で一括処理。
  3. OpenAI ファインチューニング用 JSONL を出力 (system は推論時と同じものを使う)

facet 重みは「クエリがどの観点を重視しているか」を表す。唯一の客観正解は無いため、
強モデル (gpt-5.4) の判断を実用上のラベルとする。

使い方:
  python -m scripts.build_weight_ft_dataset gen-queries --n 100
  python -m scripts.build_weight_ft_dataset submit          # 重み付与を Batch 投入
  python -m scripts.build_weight_ft_dataset fetch           # 結果回収 → FT JSONL 生成
"""
import sys
import os
import json
import time
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai import OpenAI
from app.config import settings

LABEL_MODEL = "gpt-5.4"
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BASE, "cache", "ft")
QUERIES_PATH = os.path.join(OUT_DIR, "queries.json")
BATCH_INPUT_PATH = os.path.join(OUT_DIR, "label_batch_input.jsonl")
BATCH_STATE_PATH = os.path.join(OUT_DIR, "label_batch_state.json")
FT_JSONL_PATH = os.path.join(OUT_DIR, "facet_weights_ft.jsonl")

FACETS = ["plot", "cast", "theme", "mood", "reader", "review"]

# 推論時にもそのまま使う system プロンプト (FT データの system と一致させる)
SYSTEM_PROMPT = """あなたはライトノベル推薦システムの検索クエリ解析器です。
ユーザーの検索クエリが、作品のどの観点を重視しているかを分析し、6観点の重みを返します。

観点:
- plot   : あらすじ・設定・舞台・世界観・ギミック
- cast   : 主人公像・キャラクター・関係性・キャラ配置
- theme  : テーマ・葛藤・問題意識・メッセージ性
- mood   : 雰囲気・読後感・感情・トーン (切ない/コメディ/シリアス等)
- reader : どんな読者・どんな気分のときに向くか
- review : レビュー傾向・評価・口コミ

出力は JSON のみ。各観点 0〜1 の数値で、合計が 1 になるよう正規化すること。
クエリが特定の観点を強く示すならそこへ重みを集中させ、漠然としていれば分散させる。
形式: {"plot":0.0,"cast":0.0,"theme":0.0,"mood":0.0,"reader":0.0,"review":0.0}"""

# ラベル生成専用 (推論時とは別)。実データの観点別マッチ強度とヒット作のジャンルを参考に渡し、
# 語の表面的出現に引っ張られず「検索意図」に重みを置くよう促す。
LABEL_SYSTEM = SYSTEM_PROMPT + """

【ラベル付けの追加指示】
参考として、このクエリで実際に各観点のベクトル空間にどれだけ強く合致したか (観点別マッチ強度)
と、上位ヒット作のジャンル傾向を与えます。これはデータ上の手がかりですが、観点テキストに
その語が多く出現するだけで強度が上がる (例: ジャンル語が reader/review 説明に頻出) ことがあります。
データを踏まえつつ、最終的には「ユーザーがその語で何を探しているか (検索意図)」を最優先して
重みを決めてください。"""

_client = OpenAI(api_key=settings.openai_api_key)


# ---------- 1. クエリ生成 ----------
def gen_queries(n: int):
    os.makedirs(OUT_DIR, exist_ok=True)
    prompt = f"""ライトノベル推薦アプリで実際に入力されそうな自然言語の検索クエリを{n}件、
日本語で多様に生成してください。以下がバランス良く混ざるように:
- あらすじ/設定重視 (例: 「迷宮を攻略していくダンジョン冒険もの」)
- キャラ/関係性重視 (例: 「ツンデレ幼馴染とのラブコメ」)
- テーマ重視 (例: 「挫折から立ち直る成長物語」)
- 雰囲気/読後感重視 (例: 「泣けて切ない読後感の作品」)
- 読者/気分重視 (例: 「疲れた夜に癒されたい時に読むもの」)
- ジャンル語のみ (例: 「百合」「異世界転生」)
- 複合 (例: 「主人公最強で爽快なバトルファンタジー」)
長さも単語1つ〜1文まで散らす。重複なし。JSON 配列の文字列のみ返す。"""
    resp = _client.chat.completions.create(
        model=LABEL_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content)
    # {"queries":[...]} か [...] のどちらでも拾う
    queries = data.get("queries") if isinstance(data, dict) else data
    queries = [q.strip() for q in queries if isinstance(q, str) and q.strip()][:n]
    json.dump(queries, open(QUERIES_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[クエリ生成] {len(queries)}件 → {QUERIES_PATH}")
    for q in queries[:8]:
        print(f"  - {q}")


# ---------- 2. Batch 投入 (グラウンディング付き) ----------
def _grounding_for(query: str) -> str:
    """クエリの観点別マッチ強度 + 上位ヒット作のジャンルを参考テキスト化。"""
    import numpy as np
    from app.services import vector_search as vs
    vs.ready()
    qv = vs._normed_query(query)
    if qv is None:
        return ""
    sig = {}
    overall = np.zeros(len(vs._ids), dtype="float32")
    for fct in FACETS:
        sims = vs._mats[fct] @ qv
        sig[fct] = float(np.sort(sims)[::-1][:20].mean())
        overall += sims
    # 観点別マッチ強度 (0-100 正規化表示)
    mx = max(sig.values()) or 1.0
    strength = " / ".join(f"{fct}={sig[fct]/mx*100:.0f}" for fct in FACETS)
    # 上位ヒット作のジャンル傾向
    top_idx = np.argsort(-overall)[:6]
    genres = []
    for idx in top_idx:
        g = vs._genre_only[idx][:3]
        if g:
            genres.append("/".join(g))
    return (f"観点別マッチ強度(最大100): {strength}\n"
            f"上位ヒット作のジャンル: {'; '.join(genres)}")


def submit():
    queries = json.load(open(QUERIES_PATH, encoding="utf-8"))
    with open(BATCH_INPUT_PATH, "w", encoding="utf-8") as f:
        for i, q in enumerate(queries):
            grounding = _grounding_for(q)
            user_msg = q if not grounding else f"{q}\n\n[参考データ]\n{grounding}"
            req = {
                "custom_id": f"q_{i:04d}",
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": {
                    "model": LABEL_MODEL,
                    "messages": [
                        {"role": "system", "content": LABEL_SYSTEM},
                        {"role": "user", "content": user_msg},
                    ],
                    "response_format": {"type": "json_object"},
                },
            }
            f.write(json.dumps(req, ensure_ascii=False) + "\n")
            if (i + 1) % 20 == 0:
                print(f"  grounding {i+1}/{len(queries)}")
    up = _client.files.create(file=open(BATCH_INPUT_PATH, "rb"), purpose="batch")
    batch = _client.batches.create(
        input_file_id=up.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
    )
    json.dump({"batch_id": batch.id, "n": len(queries)},
              open(BATCH_STATE_PATH, "w"), ensure_ascii=False)
    print(f"[Batch 投入] {len(queries)}件 batch_id={batch.id}")
    print(f"  → fetch で回収")


# ---------- 3. 回収 → FT JSONL ----------
def _normalize(w: dict) -> dict:
    vals = {f: max(0.0, float(w.get(f, 0) or 0)) for f in FACETS}
    s = sum(vals.values())
    if s <= 0:
        return {f: round(1 / len(FACETS), 4) for f in FACETS}
    return {f: round(v / s, 4) for f, v in vals.items()}


def fetch():
    state = json.load(open(BATCH_STATE_PATH))
    bid = state["batch_id"]
    while True:
        b = _client.batches.retrieve(bid)
        print(f"  status={b.status} ({b.request_counts.completed}/{b.request_counts.total})")
        if b.status in ("completed", "failed", "expired", "cancelled"):
            break
        time.sleep(20)
    if b.status != "completed":
        print(f"[エラー] batch status={b.status}")
        return
    out = _client.files.content(b.output_file_id).text
    queries = json.load(open(QUERIES_PATH, encoding="utf-8"))
    by_id = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        cid = r["custom_id"]
        body = r["response"]["body"]
        content = body["choices"][0]["message"]["content"]
        by_id[cid] = _normalize(json.loads(content))

    n_ok = 0
    with open(FT_JSONL_PATH, "w", encoding="utf-8") as f:
        for i, q in enumerate(queries):
            w = by_id.get(f"q_{i:04d}")
            if not w:
                continue
            rec = {"messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": q},
                {"role": "assistant", "content": json.dumps(w, ensure_ascii=False)},
            ]}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n_ok += 1
    print(f"[FT データ生成] {n_ok}件 → {FT_JSONL_PATH}")
    # サンプル表示
    for i in range(min(5, len(queries))):
        w = by_id.get(f"q_{i:04d}")
        if w:
            top = sorted(w.items(), key=lambda x: -x[1])[:3]
            print(f"  {queries[i]!r} → {top}")


FT_STATE_PATH = os.path.join(OUT_DIR, "ft_job.json")


# ---------- 4. ファインチューニング ----------
def _validate_jsonl():
    rows = [json.loads(l) for l in open(FT_JSONL_PATH, encoding="utf-8") if l.strip()]
    bad = 0
    for r in rows:
        msgs = r.get("messages", [])
        if [m["role"] for m in msgs] != ["system", "user", "assistant"]:
            bad += 1
            continue
        try:
            w = json.loads(msgs[2]["content"])
            assert abs(sum(w.values()) - 1.0) < 0.05 and set(w) == set(FACETS)
        except Exception:
            bad += 1
    print(f"[検証] {len(rows)}件 / 不正 {bad}件")
    return len(rows), bad


def train(base_model: str):
    n, bad = _validate_jsonl()
    if bad:
        print("不正データあり。中止。")
        return
    up = _client.files.create(file=open(FT_JSONL_PATH, "rb"), purpose="fine-tune")
    job = _client.fine_tuning.jobs.create(training_file=up.id, model=base_model)
    json.dump({"job_id": job.id, "base_model": base_model},
              open(FT_STATE_PATH, "w"), ensure_ascii=False)
    print(f"[FT ジョブ作成] job_id={job.id} base={base_model} status={job.status}")
    print("  → status で進捗確認")


def ft_status():
    st = json.load(open(FT_STATE_PATH))
    job = _client.fine_tuning.jobs.retrieve(st["job_id"])
    print(f"  status={job.status}  model={job.fine_tuned_model}")
    if job.status == "failed":
        print(f"  error={job.error}")
    return job


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen-queries"); g.add_argument("--n", type=int, default=100)
    sub.add_parser("submit")
    sub.add_parser("fetch")
    t = sub.add_parser("train"); t.add_argument("--model", default="gpt-4.1-mini-2025-04-14")
    sub.add_parser("status")
    a = p.parse_args()
    if a.cmd == "gen-queries":
        gen_queries(a.n)
    elif a.cmd == "submit":
        submit()
    elif a.cmd == "train":
        train(a.model)
    elif a.cmd == "status":
        ft_status()
    elif a.cmd == "fetch":
        fetch()
