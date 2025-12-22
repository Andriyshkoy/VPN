from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    encryption_key: str
    bot_token: str = ""
    billing_interval: int = 3600
    admin_tg_ids: str = ""
    telegram_pay_token: str = ""
    redis_url: str = "redis://localhost:6379/0"


settings = Settings()
