"""
6観点ベクトルを 3072 → 1536 に変換する (migration 032 の関数を RPC で呼ぶ)。

既定は --dry-run (DB を書き換えない)。順番:
  1. python -m scripts.backup_aspect_3072                 バックアップ + 照合
  2. Supabase の SQL Editor で migrations/032_aspect_embeddings_1536.sql を実行
  3. python -m scripts.migrate_aspect_1536                dry-run (件数・容量の見積もり、前提の確認)
     python -m scripts.migrate_aspect_1536 --check-truncation 5
                                                          切り詰め = dimensions 指定 になるかを実 API で確認
  4. python -m scripts.migrate_aspect_1536 --apply        変換 ([y/N] で確認)
  5. python -m scripts.migrate_aspect_1536 --verify       変換結果の照合
  6. migrations/033_aspect_hnsw_1536.sql で HNSW を作成
"""
import sys
import os
import glob
import json
import time
import random
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from openai import OpenAI

from app.config import settings
from app.services.supabase_client import get_supabase
from scripts.backup_aspect_3072 import (
    ASPECTS, ASPECT_COLS, BACKUP_ROOT, TABLE, _parse_vec, _retry, _sha256_text,
)

NEW_DIM = 1536
NEW_COL = {a: ASPECT_COLS[a][1] + "_1536" for a in ASPECTS}
# HNSW (m=16) の大きさの目安: ベクトル本体 + 近傍リスト。経験的にベクトル本体の 1.1〜1.3 倍
HNSW_FACTOR = (1.1, 1.3)


def _latest_backup() -> str | None:
    dirs = sorted(glob.glob(os.path.join(BACKUP_ROOT, "aspect_3072_*")))
    return dirs[-1] if dirs else None


def _truncate_normalize(v: np.ndarray) -> np.ndarray:
    t = v[:NEW_DIM].astype(np.float32)
    n = np.linalg.norm(t)
    return t / n if n else t


def _count(sb, col: str) -> int:
    r = _retry(lambda: sb.table(TABLE).select("id", count="exact").not_.is_(col, "null").limit(1).execute(),
               f"count {col}")
    return r.count or 0


def _mb(b: float) -> str:
    return f"{b / 1024 / 1024:,.0f}MB"


