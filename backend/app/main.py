from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.books import router as books_router
from app.api.recommend import router as recommend_router

app = FastAPI(
    title="Ranobe Finder API",
    description="講談社ラノベ文庫の新刊推薦サービス",
    version="1.0.0",
)

# CORS設定（Reactフロントエンド用）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(books_router, prefix="/api")
app.include_router(recommend_router, prefix="/api")


@app.get("/")
def root():
    return {"message": "Ranobe Finder API is running 🎉"}


@app.get("/health")
def health():
    return {"status": "ok"}
