"""
sub_genre タグを Gemini で整理する一括スクリプト。

実行すること:
  1. openbd_test_series から sub_genre_tags の実利用数を集計
  2. tags.is_ai_generated を含めた全 sub_genre タグを取得
  3. Gemini に渡し、各タグについて (subcategory, canonical_name) を生成
     - subcategory: relationship / character / setting / plot / tone / format / other
     - canonical_name: 重複なら統合先のタグ名、ユニークなら自分の名前
  4. dry-run: マージ・削除・subcategory 付与計画を表示
  5. --apply: openbd_test_series.sub_genre_tags の置換 + tags 行の削除/更新を実行

オプション:
  --dry-run            (デフォルト) 計画のみ表示
  --apply              実際に DB を更新する
  --model NAME         Gemini モデル (デフォルト Pro)
  --batch-size N       1 リクエストあたりのタグ数 (デフォルト 80)
  --delete-threshold N actual_usage < N の AI タグを削除 (デフォルト 5)
  --plan-path PATH     Gemini の出力を保存/再利用するパス (デフォルト
                       cache/sub_genre_plan.json)

通常運用:
  ./venv/bin/python -m scripts.cleanup_sub_genre_tags --dry-run
  ./venv/bin/python -m scripts.cleanup_sub_genre_tags --apply
"""
import sys
import os
import json
import time
import argparse
import re
from collections import Counter, defaultdict
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase
from app.services.gemini import _generate_with_retry
from app.config import settings


SUBCATEGORIES = {
    "relationship": "関係性 — 主要人物同士の関係 (バディ・三角関係・身分差恋愛・師弟・先輩後輩・幼なじみ・婚約者・契約結婚 等)",
    "character":    "キャラ — 主要人物の性格や特徴 (吸血鬼・メイド・お嬢様・ギャル・王女・王子・貴族・聖女・エルフ・魔女・神様・悪魔・幽霊・ケモミミ・最強主人公・陰キャ・引きこもり・天才・無自覚 等)",
    "profession":   "職業 — 主要人物の職業 (騎士・冒険者・傭兵・軍師・暗殺者・聖職者・商人・教師・医者・探偵・魔法使い・召喚士・剣士・盗賊・忍者 等)",
    "setting":      "世界観 — その物語がどのような世界か (異世界・終末世界・近未来・中華風・ダンジョン・後宮・宮廷・裏社会・現代ファンタジー・ダークファンタジー・ミリタリー・学園 等)",
    "plot":         "ストーリー — ストーリーの重要な要素 (復讐・スピンオフ・前日譚・成長物語・救出劇・逃亡劇・群像劇・サスペンス・デスゲーム・記憶喪失・正体隠匿・やり直し・身代わり 等)",
    "format":       "形式 — 作品の形式 (短編集・ノベライズ・TRPGリプレイ・単巻完結 等)",
}

# cleanup_sub_genre_tags プロンプトでも「特徴として挙げられる場合のみ」を強調する
SUBCATEGORY_GLOBAL_POLICY = (
    "サブジャンルタグは「作品の特徴として挙げられる場合のみ」分類するため、"
    "端役や微細な要素のタグも、その作品で『特徴になる』レベルかを意識して subcategory を判定すること。"
)


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


def compute_actual_usage(supabase) -> Counter:
    """openbd_test_series.sub_genre_tags を全件走査して name → 使用シリーズ数 を返す。"""
    print("[1] openbd_test_series 全件走査して実利用数を集計")
    rows = _paginate(
        lambda: supabase.table("openbd_test_series").select("id, sub_genre_tags")
    )
    counter: Counter = Counter()
    for r in rows:
        for name in (r.get("sub_genre_tags") or []):
            name = (name or "").strip()
            if name:
                counter[name] += 1
    print(f"  → シリーズ {len(rows):,} 件 / 出現タグ {len(counter):,} 種")
    return counter


