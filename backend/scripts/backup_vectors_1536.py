"""
openbd_test_series の 1536 次元ベクトル列を DB から取り出し、ファイルにバックアップする。
migration 040 (使わないベクトル列の削除) の前に実行する。DB への書き込みは一切しない。

対象 (8列):
  embedding / review_embedding                         … 028 (あらすじ由来) / 029 (レビュー要約由来)
  plot / cast / review_aspect の *_embedding_1536      … 040 で消す 3072 の切り詰め版
  theme / mood / reader の *_embedding_1536            … アプリが使う3列 (DB には残すが、念のため一緒に取る)
6観点の 3072 次元は backups/aspect_3072_20261015_094928/ に取得済み (その後は書き込んでいない)。

出力: backups/vectors_1536_YYYYMMDD_HHMMSS/
  <列名>.npy      列ごとの float32 (N, 1536)。値が無い行は NaN
  ids.json        行の並び (series_id)
  manifest.json   取得日時・件数・列ごとの非 NULL 件数・各ファイルの sha256

使い方:
  python -m scripts.backup_vectors_1536                     # 取得 → 保存 → 抜き取り照合
  python -m scripts.backup_vectors_1536 --verify <DIR>      # 既存バックアップを DB と照合だけする
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

from app.services.supabase_client import get_supabase

TABLE = "openbd_test_series"
DIM = 1536
COLS = ["embedding", "review_embedding",
        "plot_embedding_1536", "cast_embedding_1536", "review_aspect_embedding_1536",
        "theme_embedding_1536", "mood_embedding_1536", "reader_embedding_1536"]
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANY_FILTER = ",".join(f"{c}.not.is.null" for c in COLS)


def _retry(fn, what: str, n: int = 5):
    for i in range(n):
        try:
            return fn()
        except Exception as e:
            if i == n - 1:
                raise
            print(f"  … {what} 失敗 ({type(e).__name__})、{2 ** i}秒後にやり直し")
            time.sleep(2 ** i)


def _parse(v) -> np.ndarray | None:
    if v is None:
        return None
    arr = np.asarray(json.loads(v) if isinstance(v, str) else v, dtype=np.float32)
    return arr if arr.shape == (DIM,) else None


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def export(page: int) -> str:
    sb = get_supabase()
    ids: list[str] = []
    vecs: dict[str, list] = {c: [] for c in COLS}
    last = "00000000-0000-0000-0000-000000000000"
    while True:
        # 1.7GB の表なので、id の順に少しずつ読む (offset は後ろほど遅くなるので使わない)
        rows = _retry(lambda: sb.table(TABLE).select("id," + ",".join(COLS)).or_(ANY_FILTER)
                      .gt("id", last).order("id").limit(page).execute().data, "取得")
        if not rows:
            break
        for r in rows:
            ids.append(r["id"])
            for c in COLS:
                v = _parse(r.get(c))
                vecs[c].append(v if v is not None else np.full(DIM, np.nan, dtype=np.float32))
        last = rows[-1]["id"]
        print(f"  … {len(ids)}件")

    out = os.path.join(BASE_DIR, "backups", "vectors_1536_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out)
    manifest = {"kind": "vectors_1536", "table": TABLE, "dim": DIM, "count": len(ids),
                "exported_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "non_null": {}, "files_sha256": {}}
    for c in COLS:
        arr = np.stack(vecs[c]) if ids else np.zeros((0, DIM), dtype=np.float32)
        path = os.path.join(out, f"{c}.npy")
        np.save(path, arr)
        manifest["non_null"][c] = int((~np.isnan(arr[:, 0])).sum()) if len(arr) else 0
        manifest["files_sha256"][f"{c}.npy"] = _sha256(path)
    with open(os.path.join(out, "ids.json"), "w") as f:
        json.dump(ids, f)
    manifest["files_sha256"]["ids.json"] = _sha256(os.path.join(out, "ids.json"))
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print(f"[保存] {out} ({len(ids)}件) 非 NULL: {manifest['non_null']}")
    return out


def verify(out: str, sample: int) -> bool:
    """ファイルの sha256 と、DB から取り直した行との一致 (cos = 1.0) を確かめる"""
    manifest = json.load(open(os.path.join(out, "manifest.json")))
    for name, digest in manifest["files_sha256"].items():
        if _sha256(os.path.join(out, name)) != digest:
            print(f"[照合] ✗ {name} の sha256 が違う")
            return False
    ids = json.load(open(os.path.join(out, "ids.json")))
    arrays = {c: np.load(os.path.join(out, f"{c}.npy")) for c in COLS}
    sb = get_supabase()
    picks = random.sample(range(len(ids)), min(sample, len(ids)))
    worst = 1.0
    for start in range(0, len(picks), 20):
        part = picks[start:start + 20]
        rows = {r["id"]: r for r in _retry(lambda p=part: sb.table(TABLE).select("id," + ",".join(COLS))
                                           .in_("id", [ids[i] for i in p]).execute().data, "照合")}
        for i in part:
            for c in COLS:
                db = _parse(rows[ids[i]].get(c))
                saved = arrays[c][i]
                if db is None:
                    if not np.isnan(saved[0]):
                        print(f"[照合] ✗ {ids[i]} {c}: DB は NULL なのに保存側に値がある")
                        return False
                    continue
                cos = float(np.dot(db, saved) / (np.linalg.norm(db) * np.linalg.norm(saved)))
                worst = min(worst, cos)
    ok = worst > 0.99999
    print(f"[照合] {'✓' if ok else '✗'} 抜き取り {len(picks)}行 × {len(COLS)}列、最小 cos = {worst:.6f}")
    return ok


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="1536 次元のベクトル列をファイルにバックアップする")
    parser.add_argument("--verify", metavar="DIR")
    parser.add_argument("--sample", type=int, default=200)
    parser.add_argument("--page", type=int, default=50)
    args = parser.parse_args()
    if args.verify:
        sys.exit(0 if verify(args.verify, args.sample) else 1)
    out = export(args.page)
    sys.exit(0 if verify(out, args.sample) else 1)
