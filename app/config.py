from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://checkstock:checkstock@localhost:5432/checkstock"


settings = Settings()
