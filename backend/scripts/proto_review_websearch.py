"""
[プロトタイプ] OpenAI Responses API (web_search) で作品のレビュー・ブログ感想を
積極的に収集し、それを材料に「どんな物語か」を多角的に表現した詳細要約を生成、
その要約を embedding する実験スクリプト。

方針:
  - レビュー1件1ベクトルではなく、作品 = 1詳細要約 = 1ベクトル (series テーブルと相性が良い)
  - web_search で読者の声を積極収集 → それを根拠に物語性を立体的に記述させる
  - ハルシネーション抑制のため「検索で得た情報のみで書く / 不明は不明と書く」を明示

目的: 生成される要約の品質を目視確認し、本番化の判断材料を得る使い捨て。

使い方:
  python -m scripts.proto_review_websearch
  python -m scripts.proto_review_websearch --title "安達としまむら"
"""
import sys
import os
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai import OpenAI
from app.config import settings

_client = OpenAI(api_key=settings.openai_api_key)

# web_search 対応モデル (Responses API 経由)
SEARCH_MODEL = "gpt-4.1"

DEFAULT_TARGETS = [
    "嫌い同士のわたしたちは一夜の過ちで",
    "安達としまむら",
]


def build_summary(title: str) -> tuple[str, list[dict]]:
    """web_search でレビュー・ブログ感想を積極収集し、物語性の詳細要約を生成する。
    戻り値: (要約テキスト, 引用ソース一覧)
    """
    prompt = f"""ライトノベル/小説『{title}』について、Web 検索で以下を積極的に収集してください。
- Amazon / 読書メーター / ブクログ / カクヨム / 個人ブログ / SNS / レビューサイト の読者の感想・評価
- あらすじ紹介、作品紹介記事

それらの収集結果**のみ**を根拠に、この作品が「どのような物語か」を、ベクトル検索で
類似作品を見つけやすくする目的で、詳細かつ多角的に日本語で記述してください。

記述に含めてほしい観点 (見出しは付けず、前置きや後置きも書かず、本文のみを 600字以上で詳細に):
1. 物語の核となる設定・あらすじ (誰が何をする話か)
2. ジャンル・題材 (例: 百合 / 学園 / 異世界 / 恋愛 など)
3. 全体の雰囲気・読後感 (例: 切ない / ほのぼの / 緊張感 / 爽やか)
4. テーマ・主題 (例: 挫折と再起 / 思春期の距離感 / 創作への葛藤)
5. 読者が繰り返し言及している魅力や特徴 (文章力 / 心理描写 / キャラの可愛さ など)
6. どんな読者・どんな気分のときに刺さるか

重要な制約:
- 検索で得られた情報のみで書き、推測や創作で物語の事実 (登場人物名・展開) を補わないでください。
- 収集できた感想・レビューの要素はできるだけ多く盛り込み、600字以上の情報量を確保してください。
- ただし冗長な美辞麗句や繰り返しは避け、検索で得た事実・読者の声を密度高く記述してください。
- 情報が極端に乏しい場合のみ無理に膨らませず、分かった範囲で書いてください。
- 「以下は〜」のような前置きは書かず、本文だけを出力してください。"""

    resp = _client.responses.create(
        model=SEARCH_MODEL,
        tools=[{"type": "web_search"}],
        input=prompt,
    )

    text = (resp.output_text or "").strip()

    sources: list[dict] = []
    for item in resp.output:
        if getattr(item, "type", None) == "message":
            for content in getattr(item, "content", []) or []:
                for ann in getattr(content, "annotations", []) or []:
                    if getattr(ann, "type", None) == "url_citation":
                        sources.append({
                            "url": getattr(ann, "url", None),
                            "title": getattr(ann, "title", None),
                        })
    return text, sources


def main(args):
    if not settings.openai_api_key:
        print("[エラー] OPENAI_API_KEY 未設定")
        sys.exit(1)

    targets = [args.title] if args.title else DEFAULT_TARGETS

    for title in targets:
        print("=" * 70)
        print(f"■ 作品: {title}")
        print("=" * 70)
        try:
            summary, sources = build_summary(title)
        except Exception as e:
            print(f"  ! 失敗: {e}")
            continue

        print(f"\n--- 生成された詳細要約 ({len(summary)}字) ---")
        print(summary)

        uniq_urls = sorted({s.get("url") for s in sources if s.get("url")})
        print(f"\n--- 参照ソース ({len(uniq_urls)} ユニーク URL) ---")
        for u in uniq_urls:
            print(f"  - {u}")

        # 要約を 1ベクトルに embedding
        if summary:
            emb = _client.embeddings.create(
                model=settings.openai_embedding_model,
                input=summary,
            )
            vec = emb.data[0].embedding
            print(f"\n--- embedding ---")
            print(f"  次元={len(vec)}  消費 tokens={emb.usage.total_tokens}")
        else:
            print("\n--- embedding ---")
            print("  要約が空のため embedding スキップ")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="[proto] web_search 収集→物語性要約→embedding")
    parser.add_argument("--title", type=str, help="単一作品で試す")
    args = parser.parse_args()
    main(args)
