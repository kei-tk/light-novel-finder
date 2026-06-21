"""
1) 既存シリーズ全件のタグをバックアップ
2) 先の「あらすじ無し50件実験 (tags_nosyn_exp.log)」を before 値に戻す
3) リバート後の状態を再バックアップ (genre 再評価の復元ポイント)
"""
import sys
import os
import re
import ast
import json
import datetime as _dt
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.supabase_client import get_supabase
from scripts.batch_tags_nosyn_exp import fetch_existing_series, N

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/
BACKUP_DIR = os.path.join(ROOT, "backups")
LOG_PATH = os.path.join(os.path.dirname(ROOT), "tags_nosyn_exp.log")  # プロジェクト直下


def backup_existing(sb, label: str) -> str:
    """既存シリーズ全件の id/title/A/G/S を JSON 保存。"""
    rows, off = [], 0
    while True:
        rs = sb.table("openbd_test_series").select(
            "id, series_title, atmosphere_tags, genre_tags, sub_genre_tags"
        ).is_("parent_series_id", "null").eq("is_special_edition", False)\
         .lt("created_at", "2026-06-14T00:00:00+00:00")\
         .range(off, off + 999).execute().data
        rows.extend(rs)
        if len(rs) < 1000:
            break
        off += 1000
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(BACKUP_DIR, f"existing_tags_{label}_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print(f"[バックアップ] 既存 {len(rows):,} 件 → {path}")
    return path


_ENTRY = re.compile(r"^\[(\d+)/\d+\]\s+「(.+?)」")
_TAGLINE = re.compile(r"^\s+([AGS]):\s+(\[.*?\])\s+→")


def parse_log_before(path: str) -> list[dict]:
    """ログから各エントリの before タグ (A/G/S) を順番に抽出。"""
    entries, cur = [], None
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = _ENTRY.match(line)
            if m:
                if cur:
                    entries.append(cur)
                cur = {"idx": int(m.group(1)), "title": m.group(2), "A": None, "G": None, "S": None}
                continue
            if cur:
                t = _TAGLINE.match(line)
                if t:
                    cur[t.group(1)] = ast.literal_eval(t.group(2))
        if cur:
            entries.append(cur)
    return entries


def main():
    sb = get_supabase()
    # ① バックアップ (リバート前)
    backup_existing(sb, "pre_revert")

    # ② 50件リバート
    before = parse_log_before(LOG_PATH)
    print(f"[ログ解析] {len(before)} エントリの before 値を取得")
    series = fetch_existing_series(sb, N)  # 実験と同一クエリ・同順
    if len(series) != len(before):
        print(f"  ! 件数不一致 (DB {len(series)} / ログ {len(before)}) — 中断")
        return
    reverted = mismatch = 0
    for i, (row, b) in enumerate(zip(series, before), 1):
        # ログのタイトルは 36 字で切り詰められているので前方一致で照合
        if not row["series_title"].startswith(b["title"][:30]):
            print(f"  ! [{i}] タイトル不一致: DB='{row['series_title']}' ログ='{b['title']}' → skip")
            mismatch += 1
            continue
        sb.table("openbd_test_series").update({
            "atmosphere_tags": b["A"], "genre_tags": b["G"], "sub_genre_tags": b["S"],
        }).eq("id", row["id"]).execute()
        reverted += 1
    print(f"[リバート] {reverted} 件を before 値に復元 (不一致 {mismatch})")

    # ③ リバート後バックアップ (genre 再評価の復元ポイント)
    backup_existing(sb, "post_revert")


if __name__ == "__main__":
    main()
