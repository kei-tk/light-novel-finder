from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.series import router as series_router
from app.api.users import router as users_router

app = FastAPI(
    title="Ranobe Finder API",
    description="ライトノベル推薦・検索 + ユーザー機能",
    version="2.0.0",
)

# CORS 設定 (Vite dev サーバー想定)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
    ],
    # 開発時は localhost の任意ポート (Vite の自動ポート割当含む) を許可
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(series_router, prefix="/api")
app.include_router(users_router, prefix="/api")


@app.get("/")
def root():
    return {"message": "Ranobe Finder API v2"}


@app.get("/health")
def health():
    return {"status": "ok"}
