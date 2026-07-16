"""
ベクトル検索キャッシュ (backend/data/vector_cache/facets.npz) を DB から再生成する。

このキャッシュは「永続データ資産」であり、TTL では失効しない (リクエスト経路で
600MB を再取得してワーカーがブロックするのを防ぐため)。openbd_test_series の
6観点ベクトル (plot/cast/theme/mood/reader/review) を追加・編集したときにだけ、
本スクリプトを実行して更新する。

公開デプロイ時も、生成済みの facets.npz / meta.json をそのまま配布すれば、
DB からの再取得なしで即座に自然言語検索が使える。

使い方:
  python -m scripts.rebuild_vector_cache
"""
import sys
import os
import time
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import vector_search as V


def main():
    t = time.time()
    print("[rebuild] DB から 6観点ベクトルを再取得します (600MB 転送・数分かかる場合があります)…")
    n = V.rebuild_cache()
    size_mb = os.path.getsize(V._NPZ_PATH) / (1024 * 1024) if os.path.exists(V._NPZ_PATH) else 0
    print(f"[完了] {n:,}件を保存 ({time.time() - t:.1f}s, {size_mb:.0f}MB)")
    print(f"  npz : {V._NPZ_PATH}")
    print(f"  meta: {V._META_PATH}")


if __name__ == "__main__":
    main()
