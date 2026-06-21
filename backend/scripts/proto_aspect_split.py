"""
[プロトタイプ] 作品を6観点に分けて Grok で structured 生成し、各観点を個別 embedding する実験。

材料として タグ / 7軸スコア / 書籍あらすじ / 既存 review_summary を渡す。
review_summary には web 検索済みのレビュー情報が入っているので、ほとんどの作品は
追加検索なしで6観点を埋められる (= 安い)。

検索方針:
  - 基本は素材のみで記述 (検索しない)。
  - popularity_score が高い (人気作) のに素材が乏しく観点を埋められない場合のみ、
    web_search / x_search を「合わせて最大1回」だけ使う。
  - max_turns=2 で「0回 (即回答) or 1回検索→回答」に制限。

6観点:
  plot   : あらすじ・設定・舞台・ギミック
  cast   : 主人公像・キャラ配置・関係性・勢力図
  theme  : テーマ・葛藤・問題意識
  mood   : 雰囲気・読後感・感情曲線
  reader : どんな読者/気分に向くか
  review : 実際のレビュー傾向・評価/不満点

使い方:
  python -m scripts.proto_aspect_split --series-ids <UUID> [<UUID> ...]
"""
import sys
import os
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydantic import BaseModel, Field
from openai import OpenAI
from xai_sdk import Client as GrokClient
from xai_sdk.chat import system, user
from xai_sdk.tools import web_search, x_search

from app.config import settings
from app.services.supabase_client import get_supabase

GROK_MODEL = "grok-4.3"
MAX_TURNS = 2  # 0回(即回答) or 1回検索→回答 に制限

_grok = GrokClient(api_key=settings.xai_api_key)
_openai = OpenAI(api_key=settings.openai_api_key)

DEFAULT_TARGETS = [
    "6607fb36-57a4-4cf7-bae2-bcbe76e645b1",  # 安達としまむら (有名)
    "30ddfe03-529c-4457-a188-d34731583b4e",  # 嫌い同士のわたしたちは一夜の過ちで (中堅)
    "7892d529-a634-4789-af80-cba370082fc2",  # 白と黒の封魔士 (無名)
]


class AspectSplit(BaseModel):
    """作品を6観点に分割した記述 (各 150〜300字程度、ベクトル検索向けに密度高く)"""
    plot: str = Field(description="あらすじ・設定・舞台・ギミック")
    cast: str = Field(description="主人公像・キャラ配置・関係性・勢力図")
    theme: str = Field(description="テーマ・葛藤・問題意識")
    mood: str = Field(description="雰囲気・読後感・感情曲線")
    reader: str = Field(description="どんな読者/どんな気分に向くか")
    review: str = Field(description="実際のレビュー傾向・評価されている点/不満点")


SYSTEM_INSTRUCTIONS = """あなたはライトノベル/小説を分析し、ベクトル検索用に観点別へ構造化する専門家です。
与えられた素材 (タグ・雰囲気スコア・あらすじ・既存のレビュー要約) を基に、作品を6つの観点に
分けて日本語で記述してください。各観点は 150〜300字程度、類似作品をベクトル検索で見つけやすいよう
具体的かつ密度高く書きます。

観点:
- plot   : あらすじ・設定・舞台・ギミック (何がどう起きる話か)
- cast   : 主人公像・キャラ配置・関係性・勢力図
- theme  : テーマ・葛藤・問題意識
- mood   : 雰囲気・読後感・感情曲線
- reader : どんな読者/どんな気分に向くか
- review : 実際のレビュー傾向・評価されている点や不満点

重要な制約:
- 雰囲気スコアや人気度などの数値は「判断材料」として参照するだけにし、本文に数値を
  そのまま書かないでください。例えば「コメディ53・切ない68」のようなスコアの羅列や
  「人気度83で〜」といった数値の言及は禁止です。スコアから読み取れる作風 (例: 切なさが
  強め、コメディは控えめ 等) を自然な言葉で表現してください。
- 原則として Web 検索はしないでください。与えられた素材だけで6観点を記述します。
- ただし、人気度(popularity)が高い作品なのに素材が乏しく観点を埋められない場合に限り、
  web_search か x_search を「合わせて最大1回だけ」使って補ってよいです。
- 素材から分かる範囲で書き、推測で固有名詞や展開を創作しないでください。
- 前置きや見出しは書かず、各フィールドに本文だけを入れてください。"""


