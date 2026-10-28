"""
ラノベ書庫 (Next.js) のキャッシュを古い扱いにする。DB を更新したタスクの最後に呼ぶ。
送り先とシークレットは .env の REVALIDATE_URLS / REVALIDATE_SECRET。

使い方:
  python -m scripts.revalidate_app             # タグ series
  python -m scripts.revalidate_app --tag series --tag series:<uuid>
"""
import sys
import os
import argparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.pipeline_common import revalidate

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="アプリのキャッシュを捨てる")
    parser.add_argument("--tag", action="append", default=None)
    revalidate(parser.parse_args().tag)