def dry_run(backup_dir: str | None, check_truncation: int) -> bool:
    sb = get_supabase()
    ok = True

    print("[1. バックアップ]")
    backup_dir = backup_dir or _latest_backup()
    if not backup_dir or not os.path.exists(os.path.join(backup_dir, "manifest.json")):
        print("  ✗ バックアップが見つかりません。先に python -m scripts.backup_aspect_3072 を実行してください。")
        return False
    with open(os.path.join(backup_dir, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    print(f"  ✓ {backup_dir}")
    print(f"    取得日時 {manifest['exported_at']} / {manifest['count']:,}件 / {manifest['model']} {manifest['dim']}次元")

    print("[2. migration 032 が適用済みか]")
    try:
        sb.table(TABLE).select(",".join(NEW_COL.values())).limit(1).execute()
        print("  ✓ *_embedding_1536 列がある")
    except Exception as e:
        print(f"  ✗ 1536 列がありません。032 を SQL Editor で実行してください。({str(e)[:80]})")
        ok = False
    try:
        # p_limit=0 は何も更新せずに (0, NULL) を返すので、関数の有無だけを確かめられる
        sb.rpc("migrate_aspect_1536_batch", {"p_after": None, "p_limit": 0}).execute()
        print("  ✓ migrate_aspect_1536_batch() を呼べる")
    except Exception as e:
        print(f"  ✗ migrate_aspect_1536_batch() を呼べません。({str(e)[:80]})")
        ok = False

    print("[3. 件数]")
    print(f"  {'観点':8} {'3072 あり':>10} {'1536 あり':>10} {'バックアップ':>12}")
    total_vectors = 0
    for a in ASPECTS:
        n_old = _count(sb, ASPECT_COLS[a][1])
        n_new = _count(sb, NEW_COL[a]) if ok else 0
        n_bk = manifest["aspects"][a]["non_null"]
        total_vectors += n_old
        mark = "" if n_old == n_bk else "  ⚠ バックアップと件数が違う (取り直しを推奨)"
        print(f"  {a:8} {n_old:>10,} {n_new:>10,} {n_bk:>12,}{mark}")

    print("[4. 容量の見積もり]")
    data = total_vectors * (4 * NEW_DIM + 8)
    print(f"  1536 列の本体: 約 {_mb(data)} (6観点 × {total_vectors // 6:,}件前後)")
    print(f"  HNSW 6本 (033): 約 {_mb(data * HNSW_FACTOR[0])}〜{_mb(data * HNSW_FACTOR[1])}")
    print(f"  旧 3072 列 (切り替えの1週間後まで残す): 約 {_mb(total_vectors * (4 * 3072 + 8))}")

    print("[5. バックアップから変換をローカルで試算]")
    zero, rows_done = 0, 0
    for a in ASPECTS:
        m = np.load(os.path.join(backup_dir, f"{a}.npy"), mmap_mode="r")
        valid = ~np.isnan(m[:, 0])
        head = np.asarray(m[valid][:, :NEW_DIM], dtype=np.float32)
        norms = np.linalg.norm(head, axis=1)
        zero += int((norms == 0).sum())
        rows_done += int(valid.sum())
        print(f"  {a:8} 切り詰め後のノルム: 最小 {norms.min():.4f} / 中央値 {np.median(norms):.4f} / 最大 {norms.max():.4f}")
    if zero:
        print(f"  ⚠ 先頭 1536 がすべて 0 のベクトルが {zero}件あります (正規化できないので 0 のまま入る)")
    else:
        print(f"  ✓ {rows_done:,}本すべて正規化できる")

    if check_truncation:
        ok = _check_truncation(sb, backup_dir, manifest, check_truncation) and ok

    print("[結果]")
    print("  ✓ --apply に進めます" if ok else "  ✗ 上の ✗ を解消してから --apply してください")
    return ok


def _check_truncation(sb, backup_dir: str, manifest: dict, n: int) -> bool:
    """「3072 を切り詰めて正規化」と「dimensions=1536 で埋め込み」が同じになるかを実 API で確かめる。"""
    print(f"[6. 切り詰め = dimensions 指定 の確認] {n}件 × 6観点 (OpenAI API を {n * 6}回分使う)")
    with open(os.path.join(backup_dir, "ids.json"), encoding="utf-8") as f:
        ids = json.load(f)
    pos = {sid: i for i, sid in enumerate(ids)}
    mats = {a: np.load(os.path.join(backup_dir, f"{a}.npy"), mmap_mode="r") for a in ASPECTS}
    text_cols = [ASPECT_COLS[a][0] for a in ASPECTS]
    client = OpenAI(api_key=settings.openai_api_key)

    picked = random.sample(ids, min(n * 3, len(ids)))
    rows = _retry(lambda: sb.table(TABLE).select("id," + ",".join(text_cols)).in_("id", picked).execute(),
                  "fetch texts").data or []
    sims = []
    for r in rows:
        if len(sims) >= n * 6:
            break
        for a in ASPECTS:
            t = r.get(ASPECT_COLS[a][0])
            local = np.asarray(mats[a][pos[r["id"]]])
            # テキストがバックアップ時から変わっていたら比べられないので飛ばす
            if not t or np.isnan(local[0]) or _sha256_text(t) != manifest["rows"][r["id"]]["text_sha256"][a]:
                continue
            # reembed_aspects_large.py と同じく先頭 8000 文字で埋め込む
            emb = client.embeddings.create(model=manifest["model"], input=t.strip()[:8000],
                                           dimensions=NEW_DIM).data[0].embedding
            api_v = np.asarray(emb, dtype=np.float32)
            api_v /= np.linalg.norm(api_v)
            sims.append(float(api_v @ _truncate_normalize(local)))
    if not sims:
        print("  ⚠ 比べられる行がありませんでした (テキストがすべて変わっている?)")
        return False
    sims = np.array(sims)
    print(f"  cos: 最小 {sims.min():.5f} / 平均 {sims.mean():.5f} ({len(sims)}組)")
    # 埋め込み API の出力には小さな揺れがあるので、完全な 1.0 にはならない
    if sims.min() >= 0.995:
        print("  ✓ 切り詰め + 正規化で dimensions=1536 と同じベクトルになる")
        return True
    print("  ✗ 一致しません。モデルが違う / 埋め込み時のテキストの前処理が違う可能性があります。--apply しないでください。")
    return False


def apply(batch: int, yes: bool, after: str | None = None) -> None:
    sb = get_supabase()
    if not yes:
        ans = input(f"openbd_test_series の *_embedding_1536 を書き込みます ({batch}件ずつ)。続けますか? [y/N] ")
        if ans.strip().lower() != "y":
            print("中止しました。")
            return
    print("[変換]")
    # 途中で止まったときは --after <最後に表示された last_id> で続きから再開できる
    last_id, done, started = after, 0, time.time()
    # statement timeout はバッチの大きさに関係なく、DB の I/O が詰まったときに出る。
    # 出たら件数を半分にして待ち、成功が続いたら元の件数へ戻す
    size, fails = batch, 0
    while True:
        try:
            res = sb.rpc("migrate_aspect_1536_batch", {"p_after": last_id, "p_limit": size}).execute()
        except Exception as e:
            fails += 1
            if fails > 12:
                print(f"  ✗ 失敗が続いたので止めます。--after {last_id} で再開できます。")
                raise
            size = max(5, size // 2)
            wait = min(30, 3 * fails)
            print(f"  ! 失敗 ({str(e)[:60]}) → {wait}秒待って {size}件で再試行", flush=True)
            time.sleep(wait)
            continue
        fails = 0
        size = min(batch, size * 2)
        row = (res.data or [{}])[0]
        processed = row.get("processed") or 0
        if processed == 0:
            break
        done += processed
        last_id = row["last_id"]
        print(f"  {done:,}件 ({time.time() - started:.0f}秒)  last_id={last_id}", flush=True)
    print(f"  完了: {done:,}件。次に --verify を実行してください。")


def verify(sample: int) -> bool:
    sb = get_supabase()
    ok = True
    print("[件数の一致]")
    for a in ASPECTS:
        n_old, n_new = _count(sb, ASPECT_COLS[a][1]), _count(sb, NEW_COL[a])
        mark = "✓" if n_old == n_new else "✗"
        ok = ok and n_old == n_new
        print(f"  {mark} {a:8} 3072 {n_old:,} / 1536 {n_new:,}")

    print(f"[値の照合] ランダムな {sample}件: DB の 1536 列 = DB の 3072 列を切り詰めて正規化したもの")
    ids_rows = _retry(lambda: sb.table(TABLE).select("id").not_.is_(NEW_COL["plot"], "null").execute(),
                      "fetch ids").data or []
    picked = random.sample([r["id"] for r in ids_rows], min(sample, len(ids_rows)))
    cols = ",".join([ASPECT_COLS[a][1] for a in ASPECTS] + list(NEW_COL.values()))
    max_diff, max_norm_err = 0.0, 0.0
    for k in range(0, len(picked), 20):
        part = picked[k:k + 20]
        rows = _retry(lambda p=part: sb.table(TABLE).select("id," + cols).in_("id", p).execute(),
                      "verify fetch").data or []
        for r in rows:
            for a in ASPECTS:
                old_v, new_v = _parse_vec(r.get(ASPECT_COLS[a][1])), _parse_vec(r.get(NEW_COL[a]))
                if old_v is None and new_v is None:
                    continue
                if old_v is None or new_v is None or new_v.shape[0] != NEW_DIM:
                    print(f"  ✗ {r['id']} {a}: 片方だけ NULL か、次元が違う")
                    ok = False
                    continue
                max_diff = max(max_diff, float(np.abs(new_v - _truncate_normalize(old_v)).max()))
                max_norm_err = max(max_norm_err, abs(float(np.linalg.norm(new_v)) - 1.0))
    print(f"  最大の誤差 {max_diff:.2e} / ノルムの最大のずれ {max_norm_err:.2e}")
    if max_diff > 1e-5 or max_norm_err > 1e-4:
        ok = False
    print("[結果]")
    print("  ✓ 変換は正しく終わっています。033 で HNSW を作成できます。" if ok else "  ✗ 不一致があります。")
    return ok


def main():
    ap = argparse.ArgumentParser(description="6観点ベクトルの 3072 → 1536 変換")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="既定。DB を書き換えずに前提と見積もりを確認")
    mode.add_argument("--apply", action="store_true", help="変換を実行する")
    mode.add_argument("--verify", action="store_true", help="変換結果を照合する")
    ap.add_argument("--backup-dir", help="dry-run で見るバックアップ (既定: 最新)")
    ap.add_argument("--check-truncation", type=int, default=0, metavar="N",
                    help="dry-run で、N件 × 6観点を実際に dimensions=1536 で埋め込んで比べる")
    # 200件だと statement timeout にかかることがあるので 50件にしている
    ap.add_argument("--batch", type=int, default=50, help="--apply で1回に変換する行数 (既定 50)")
    ap.add_argument("--after", help="--apply をこの id の次から再開する")
    ap.add_argument("--sample", type=int, default=100, help="--verify で照合する行数 (既定 100)")
    ap.add_argument("--yes", action="store_true", help="--apply の確認を省く")
    args = ap.parse_args()

    if args.apply:
        apply(args.batch, args.yes, args.after)
    elif args.verify:
        sys.exit(0 if verify(args.sample) else 1)
    else:
        sys.exit(0 if dry_run(args.backup_dir, args.check_truncation) else 1)


if __name__ == "__main__":
    main()
