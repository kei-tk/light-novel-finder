"""
6観点の 3072次元ベクトル (*_embedding) を DB から取り出し、ファイルにバックアップする。
migration 032 (1536 列の追加と変換) の前に実行する。DB への書き込みは一切しない。

出力: backups/aspect_3072_YYYYMMDD_HHMMSS/
  plot.npy ... review.npy   観点ごとの float32 (N, 3072)。値が無い行は NaN
  ids.json                  行の並び (series_id)
  manifest.json             取得日時・モデル・次元・件数・各ファイルの sha256・
                            各行の *_text の sha256 (どのテキストから作られたベクトルか追えるように)

backups/ は .gitignore 済み。旧 facets.npz (2026-07) はその後の更新が入っていない可能性があるので
正本にせず、必ずここで DB から取り直す。

使い方:
  python -m scripts.backup_aspect_3072                       # 取得 → 保存 → 抜き取り照合
  python -m scripts.backup_aspect_3072 --verify <DIR>        # 既存バックアップを DB と照合だけする
  python -m scripts.backup_aspect_3072 --verify <DIR> --sample 300
"""
import sys
import os
import json
import time
import random
import hashlib
import argparse
import datetime as dt
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from app.config import settings
from app.services.supabase_client import get_supabase

TABLE = "openbd_test_series"
DIM = 3072
# 観点 → (テキスト列, 3072 ベクトル列)。reembed_aspects_large.py と同じ対応
ASPECT_COLS = {
    "plot": ("plot_text", "plot_embedding"),
    "cast": ("cast_text", "cast_embedding"),
    "theme": ("theme_text", "theme_embedding"),
    "mood": ("mood_text", "mood_embedding"),
    "reader": ("reader_text", "reader_embedding"),
    "review": ("review_aspect_text", "review_aspect_embedding"),
}
ASPECTS = list(ASPECT_COLS)
VEC_COLS = [c[1] for c in ASPECT_COLS.values()]
TEXT_COLS = [c[0] for c in ASPECT_COLS.values()]
# どれか1観点でもベクトルがある行を対象にする
ANY_VEC_FILTER = ",".join(f"{c}.not.is.null" for c in VEC_COLS)

BACKUP_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backups")


def _retry(fn, what, n=5):
    for a in range(n):
        try:
            return fn()
        except Exception as e:
            if a == n - 1:
                raise
            print(f"  ! {what} 失敗 ({str(e)[:80]}) → {1.5 * (a + 1):.1f}秒後に再試行", flush=True)
            time.sleep(1.5 * (a + 1))


def _parse_vec(v) -> np.ndarray | None:
    if v is None:
        return None
    if isinstance(v, str):
        return np.array(v.strip("[]").split(","), dtype=np.float32)
    return np.asarray(v, dtype=np.float32)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_text(t: str | None) -> str | None:
    return hashlib.sha256(t.encode("utf-8")).hexdigest() if t else None


