"""Vercel用エントリーポイント。FastAPIとVite成果物を同一ドメインで配信する。"""

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = ROOT_DIR / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.main import app  # noqa: E402


# FastAPI 0.142以降ではVite成果物をVercel CDNへ昇格できる。
# ローカルの既存venv（FastAPI 0.111）でAPIだけ起動する場合は登録をスキップする。
if hasattr(app, "frontend"):
    app.frontend("/", directory=ROOT_DIR / "frontend" / "dist", fallback="index.html")
