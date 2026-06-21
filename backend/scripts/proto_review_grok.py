"""
[プロトタイプ] xAI Grok API (Live Search: Web + X) で作品のレビュー・感想・ツイートを
積極収集し、物語性の詳細要約を生成、その要約を OpenAI で embedding する実験スクリプト。

OpenAI 版 (proto_review_websearch.py) との比較用。Grok は X(Twitter) の投稿を
リアルタイム検索できるため、ラノベ読者の生の声を拾える可能性がある。

Grok API は OpenAI 互換 (base_url=https://api.x.ai/v1)。Live Search は
search_parameters で指定する (chat.completions の extra_body 経由)。

embedding は OpenAI text-embedding-3-small を使う (Grok に embedding API が無いため、
ベクトル空間を既存のあらすじ embedding と揃える)。

使い方:
  python -m scripts.proto_review_grok
  python -m scripts.proto_review_grok --title "安達としまむら"
"""
import sys
import os
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai import OpenAI
from app.config import settings

# Grok クライアント (OpenAI SDK の base_url 差し替え)
_grok = OpenAI(api_key=settings.xai_api_key, base_url="https://api.x.ai/v1")
# embedding は OpenAI で取る (ベクトル空間を既存 embedding と揃える)
_openai = OpenAI(api_key=settings.openai_api_key)

GROK_MODEL = "grok-4.3"

DEFAULT_TARGETS = [
    "嫌い同士のわたしたちは一夜の過ちで",
    "安達としまむら",
]


def build_summary(title: str) -> tuple[str, list[str]]:
    """Grok Live Search (Web + X) でレビュー/感想を収集し、物語性の詳細要約を生成。
    戻り値: (要約テキスト, 引用ソース URL 一覧)
    """
    prompt = f"""ライトノベル/小説『{title}』について、Web とX(旧Twitter)から読者の感想・
レビュー・ブログ記事・ツイートを積極的に収集してください。

それらの収集結果のみを根拠に、この作品が「どのような物語か」を、ベクトル検索で類似作品を
見つけやすくする目的で、詳細かつ多角的に日本語で記述してください。

含めてほしい観点 (見出しは付けず、前置きや後置きも書かず、本文のみを 600字以上で詳細に):
1. 物語の核となる設定・あらすじ
2. ジャンル・題材 (百合 / 学園 / 異世界 / 恋愛 など)
3. 全体の雰囲気・読後感 (切ない / ほのぼの / 緊張感 / 爽やか など)
4. テーマ・主題
5. 読者が繰り返し言及している魅力・特徴
6. どんな読者・どんな気分のときに刺さるか

制約:
- 検索で得た情報のみで書き、推測や創作で物語の事実 (人物名・展開) を補わない。
- 収集できた感想・レビューの要素はできるだけ多く盛り込み、600字以上の情報量を確保する。
- ただし冗長な美辞麗句や繰り返しは避け、検索で得た事実・読者の声を密度高く記述する。
- 情報が極端に乏しい場合のみ無理に膨らませず、分かった範囲で書く。
- 「以下は〜」のような前置きは書かず、本文だけを出力する。"""

    # Live Search は廃止 → Agent Tools API (Responses + web_search/x_search) を使う
    resp = _grok.responses.create(
        model=GROK_MODEL,
        input=[{"role": "user", "content": prompt}],
        tools=[
            {"type": "web_search"},
            {"type": "x_search"},
        ],
    )

    text = (resp.output_text or "").strip()

    # citations は response.citations、または annotations 経由で取得
    citations: list[str] = []
    raw = getattr(resp, "citations", None)
    if isinstance(raw, list):
        citations = [c for c in raw if isinstance(c, str)]
    if not citations:
        for item in getattr(resp, "output", []) or []:
            if getattr(item, "type", None) == "message":
                for content in getattr(item, "content", []) or []:
                    for ann in getattr(content, "annotations", []) or []:
                        url = getattr(ann, "url", None)
                        if url:
                            citations.append(url)
    return text, citations


def main(args):
    if not settings.xai_api_key:
        print("[エラー] XAI_API_KEY が .env に未設定です")
        print("  → https://console.x.ai で API キーを発行し .env に XAI_API_KEY=xai-... を追加してください")
        sys.exit(1)
    if not settings.openai_api_key:
        print("[エラー] OPENAI_API_KEY 未設定 (embedding 用)")
        sys.exit(1)

    targets = [args.title] if args.title else DEFAULT_TARGETS

    for title in targets:
        print("=" * 70)
        print(f"■ 作品: {title}  (Grok {GROK_MODEL})")
        print("=" * 70)
        try:
            summary, citations = build_summary(title)
        except Exception as e:
            print(f"  ! 失敗: {e}")
            continue

        print(f"\n--- 生成された詳細要約 ({len(summary)}字) ---")
        print(summary)

        print(f"\n--- 引用ソース ({len(citations)}件) ---")
        for c in citations:
            print(f"  - {c}")

        if summary:
            emb = _openai.embeddings.create(
                model=settings.openai_embedding_model,
                input=summary,
            )
            vec = emb.data[0].embedding
            print(f"\n--- embedding (OpenAI) ---")
            print(f"  次元={len(vec)}  消費 tokens={emb.usage.total_tokens}")
        else:
            print("\n--- embedding ---")
            print("  要約が空のためスキップ")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="[proto] Grok Live Search 収集→要約→embedding")
    parser.add_argument("--title", type=str, help="単一作品で試す")
    args = parser.parse_args()
    main(args)