def fetch_sub_genre_tags(supabase) -> list[dict]:
    print("[2] tags マスタから sub_genre タグを取得")
    rows = _paginate(
        lambda: supabase.table("tags").select(
            "id, name, description, is_ai_generated, usage_count"
        ).eq("tag_type", "sub_genre")
    )
    print(f"  → {len(rows):,} タグ")
    return rows


# ============== Gemini 呼び出し ==============

def _build_prompt(batch_names: list[str], all_names: list[str],
                  tag_descriptions: dict[str, str]) -> str:
    """1 バッチ分の分類プロンプトを作る。"""
    subcat_lines = "\n".join(
        f"  - {key}: {desc}" for key, desc in SUBCATEGORIES.items()
    )
    # 参照リスト (全タグ) — canonical_name は必ずこの中から選ぶ
    all_names_str = "、".join(all_names)
    # 分類対象 (バッチ)
    batch_lines = []
    for name in batch_names:
        desc = tag_descriptions.get(name) or ""
        if desc:
            batch_lines.append(f"  - {name}: {desc}")
        else:
            batch_lines.append(f"  - {name}")
    batch_str = "\n".join(batch_lines)

    return f"""
あなたはライトノベルのサブジャンルタグを整理する編集者です。
以下の sub_genre タグを 6 種のサブカテゴリに分類し、重複している同義タグを「正規化された 1 つの代表タグ」にまとめてください。

【共通原則】
{SUBCATEGORY_GLOBAL_POLICY}

【サブカテゴリ定義】
{subcat_lines}

【全 sub_genre タグ一覧 (canonical_name はこの一覧から必ず 1 つ選ぶ)】
{all_names_str}

【分類対象 (このバッチ)】
{batch_str}

【ルール】
- 各タグについて以下を判定し、JSON で返す:
  1. subcategory: 上記 6 種のキーから 1 つ (relationship / character / profession / setting / plot / format)
  2. canonical_name: 同義タグが他にある場合はそちらを「全 sub_genre タグ一覧」から選ぶ。
     ユニーク (同義なし) なら自分自身を返す。
     表記揺れ (例: 「異能バトル」「能力バトル」「異能力バトル」) は最も使われている / 一般的な 1 つに統合。
     微妙にニュアンスが違うものは別タグとして残す (例: 「身分差」と「身分差恋愛」は別)。
- character と profession の区別: 生来の身分・種族・性質 (王女・吸血鬼・お嬢様) は character、
  職務・役割として就いているもの (騎士・冒険者・教師・医者) は profession。
  両方の側面がある場合は作品で**中心になっている方**を選ぶ。
- canonical_name は必ず上の「全 sub_genre タグ一覧」に含まれる文字列にする。
  新しい名前は作らないこと。

【出力フォーマット (JSON のみ。前後の説明文・コードフェンス禁止)】
{{
  "items": [
    {{"name": "対象タグ名", "subcategory": "relationship", "canonical_name": "代表タグ名"}},
    ...
  ]
}}

対象タグ {len(batch_names)} 件すべてについて items に含めてください。
"""


