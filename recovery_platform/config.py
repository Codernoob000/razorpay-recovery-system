"""
recovery_platform/config.py
============================
Unified configuration loader for the AI Revenue Recovery Platform.

Reads:
  1. config.yaml  – baseline recovery policy (checked into repo)
  2. Environment variables / .env file – secrets and deployment overrides

All settings are validated by Pydantic at startup, so the app fails fast
on any missing or malformed config rather than silently misbehaving.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_YAML_PATH = _PROJECT_ROOT / "config.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file and return its contents as a dict."""
    if not path.exists():
        raise FileNotFoundError(f"config.yaml not found at: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data or {}


# ---------------------------------------------------------------------------
# Nested Pydantic models for config.yaml sections
# ---------------------------------------------------------------------------


class DiscountEligibility(BaseSettings):
    """Controls who can receive a discount offer during recovery."""

    model_config = SettingsConfigDict(extra="forbid")

    min_tier: Literal["starter", "business", "enterprise"] = "enterprise"
    max_pct: int = Field(default=15, ge=0, le=100, description="Max discount % (0-100)")


class RetryDelays(BaseSettings):
    """Minimum wait times (minutes) before each retry attempt."""

    model_config = SettingsConfigDict(extra="forbid")

    soft_decline_min: int = Field(default=60, gt=0)
    technical_failure_min: int = Field(default=15, gt=0)


class RetryStrategies(BaseSettings):
    """Ordered action lists for each decline category."""

    model_config = SettingsConfigDict(extra="forbid")

    soft_decline: list[str] = ["retry_same_method", "offer_emi", "offer_discount"]
    hard_decline: list[str] = ["escalate_to_human"]
    technical_failure: list[str] = ["retry_same_method", "retry_alternate_gateway"]


class TierPriority(BaseSettings):
    """Numeric priority weights per customer tier (higher = more aggressive)."""

    model_config = SettingsConfigDict(extra="forbid")

    enterprise: int = 3
    business: int = 2
    starter: int = 1


class RecoveryPolicy(BaseSettings):
    """Top-level recovery policy parsed from config.yaml."""

    model_config = SettingsConfigDict(extra="forbid")

    max_retries: int = Field(default=3, ge=1, le=10)
    risk_hold_action: str = "escalate_to_human"
    discount_eligibility: DiscountEligibility = DiscountEligibility()
    retry_delays: RetryDelays = RetryDelays()
    retry_strategies: RetryStrategies = RetryStrategies()
    tier_priority: TierPriority = TierPriority()

    @field_validator("risk_hold_action")
    @classmethod
    def _valid_action(cls, v: str) -> str:
        allowed = {"escalate_to_human", "block", "flag_for_review"}
        if v not in allowed:
            raise ValueError(f"risk_hold_action must be one of {allowed}, got: {v!r}")
        return v


# ---------------------------------------------------------------------------
# Main Settings (env vars + .env file)
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables and .env file.
    Secrets like API keys and DB URLs must **never** be committed to config.yaml.
    """

    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Required secrets
    # ------------------------------------------------------------------
    gemini_api_key: str = Field(
        ...,
        description="Google Gemini API key – set via GEMINI_API_KEY env var",
    )
    database_url: str = Field(
        ...,
        description="SQLAlchemy-compatible DB URL – set via DATABASE_URL env var",
    )

    # ------------------------------------------------------------------
    # Optional deployment overrides
    # ------------------------------------------------------------------
    app_env: Literal["development", "staging", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8000, ge=1, le=65535)
    config_yaml_path: str = str(_CONFIG_YAML_PATH)

    # ------------------------------------------------------------------
    # Derived: recovery_policy (populated from config.yaml at validation)
    # ------------------------------------------------------------------
    recovery_policy: RecoveryPolicy | None = None

    @model_validator(mode="after")
    def _load_recovery_policy(self) -> Settings:
        """Parse config.yaml and populate recovery_policy after env vars are loaded."""
        yaml_data = _load_yaml(Path(self.config_yaml_path))
        policy_dict = yaml_data.get("recovery_policy", {})
        self.recovery_policy = RecoveryPolicy(**policy_dict)
        return self


# ---------------------------------------------------------------------------
# Cached accessor
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the validated, cached application settings.

    Usage::

        from recovery_platform.config import get_settings

        settings = get_settings()
        print(settings.gemini_api_key)
        print(settings.recovery_policy.max_retries)
    """
    return Settings()
