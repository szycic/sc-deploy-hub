"""
Configuration models and loader for sc-deploy-hub.

Reads config.yaml from the project root (or the path set in the CONFIG_PATH
environment variable) and exposes strongly-typed Pydantic models for the rest
of the application to consume.
"""

import os
import yaml
from dotenv import load_dotenv
from typing import Dict, List, Optional
from pydantic import BaseModel, Field

load_dotenv()

class RepositoryConfig(BaseModel):
    """Configuration for a single monitored repository.

    Attributes:
        path: Absolute path to the local git clone.
        branch: Branch that triggers deployments (default: ``"main"``).
        service_name: Name of the systemd service unit to restart after deploy.
        deploy_steps: Ordered list of shell commands to run between the git
            update and the service restart (e.g. installing dependencies).
    """

    path: str
    branch: str = "main"
    service_name: str
    deploy_steps: List[str] = Field(default_factory=list)


class WebhookConfig(BaseModel):
    """Webhook authentication settings.

    Attributes:
        github_secret: HMAC-SHA256 secret configured in GitHub.  When set,
            every incoming webhook payload is verified against its signature.
            Leave ``None`` to skip verification (not recommended in production).
    """

    github_secret: Optional[str] = None


class AppConfig(BaseModel):
    """Top-level application configuration.

    Attributes:
        webhooks: Webhook authentication settings.
        repositories: Mapping of repository name → repository configuration.
    """

    webhooks: WebhookConfig = Field(default_factory=WebhookConfig)
    repositories: Dict[str, RepositoryConfig] = Field(default_factory=dict)


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.getenv("CONFIG_PATH", os.path.join(PROJECT_ROOT, "config.yaml"))


def load_config() -> AppConfig:
    """Load and validate ``config.yaml`` from disk.

    Returns an empty :class:`AppConfig` when the file does not exist so that
    the application can start even before the operator has created a config.

    Returns:
        Validated :class:`AppConfig` instance.
    """
    if not os.path.exists(CONFIG_PATH):
        return AppConfig()
    with open(CONFIG_PATH, "r") as f:
        data = yaml.safe_load(f) or {}
    return AppConfig.model_validate(data)


def save_config(config: AppConfig) -> None:
    """Serialise *config* back to ``config.yaml``.

    Args:
        config: The :class:`AppConfig` to persist.
    """
    with open(CONFIG_PATH, "w") as f:
        yaml.safe_dump(config.model_dump(), f)