def classify_with_gemini(tags: list[dict], all_names: list[str],
                         model: str, batch_size: int) -> dict[str, dict]:
    """各タグ name → {"subcategory": ..., "canonical_name": ...} を返す。"""
    tag_descriptions = {t["name"]: (t.get("description") or "") for t in tags}
    names = [t["name"] for t in tags]
    result: dict[str, dict] = {}

    # 利用数の多い順に並べると canonical が「メジャー側」になりやすい
    # (Gemini に渡す batch_names の順序は気にしないが、参照リスト all_names も同じ並び)
    for i in range(0, len(names), batch_size):
        batch = names[i:i + batch_size]
        prompt = _build_prompt(batch, all_names, tag_descriptions)
        print(f"  [Gemini] batch {i // batch_size + 1}/"
              f"{(len(names) + batch_size - 1) // batch_size} "
              f"({len(batch)} タグ) 送信...")
        text = _generate_with_retry(prompt, model=model)
        if not text:
            print(f"    ! 空応答")
            continue
        # JSON 抽出
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if not m:
            print(f"    ! JSON 抽出失敗 (応答先頭 200 文字): {text[:200]}")
            continue
        try:
            data = json.loads(m.group())
        except json.JSONDecodeError as e:
            print(f"    ! JSON parse 失敗: {e}")
            continue
        items = data.get("items") or []
        ok = 0
        for it in items:
            name = (it.get("name") or "").strip()
            sub = (it.get("subcategory") or "").strip()
            canon = (it.get("canonical_name") or "").strip()
            if not name or name not in tag_descriptions:
                continue
            if sub not in SUBCATEGORIES:
                sub = "other"
            if not canon or canon not in tag_descriptions:
                # canonical が参照リストに無い → 自分自身にフォールバック
                canon = name
            result[name] = {"subcategory": sub, "canonical_name": canon}
            ok += 1
        missing = set(batch) - set(result.keys())
        print(f"    → 取得 {ok} 件 / 抜け {len(missing)} 件")
        time.sleep(1)
    return result


# ============== 計画立案・適用 ==============

def build_plan(tags: list[dict], actual_usage: Counter,
               classifications: dict[str, dict],
               delete_threshold: int) -> dict:
    """Gemini 結果と実利用数から最終計画を組み立てる。
    返り値: {
      "merges":      [(from_name, to_name, count_to_move)],
      "subcategory": {name: subcat},
      "deletions":   [name, ...],   # AI タグ かつ マージ後実利用<threshold
      "unclassified":[name, ...],
    }
    """
    name_to_tag = {t["name"]: t for t in tags}
    classify_keys = set(classifications.keys())
    unclassified = [t["name"] for t in tags if t["name"] not in classify_keys]

    # マージ計画 (from → to, ただし to が自分自身ならスキップ)
    # 連鎖防止: A→B, B→C なら A→C にする
    canonical_map: dict[str, str] = {}
    for name, info in classifications.items():
        canon = info["canonical_name"]
        if canon == name:
            continue
        canonical_map[name] = canon

    # 連鎖を解決
    def resolve(name: str, visited=None) -> str:
        visited = visited or set()
        if name in visited:
            return name  # 循環
        visited.add(name)
        nxt = canonical_map.get(name)
        return resolve(nxt, visited) if nxt and nxt != name else name

    final_map = {n: resolve(n) for n in canonical_map.keys()}
    merges = []
    # マージ後利用数 (実利用) を再集計
    post_usage: Counter = Counter()
    for name in name_to_tag.keys():
        target = final_map.get(name, name)
        post_usage[target] += actual_usage.get(name, 0)

    for src, dst in final_map.items():
        if src == dst:
            continue
        merges.append((src, dst, actual_usage.get(src, 0)))

    # subcategory 計画 (マージ先 = 残るタグだけ対象)
    survivors = set(name_to_tag.keys()) - set(final_map.keys())
    subcategory_assign: dict[str, str] = {}
    for name in survivors:
        info = classifications.get(name)
        if info and info["subcategory"] in SUBCATEGORIES:
            subcategory_assign[name] = info["subcategory"]

    # 削除候補: マージで消えるもの + 公式ではないかつ post_usage<threshold
    deletions_merged = [src for src, dst, _cnt in merges]
    deletions_low = []
    for name in survivors:
        t = name_to_tag[name]
        if t.get("is_ai_generated") and post_usage.get(name, 0) < delete_threshold:
            deletions_low.append(name)

    return {
        "merges": merges,
        "subcategory_assign": subcategory_assign,
        "deletions_merged": deletions_merged,
        "deletions_low": deletions_low,
        "unclassified": unclassified,
        "post_usage": post_usage,
        "final_map": final_map,
    }