def _fetch_material(supabase, sid: str) -> dict | None:
    rs = supabase.table("openbd_test_series").select(
        "id, series_title, author, atmosphere_tags, genre_tags, sub_genre_tags, "
        "comedy_score, serious_score, heartwarming_score, bittersweet_score, "
        "romantic_score, uplifting_score, popularity_score, momentum_score, review_summary"
    ).eq("id", sid).execute()
    if not rs.data:
        return None
    s = rs.data[0]
    # 書籍あらすじ (重複除外)
    bks = supabase.table("openbd_test_books").select("description").eq("series_id", sid) \
        .order("volume_number").execute().data or []
    seen, descs = set(), []
    for b in bks:
        d = (b.get("description") or "").strip()
        if d and d[:120] not in seen:
            seen.add(d[:120])
            descs.append(d)
    s["_descriptions"] = "\n".join(descs)[:3000]
    return s


def _score_band(v: int | None) -> str | None:
    """数値スコアを定性表現に変換 (本文への数値漏れ防止)。"""
    if v is None:
        return None
    if v < 25:
        return "ほぼなし"
    if v < 45:
        return "やや弱め"
    if v < 65:
        return "中程度"
    if v < 80:
        return "強め"
    return "とても強い"


def _build_material_text(s: dict) -> str:
    # スコアは定性表現で渡す (数値を見せないことで本文への書き写しを防ぐ)
    scores = []
    for label, key in [("コメディ","comedy_score"),("シリアス","serious_score"),
                       ("ほのぼの","heartwarming_score"),("切なさ","bittersweet_score"),
                       ("胸キュン","romantic_score"),("高揚感","uplifting_score")]:
        band = _score_band(s.get(key))
        if band:
            scores.append(f"{label}={band}")
    pop_band = _score_band(s.get("popularity_score"))
    mom_band = _score_band(s.get("momentum_score"))
    lines = [
        f"作品名: {s.get('series_title')}",
        f"著者: {s.get('author')}",
        f"ジャンル: {', '.join(s.get('genre_tags') or [])}",
        f"サブジャンル: {', '.join(s.get('sub_genre_tags') or [])}",
        f"雰囲気タグ: {', '.join(s.get('atmosphere_tags') or [])}",
        f"作風の傾向(参考): {' / '.join(scores)}",
        f"知名度(参考): 累計={pop_band or '不明'} / 直近の話題度={mom_band or '不明'}",
    ]
    if s.get("_descriptions"):
        lines.append(f"あらすじ(書誌):\n{s['_descriptions']}")
    if s.get("review_summary"):
        lines.append(f"既存レビュー要約:\n{s['review_summary']}")
    return "\n".join(lines)


def split_aspects(material_text: str) -> tuple[AspectSplit | None, float | None, bool]:
    """Grok で6観点に分割。戻り値: (結果, コスト, 検索したか)"""
    chat = _grok.chat.create(
        model=GROK_MODEL,
        tools=[web_search(), x_search()],
        max_turns=MAX_TURNS,
    )
    chat.append(system(SYSTEM_INSTRUCTIONS))
    chat.append(user(material_text))
    resp, parsed = chat.parse(AspectSplit)
    # 検索したか (server_side_tools_used を見る)
    searched = False
    try:
        searched = bool(getattr(resp.usage, "server_side_tools_used", None))
    except Exception:
        pass
    cost = getattr(resp, "cost_usd", None)
    return parsed, cost, searched


def main(args):
    supabase = get_supabase()
    targets = args.series_ids or DEFAULT_TARGETS
    aspects = ["plot", "cast", "theme", "mood", "reader", "review"]

    for sid in targets:
        s = _fetch_material(supabase, sid)
        if not s:
            print(f"[{sid[:8]}] 見つからず")
            continue
        print("=" * 70)
        print(f"■ {s.get('series_title')}  (pop={s.get('popularity_score')})")
        print("=" * 70)
        mat = _build_material_text(s)
        try:
            result, cost, searched = split_aspects(mat)
        except Exception as e:
            print(f"  ! 失敗: {e}")
            continue
        if not result:
            print("  ! parsed 結果が空")
            continue
        print(f"  [検索: {'あり' if searched else 'なし'}  コスト: ${cost or 0:.4f}]\n")
        for a in aspects:
            text = getattr(result, a)
            print(f"--- {a} ({len(text)}字) ---")
            print(text)
            # embedding 確認
            emb = _openai.embeddings.create(model=settings.openai_embedding_model, input=text)
            print(f"  → embedding 次元 {len(emb.data[0].embedding)}")
            print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="[proto] 6観点分割 + 個別embedding")
    parser.add_argument("--series-ids", nargs="+", metavar="UUID")
    args = parser.parse_args()
    main(args)