def export(page: int) -> str:
    sb = get_supabase()

    print("[件数確認]")
    head = _retry(lambda: sb.table(TABLE).select("id", count="exact")
                  .or_(ANY_VEC_FILTER).limit(1).execute(), "count")
    total = head.count or 0
    print(f"  ベクトルのある series: {total:,}件")
    if total == 0:
        print("  対象がありません。終了します。")
        return ""

    out_dir = os.path.join(BACKUP_ROOT, "aspect_3072_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=False)

    # 件数分を先に確保 (約 600MB)。値が無い観点は NaN のまま残る
    mats = {a: np.full((total, DIM), np.nan, dtype=np.float32) for a in ASPECTS}
    ids: list[str] = []
    text_hashes: dict[str, dict[str, str | None]] = {}
    meta_rows: dict[str, dict] = {}

    print(f"[取得] {page}件ずつ (id 順)")
    select_cols = "id,aspect_source,aspect_built_at," + ",".join(TEXT_COLS + VEC_COLS)
    last_id = None
    started = time.time()
    while True:
        # offset ではなく id のキーセットで進める (途中で行が増減してもずれない)
        def _fetch(after=last_id):
            q = sb.table(TABLE).select(select_cols).or_(ANY_VEC_FILTER).order("id").limit(page)
            if after:
                q = q.gt("id", after)
            return q.execute()
        rows = _retry(_fetch, f"page after {last_id}").data or []
        if not rows:
            break
        for r in rows:
            i = len(ids)
            if i >= total:
                raise RuntimeError("件数確認より多い行が返りました。取得中に DB が更新された可能性があります。やり直してください。")
            ids.append(r["id"])
            for a in ASPECTS:
                tcol, vcol = ASPECT_COLS[a]
                v = _parse_vec(r.get(vcol))
                if v is not None:
                    if v.shape[0] != DIM:
                        raise RuntimeError(f"{r['id']} {vcol} の次元が {v.shape[0]} です (期待値 {DIM})")
                    mats[a][i] = v
            text_hashes[r["id"]] = {a: _sha256_text(r.get(ASPECT_COLS[a][0])) for a in ASPECTS}
            meta_rows[r["id"]] = {"aspect_source": r.get("aspect_source"),
                                  "aspect_built_at": r.get("aspect_built_at")}
        last_id = rows[-1]["id"]
        print(f"  {len(ids):,}/{total:,}  ({time.time() - started:.0f}秒)", flush=True)
        if len(rows) < page:
            break

    if len(ids) != total:
        raise RuntimeError(f"取得件数 {len(ids)} が件数確認 {total} と一致しません。やり直してください。")

    print("[保存]")
    files = {}
    for a in ASPECTS:
        path = os.path.join(out_dir, f"{a}.npy")
        np.save(path, mats[a])
        files[f"{a}.npy"] = _sha256_file(path)
    with open(os.path.join(out_dir, "ids.json"), "w", encoding="utf-8") as f:
        json.dump(ids, f)
    files["ids.json"] = _sha256_file(os.path.join(out_dir, "ids.json"))

    manifest = {
        "kind": "aspect_embeddings_3072",
        "table": TABLE,
        "exported_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": settings.openai_aspect_embedding_model,
        "dim": DIM,
        "count": total,
        "aspects": {a: {"text_col": ASPECT_COLS[a][0], "vec_col": ASPECT_COLS[a][1],
                        "non_null": int((~np.isnan(mats[a][:, 0])).sum())} for a in ASPECTS},
        "files_sha256": files,
        "rows": {sid: {"text_sha256": text_hashes[sid], **meta_rows[sid]} for sid in ids},
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False)
    for a in ASPECTS:
        print(f"  {a:6}: {manifest['aspects'][a]['non_null']:,}件")
    print(f"  → {out_dir}")
    return out_dir


def verify(out_dir: str, sample: int) -> bool:
    """ファイルの sha256 を確かめ、ランダムな行を DB から取り直して完全一致するか照合する。"""
    print(f"[照合] {out_dir}")
    with open(os.path.join(out_dir, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    ok = True
    for name, expected in manifest["files_sha256"].items():
        if _sha256_file(os.path.join(out_dir, name)) != expected:
            print(f"  ✗ {name} の sha256 が manifest と一致しません")
            ok = False
    if not ok:
        return False
    print("  ✓ ファイルの sha256 は manifest と一致")

    with open(os.path.join(out_dir, "ids.json"), encoding="utf-8") as f:
        ids = json.load(f)
    mats = {a: np.load(os.path.join(out_dir, f"{a}.npy"), mmap_mode="r") for a in ASPECTS}
    pos = {sid: i for i, sid in enumerate(ids)}
    picked = random.sample(ids, min(sample, len(ids)))

    sb = get_supabase()
    mismatched, changed_text = [], 0
    # 1回の取得は 20件ずつ (3072×6 列で応答が大きいため)
    for k in range(0, len(picked), 20):
        part = picked[k:k + 20]
        rows = _retry(lambda p=part: sb.table(TABLE).select("id," + ",".join(TEXT_COLS + VEC_COLS))
                      .in_("id", p).execute(), "verify fetch").data or []
        for r in rows:
            i = pos[r["id"]]
            for a in ASPECTS:
                tcol, vcol = ASPECT_COLS[a]
                db_v = _parse_vec(r.get(vcol))
                bk_v = np.asarray(mats[a][i])
                bk_null = bool(np.isnan(bk_v[0]))
                if db_v is None and bk_null:
                    continue
                if db_v is None or bk_null or not np.array_equal(db_v, bk_v):
                    mismatched.append((r["id"], a))
                if _sha256_text(r.get(tcol)) != manifest["rows"][r["id"]]["text_sha256"][a]:
                    changed_text += 1
    if mismatched:
        print(f"  ✗ {len(mismatched)}件の (series, 観点) が DB と一致しません。例: {mismatched[:3]}")
        print("    バックアップ後に DB が更新された可能性があります。取り直してください。")
        return False
    print(f"  ✓ ランダムな {len(picked)}件 × 6観点が DB と完全一致")
    if changed_text:
        print(f"  ⚠ テキスト (*_text) が変わっている観点が {changed_text}件あります (ベクトルは一致)")
    return True


def main():
    ap = argparse.ArgumentParser(description="6観点 3072 ベクトルのバックアップ (読み取り専用)")
    ap.add_argument("--verify", metavar="DIR", help="既存のバックアップを DB と照合だけする")
    ap.add_argument("--sample", type=int, default=100, help="照合する行数 (既定 100)")
    ap.add_argument("--page", type=int, default=100, help="1回に取得する行数 (既定 100)")
    args = ap.parse_args()

    if args.verify:
        sys.exit(0 if verify(args.verify, args.sample) else 1)
    out_dir = export(args.page)
    if out_dir:
        sys.exit(0 if verify(out_dir, args.sample) else 1)


if __name__ == "__main__":
    main()