def print_plan(plan: dict, tags: list[dict]):
    name_to_tag = {t["name"]: t for t in tags}
    merges = plan["merges"]
    subcat = plan["subcategory_assign"]
    del_m = plan["deletions_merged"]
    del_l = plan["deletions_low"]
    unc = plan["unclassified"]

    print("\n========== 計画 ==========")
    print(f"\n■ マージ (重複統合): {len(merges)} 件")
    for src, dst, cnt in sorted(merges, key=lambda x: -x[2])[:50]:
        print(f"    {src:<20} → {dst:<20} (移動 {cnt} シリーズ)")
    if len(merges) > 50:
        print(f"    ... (他 {len(merges) - 50} 件)")

    print(f"\n■ subcategory 付与: {len(subcat)} 件")
    by_sub: dict[str, list[str]] = defaultdict(list)
    for n, s in subcat.items():
        by_sub[s].append(n)
    for s in SUBCATEGORIES.keys():
        names = by_sub.get(s, [])
        sample = "、".join(names[:8])
        more = f" ... (他 {len(names) - 8})" if len(names) > 8 else ""
        print(f"    [{s}] {len(names)} 件: {sample}{more}")

    print(f"\n■ 削除 (マージ統合済み): {len(del_m)} 件 (重複)")
    print(f"\n■ 削除 (AI タグ かつ マージ後利用 < threshold): {len(del_l)} 件")
    sample = ", ".join(sorted(del_l)[:30])
    if del_l:
        print(f"    例: {sample}{'...' if len(del_l) > 30 else ''}")

    if unc:
        print(f"\n■ 未分類 (Gemini 応答に含まれず): {len(unc)} 件")
        print(f"    例: {', '.join(unc[:20])}")

    print(f"\n■ importance: 残る全 sub_genre タグに 1 を設定")
    print("==========================\n")


def apply_plan(supabase, tags: list[dict], plan: dict):
    """計画を実際に DB に反映する。"""
    name_to_tag = {t["name"]: t for t in tags}
    final_map: dict[str, str] = plan["final_map"]
    subcat: dict[str, str] = plan["subcategory_assign"]
    del_m: list[str] = plan["deletions_merged"]
    del_l: list[str] = plan["deletions_low"]

    # --- (a) openbd_test_series.sub_genre_tags の置換 ---
    if final_map:
        print("[apply A] openbd_test_series.sub_genre_tags の置換")
        rows = _paginate(
            lambda: supabase.table("openbd_test_series").select("id, sub_genre_tags")
        )
        updated = 0
        for r in rows:
            tags_arr = r.get("sub_genre_tags") or []
            if not tags_arr:
                continue
            new_arr_set: list[str] = []
            seen = set()
            changed = False
            for n in tags_arr:
                mapped = final_map.get(n, n)
                if mapped != n:
                    changed = True
                if mapped not in seen:
                    seen.add(mapped)
                    new_arr_set.append(mapped)
                else:
                    # 同じ正規化結果が複数 (例: A → C, B → C) なら 1 つに
                    changed = True
            if changed:
                try:
                    supabase.table("openbd_test_series").update(
                        {"sub_genre_tags": new_arr_set}
                    ).eq("id", r["id"]).execute()
                    updated += 1
                except Exception as e:
                    print(f"  ! 更新失敗 id={r['id'][:8]}: {e}")
        print(f"  → {updated} シリーズの sub_genre_tags を更新")

    # --- (b) 削除: マージ統合済み + 低利用 AI タグ ---
    to_delete = set(del_m) | set(del_l)
    # ただし、削除対象が他のタグの canonical 先になっている場合は残す
    canon_targets = set(final_map.values())
    to_delete = {n for n in to_delete if n not in canon_targets}
    if to_delete:
        print(f"[apply B] tags 削除: {len(to_delete)} 件")
        ids = [name_to_tag[n]["id"] for n in to_delete if n in name_to_tag]
        # in_ で一括削除 (1000 件ずつ)
        for i in range(0, len(ids), 1000):
            chunk = ids[i:i + 1000]
            try:
                supabase.table("tags").delete().in_("id", chunk).execute()
            except Exception as e:
                print(f"  ! 削除失敗 chunk {i}: {e}")
        print(f"  → 完了")

    # --- (c) subcategory + importance を更新 ---
    print("[apply C] subcategory / importance 更新")
    updated = 0
    for name, sub in subcat.items():
        t = name_to_tag.get(name)
        if not t:
            continue
        try:
            supabase.table("tags").update(
                {"subcategory": sub, "importance": 1}
            ).eq("id", t["id"]).execute()
            updated += 1
        except Exception as e:
            print(f"  ! 更新失敗 {name!r}: {e}")
    # 未分類だが残っているタグも importance=1 をセット
    survivor_unclassified = [
        n for n in plan["unclassified"]
        if n not in to_delete and n not in final_map
    ]
    for name in survivor_unclassified:
        t = name_to_tag.get(name)
        if not t:
            continue
        try:
            supabase.table("tags").update({"importance": 1}).eq("id", t["id"]).execute()
        except Exception as e:
            print(f"  ! importance 更新失敗 {name!r}: {e}")
    print(f"  → subcategory 設定 {updated} 件 / 未分類 importance=1: {len(survivor_unclassified)} 件")


