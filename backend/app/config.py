from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # X API
    x_bearer_token: str
    x_api_key: str = ""
    x_api_secret: str = ""

    # Gemini
    gemini_api_key: str
    gemini_analysis_model: str = "gemini-2.5-pro"    # ツイート判定用
    gemini_enrichment_model: str = "gemini-3.1-pro-preview"  # あらすじ・タグ生成・発売日確認用
    gemini_parse_model: str = "gemini-2.5-flash"     # タイトル分解（軽量タスク）

    # OpenAI (ベクトル埋め込み専用。タグ・スコア判定は Gemini を使う)
    openai_api_key: str = ""
    openai_embedding_model: str = "text-embedding-3-small"

    # xAI Grok (Live Search でレビュー収集の実験用)
    xai_api_key: str = ""

    # Supabase
    supabase_url: str
    supabase_service_key: str

    # アプリ設定
    target_x_account: str = "K_lanove_bunko"
    label_name: str = "講談社ラノベ文庫"
    data_fetch_months: int = 6
    like_threshold: int = 500  # トレンド判定のいいね数閾値

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
