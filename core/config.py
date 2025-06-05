from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    encryption_key: str
    bot_token: str = ""
    per_config_cost: float = 1.0
    billing_interval: int = 3600

    class Config:
        env_file = '.env'


settings = Settings()
