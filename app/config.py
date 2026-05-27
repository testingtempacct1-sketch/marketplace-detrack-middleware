from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "Marketplace Detrack Middleware"
    database_url: str = "sqlite:///./middleware.db"

    detrack_api_key: str = ""
    detrack_base_url: str = "https://app.detrack.com/api/v2/jobs"

    shopify_webhook_secret: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