def main(args):
    supabase = get_supabase()
    actual_usage = compute_actual_usage(supabase)
    tags = fetch_sub_genre_tags(supabase)

    if not tags:
        print("sub_genre タグなし。終了。")
        return

    # キャッシュ: 既に plan ファイルがあれば再利用
    plan_path = args.plan_path
    classifications: dict[str, dict] = {}
    if plan_path and os.path.exists(plan_path):
        print(f"[3] キャッシュから Gemini 分類結果を読み込み: {plan_path}")
        with open(plan_path, encoding="utf-8") as f:
            classifications = json.load(f)
        print(f"  → {len(classifications)} 件")

    # 不足分を Gemini で取得
    missing_tags = [t for t in tags if t["name"] not in classifications]
    if missing_tags:
        print(f"[3] Gemini 分類: model={args.model}  batch={args.batch_size}  対象 {len(missing_tags)} 件")
        all_names = [t["name"] for t in tags]
        new_class = classify_with_gemini(
            missing_tags, all_names,
            model=args.model, batch_size=args.batch_size,
        )
        classifications.update(new_class)
        if plan_path:
            os.makedirs(os.path.dirname(plan_path) or ".", exist_ok=True)
            with open(plan_path, "w", encoding="utf-8") as f:
                json.dump(classifications, f, ensure_ascii=False, indent=2)
            print(f"  → 分類結果を {plan_path} に保存")

    print(f"[4] 計画立案 (削除閾値 actual_usage < {args.delete_threshold})")
    plan = build_plan(tags, actual_usage, classifications, args.delete_threshold)
    print_plan(plan, tags)

    if not args.apply:
        print("(--apply 未指定: DB は更新しない)")
        return

    confirm = input("上記の計画を実行しますか? [y/N]: ").strip().lower()
    if confirm != "y":
        print("中止")
        return
    apply_plan(supabase, tags, plan)
    print("[完了]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="sub_genre タグ整理")
    parser.add_argument("--dry-run", action="store_true",
                        help="(デフォルト) 計画のみ表示")
    parser.add_argument("--apply", action="store_true",
                        help="実際に DB を更新")
    parser.add_argument("--model", default=settings.gemini_enrichment_model,
                        help="Gemini モデル")
    parser.add_argument("--batch-size", type=int, default=80,
                        help="1 リクエストあたりのタグ数")
    parser.add_argument("--delete-threshold", type=int, default=5,
                        help="マージ後 actual_usage がこの値未満の AI タグを削除")
    parser.add_argument("--plan-path", default="cache/sub_genre_plan.json",
                        help="Gemini 分類結果のキャッシュパス")
    args = parser.parse_args()
    if not (args.dry_run or args.apply):
        args.dry_run = True
    main(args)
