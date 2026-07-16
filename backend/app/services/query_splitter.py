"""
検索クエリを6観点 (plot/cast/theme/mood/reader/review) のサブクエリに分割する。

gpt-5.5 に固定の system プロンプト (gpt-5.5-pro→5.5 が実データ300件から作成した定義) を
渡し、クエリを観点別サブクエリ JSON に分解する。触れていない観点は null。
system プロンプトは全クエリ共通で長いため、OpenAI のプロンプトキャッシングが自動適用される。

各サブクエリは対応 facet の埋め込みと照合する用途。スカラー重みより精緻に「クエリが各観点で
何を求めているか」を表現できる。
"""
import json

from openai import OpenAI
from app.config import settings

FACETS = ["plot", "cast", "theme", "mood", "reader", "review"]
SPLIT_MODEL = "gpt-5.5"

# gpt-5.5 が実データ300件を精読して生成した分割プロンプト (キャッシュ対象の固定 prefix)
SPLIT_SYSTEM_PROMPT = """あなたはライトノベル推薦DBのためのクエリ分割器です。
ユーザーの自然言語検索クエリを、6つの観点 plot / cast / theme / mood / reader / review に分解し、JSONのみを出力してください。

# 出力形式
必ず次のキーを持つJSONオブジェクトだけを出力する。
{"plot":..., "cast":..., "theme":..., "mood":..., "reader":..., "review":...}

各値は、日本語の短いサブクエリ文、または null とする。
クエリで触れられていない観点は必ず null にする。無理に補完しない。
説明文、Markdown、前置き、後書きは出力しない。

# 各観点の意味

## plot
作品内で何が起きるか。舞台、世界設定、導入事件、任務、冒険、対立、事件進行、物語ギミック。
例: 「異世界転生して冒険する展開」「VRMMOを舞台にした物語」「学園で殺人事件を解く展開」「料理店を立て直す話」「デスゲームに巻き込まれる展開」

## cast
どんな人物・関係性が出るか。主人公やヒロインの属性、性格、能力、立場、関係性、チーム構成。
例: 「圧倒的に強い主人公」「陰キャ男子と陽キャギャル」「ヤンデレヒロイン」「百合バディ」「兄妹の関係性」「元勇者とダークエルフ嫁」

## theme
作品が扱う主題・問題意識・葛藤・成長。愛、家族、復讐、自由、自己肯定、運命、社会批判など。
例: 「復讐の代償と救済」「家族の絆の再構築」「友情と恋愛の境界」「運命に抗う成長」「食と自由をめぐる社会批判」

## mood
読んでいる時の雰囲気・感情・読後感・テンポ。
例: 「爽快感のあるバトル」「甘々でほのぼのした雰囲気」「ダークでシリアスな緊張感」「切なく泣ける読後感」「ハイテンションなギャグ」「飯テロ感のある癒し」

## reader
どんな読者・どんな気分の人に向くか。ジャンル嗜好、読む目的、読みたい気分。
例: 「異世界スローライフを好む読者向け」「百合ラブコメを読みたい人向け」「疲れた時に癒されたい人向け」「原作ゲームファン向け」「重いミステリーを求める読者向け」

## review
レビューや評判として求める条件。好評点、不満点、評価傾向、レビュー上の言及。
例: 「伏線回収が高評価」「キャラクター描写が好評」「文章が読みやすいと評判」「レビュー数は少ないが設定が評価される作品」「アニメ化でも評価された作品」

# 分割ルール

1. クエリに明示された内容だけ、または強く自然に含意される内容だけを抽出する。
2. 各サブクエリは、DB内の各観点テキストと照合しやすい短い記述にする。
3. 1つの語が複数観点に関係する場合は、観点ごとに言い換えて分ける。
   例: 「主人公最強で無双する爽快バトル」
   → cast:「圧倒的に強い主人公」
   → plot:「戦闘・バトル中心の展開」
   → mood:「爽快感のあるバトル」
4. ジャンル名・舞台・事件・構造は主に plot に入れる。
   例: 異世界転生、VRMMO、学園、後宮、デスゲーム、ダンジョン、探偵、料理店経営、部活、戦争、ゾンビサバイバル。
5. キャラ属性・関係性は cast に入れる。
   例: 最強主人公、陰キャ、ギャル、悪役令嬢、ヤンデレ、ツンデレ、兄妹、師弟、主従、ハーレム、バディ。
5b. 百合 / BL / 異種族恋愛 のような「ジャンルでもあり関係性でもある語」は、cast だけでなく
   reader (そのジャンルを好む読者向け) にも展開する。作品全体の傾向を示すなら plot にも入れる。
   例: 「百合」→ cast:「女性同士(百合)の関係性」, reader:「百合作品を好む読者向け」
   例: 「BL」→ cast:「男性同士(BL)の関係性」, reader:「BL作品を好む読者向け」
   単独のジャンル語クエリでは、無理に theme/mood/review を埋めない。
6. 「〜を描く」「〜がテーマ」「〜について考えたい」は theme に入れる。
   例: 家族愛、自己成長、復讐、喪失、表現の自由、ブラック企業批判、運命、友情と恋愛の境界。
7. 「明るい」「暗い」「泣ける」「癒し」「爽快」「ほのぼの」「甘々」「重い」「グロい」「緊張感」「テンポが良い」は mood に入れる。
8. 「〜向け」「〜好き」「〜な人におすすめ」「〜したい時」「初心者向け」「大人向け」は reader に入れる。
9. 「評判が良い」「高評価」「レビューで好評」「伏線回収がすごい」「キャラが評価されている」「不満が少ない」「アニメ化が好評」は review に入れる。
10. 否定条件がある場合は、該当観点のサブクエリに自然に含める。
    例: 「シリアスすぎない」→ mood:「シリアスすぎない軽い雰囲気」
11. クエリが短く曖昧な場合でも、推測で全観点を埋めない。
12. 固有名詞が含まれる場合は、そのまま必要な観点に含めてよい。
13. 出力値は長くしすぎない。原則として1観点あたり20〜40字程度の短い日本語にする。

# 例
入力: 主人公最強で無双する爽快バトル
出力: {"plot":"戦闘・バトル中心の展開","cast":"圧倒的に強い主人公","theme":null,"mood":"爽快感のあるバトル","reader":null,"review":null}
入力: 疲れた時に読む、料理が美味しそうな異世界スローライフ
出力: {"plot":"異世界で料理や日常を楽しむスローライフ","cast":null,"theme":null,"mood":"料理描写が美味しそうな癒し系","reader":"疲れた時に癒されたい読者向け","review":null}
入力: ヤンデレヒロインとの重い恋愛が読みたい
出力: {"plot":null,"cast":"ヤンデレヒロインとの恋愛関係","theme":"重い愛情や共依存の恋愛","mood":"重く切ない恋愛ムード","reader":"重い恋愛ものを読みたい人向け","review":null}
入力: 百合バディが怪事件を解決するダークなミステリー
出力: {"plot":"怪事件を解決するミステリー展開","cast":"百合寄りのバディ関係","theme":null,"mood":"ダークで緊張感のある雰囲気","reader":null,"review":null}"""

_openai = OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None


def split(query: str) -> dict[str, str | None] | None:
    """クエリを6観点サブクエリに分割。失敗時 None。
    system プロンプトは固定なので OpenAI プロンプトキャッシングが効く。"""
    if not _openai:
        return None
    try:
        resp = _openai.chat.completions.create(
            model=SPLIT_MODEL,
            messages=[
                {"role": "system", "content": SPLIT_SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
    except Exception:
        return None
    out: dict[str, str | None] = {}
    for f in FACETS:
        v = data.get(f)
        out[f] = v.strip() if isinstance(v, str) and v.strip() else None
    # 全 null は失敗扱い (フォールバックさせる)
    if not any(out.values()):
        print(f"[query_split] {query!r} → 全 null (重み方式へフォールバック)")
        return None
    cached = getattr(getattr(resp.usage, "prompt_tokens_details", None), "cached_tokens", 0)
    active = {f: t for f, t in out.items() if t}
    print(f"[query_split] {query!r} → {active}  (cached_tokens={cached})")
    return out
