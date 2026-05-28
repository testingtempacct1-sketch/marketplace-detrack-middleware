from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "Marketplace Detrack Middleware"
    database_url: str = "sqlite:///./middleware.db"

    detrack_api_key: str = ""
    detrack_base_url: str = "https://app.detrack.com/api/v2/jobs"
    detrack_webhook_secret: str = ""

    shopify_webhook_secret: str = ""
    shopify_store_domain: str = ""
    shopify_admin_access_token: str = ""
    shopify_client_id: str = ""
    shopify_client_secret: str = ""
    shopify_admin_scopes: str = ""
    shopify_fulfilment_dry_run: bool = True
    shopify_fulfilment_allowed: bool = False



    admin_api_key: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
