import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PAYSWIFT_BASE_URL: str = os.getenv("PAYSWIFT_BASE_URL", "http://localhost:8081")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./quickdrop.db")
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY", None)
    
    # Financial thresholds according to policy and finance rules
    MAX_AUTO_PAY_LIMIT: int = 200  # Up to ₹200 auto-approved
    MAX_AUTO_PAYS_PER_DAY: int = 1  # 1 per rider per day
    DISPUTE_LOOKBACK_DAYS: int = 7  # Only last 7 days disputes accepted
    
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    ENVIRONMENT: str = "development"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
