"""
gpt-5.5-pro に 300件の実データ (各作品の6観点テキスト) を読ませ、
(1) 6観点の精密な定義 と (2) 検索クエリを6観点のサブクエリに分割する推論プロンプト
を生成させる。出力は cache/ft/facet_split_spec.md に保存。
"""
import sys, os, json
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from openai import OpenAI
from app.config import settings

_c = OpenAI(api_key=settings.openai_api_key)
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(BASE, "cache", "ft", "facet_samples_300.json")
OUT = os.path.join(BASE, "cache", "ft", "facet_split_spec.md")

data = json.load(open(SAMPLES, encoding="utf-8"))
# gpt-5.5 (非pro) は TPM が緩いので 300件全件を渡す
lines = []
for r in data:
    lines.append(f"■{r['t']} [{'/'.join(r['g'])}]")
    for k in ["plot", "cast", "theme", "mood", "reader", "review"]:
        lines.append(f"  {k}: {(r[k] or '')[:90]}")
corpus = "\n".join(lines)

prompt = f"""あなたはベクトル検索システムの設計者です。あるライトノベル推薦DBでは、各作品が
6つの観点テキスト (plot/cast/theme/mood/reader/review) に分割され、各々が埋め込みベクトル化
されています。下に実データ300件の6観点テキストを示します。これを精読し、各観点が実際に
「何を書いているか」を把握してください。

その上で、次の2つを作成してください:

# 1. 6観点の定義
各観点 (plot/cast/theme/mood/reader/review) が実データ上で実際にカバーしている範囲・
含む要素・含まない要素を、データに即して簡潔かつ厳密に定義する。観点同士の境界
(どちらに入るか紛らわしいもの) も明記する。

# 2. クエリ分割プロンプト
ユーザーの自然言語検索クエリを入力として受け取り、それを6観点それぞれの「サブクエリ文」
に分割して JSON 出力させるための、推論モデル用システムプロンプトを作成する。要件:
- クエリが触れていない観点は null にする (無理に埋めない)
- 各サブクエリは、その観点の埋め込み (上記データと同じ粒度・文体) と照合しやすい短い記述にする
- 例: 「主人公最強で無双する爽快バトル」→ cast:「圧倒的に強い主人公」, mood:「爽快感のある
  バトル」, plot:「戦闘・バトル中心の展開」, theme/reader/review: null
- 出力フォーマット: {{"plot":..,"cast":..,"theme":..,"mood":..,"reader":..,"review":..}}
そのままコピーして使える完成したプロンプト文を提示すること。

=== 実データ300件 ===
{corpus}
"""

print(f"[gpt-5.5-pro 生成中... 入力{len(prompt):,}字 (reasoning のため数分)]")
import time
text = None
for attempt in range(6):
    try:
        resp = _c.responses.create(model="gpt-5.5", input=prompt)
        text = resp.output_text
        break
    except Exception as e:
        if "429" in str(e) or "rate" in str(e).lower():
            wait = 30 * (attempt + 1)
            print(f"  レート制限 → {wait}s 待機 (試行 {attempt+1}/6)")
            time.sleep(wait)
        else:
            raise
if text is None:
    print("リトライ上限。中止。")
    sys.exit(1)
open(OUT, "w", encoding="utf-8").write(text)
print(f"[完了] → {OUT}  ({len(text)}字)")
print("=" * 60)
print(text[:1500])
