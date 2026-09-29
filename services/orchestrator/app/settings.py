from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Process settings from the environment. Changing these needs a redeploy;
    anything you want to tweak live belongs in config/aurelia.yaml instead."""

    database_url: str
    redis_url: str
    config_path: str = "/config/aurelia.yaml"
    governance_path: str = "/config/governance"
    # Where functions reach the orchestrator; Mnemosyne registers at this address.
    self_url: str = "http://orchestrator:8000"


settings = Settings()
