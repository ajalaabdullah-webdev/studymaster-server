"""Application configuration.

All settings are loaded from environment variables (see .env). Values that
must match the legacy Node backend (JWT secret, Mongo URI) are carried over so
this service is drop-in compatible with existing tokens and data.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- App ---
    env: str = "development"
    port: int = 8000
    frontend_url: str = "http://localhost:3000"
    cors_origins: str = "http://localhost:3000"

    # --- Database ---
    mongodb_uri: str
    mongodb_db: str = "studymaster"

    # --- Auth (must match legacy Node backend) ---
    jwt_secret: str
    jwt_expire_days: int = 30
    jwt_algorithm: str = "HS256"

    # --- LLM ---
    openai_api_key: str | None = None
    openai_tts_model: str = "gpt-4o-mini-tts"
    openai_tts_voice: str = "marin"
    openai_tts_instructions: str = (
        "Speak like a warm, patient Nigerian secondary-school teacher. Use a calm, clear pace, "
        "natural pauses, and encouraging emphasis. Read equations and technical terms carefully."
    )
    openai_live_model: str = "gpt-live-1"
    openai_live_voice: str = "marin"
    openai_decisions_model: str = "gpt-6-luna"
    # The reasoning model behind the live voice. With responses delegation this
    # runs inside OpenAI, which removes the browser -> Render -> OpenAI round
    # trip that made "thinking" pauses 2-3 seconds long.
    openai_delegate_model: str = "gpt-6-luna"
    # Picking a prepared board card is not a hard problem. Measured on the
    # board assistant (median of 3, Oct 2026): low/default 3.1s, none/default
    # 2.5s, none/priority 1.0s — and none/priority picked the right card 3/3.
    openai_delegate_reasoning: str = "none"
    # "priority" is ~3x faster for these short calls but billed at a higher
    # rate; set OPENAI_DELEGATE_SERVICE_TIER= (empty) to use the account default.
    openai_delegate_service_tier: str = "priority"
    # Writes each lesson's board content ahead of time (services/lesson_pack.py).
    # Runs in the background during the greeting, so accuracy beats speed here.
    lesson_pack_model: str = "openai/gpt-6-luna"
    deepseek_api_key: str | None = None
    llm_default_model: str = "openai/gpt-4o-mini"
    llm_smart_model: str = "openai/gpt-4o"
    llm_cheap_model: str | None = None
    # Embeddings for meaning-based question search. 256 dims keeps the vectors
    # small (~2KB/question) while staying accurate for short exam questions.
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 256

    # --- Gemini (Live) ---
    gemini_api_key: str | None = None
    gemini_live_model: str = "gemini-3.1-flash-live-preview"

    # --- Spitch (dictation/STT only) ---
    spitch_api_key: str | None = None
    spitch_voice: str = "john"  # Nigerian-English voice (others: lucy, henry, lina, jude, kani)
    spitch_language: str = "en"

    # --- Search ---
    search_api_key: str | None = None
    search_provider: str = "tavily"

    # --- Email ---
    resend_api_key: str | None = None
    email_from: str = "StudyMaster <noreply@studymaster.live>"

    # --- Google OAuth ---
    google_client_id: str | None = None

    # --- Cloudinary ---
    cloudinary_cloud_name: str | None = None
    cloudinary_api_key: str | None = None
    cloudinary_api_secret: str | None = None

    # --- Admin ---
    admin_password: str | None = None

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.env.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
