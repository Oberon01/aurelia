from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Process settings from the environment. Changing these needs a redeploy;
    anything you want to tweak live belongs in config/aurelia.yaml instead."""

    database_url: str
    redis_url: str
    config_path: str = "/config/aurelia.yaml"
    governance_path: str = "/config/governance"


settings = Settings()
