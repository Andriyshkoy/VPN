from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    encryption_key: str
    bot_token: str = ""
    per_config_cost: float = 1.0
    config_creation_cost: float = 10.0
    billing_interval: int = 3600
    admin_username: str = ""
    admin_password_hash: str = ""
    telegram_pay_token: str = ""
    redis_url: str = "redis://localhost:6379/0"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
