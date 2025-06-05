from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    encryption_key: str
    bot_token: str = ""

    class Config:
        env_file = '.env'


settings = Settings()
