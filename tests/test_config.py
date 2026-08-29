"""
tests/test_config.py
====================
Phase 1 smoke tests: verify config loads correctly and all required
keys are present and valid without any actual secrets.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make sure the project root is importable regardless of how pytest is invoked
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_ENV = {
    "GEMINI_API_KEY": "test-gemini-key-abc123",
    "DATABASE_URL": "sqlite:///./test_recovery.db",
    "APP_ENV": "development",
    "LOG_LEVEL": "DEBUG",
}


def _make_settings():
    """Create a fresh Settings instance with fake env vars (no .env file needed)."""
    from recovery_platform.config import Settings

    with patch.dict(os.environ, FAKE_ENV, clear=False):
        # Clear the lru_cache so we always get a fresh instance in tests
        from recovery_platform import config as cfg_module

        cfg_module.get_settings.cache_clear()
        return Settings()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSettingsLoading:
    def test_settings_loads_without_error(self):
        """Settings must instantiate successfully with valid env vars."""
        settings = _make_settings()
        assert settings is not None

    def test_required_secrets_present(self):
        """GEMINI_API_KEY and DATABASE_URL must be non-empty strings."""
        settings = _make_settings()
        assert settings.gemini_api_key, "GEMINI_API_KEY must not be empty"
        assert settings.database_url, "DATABASE_URL must not be empty"

    def test_default_app_env(self):
        settings = _make_settings()
        assert settings.app_env in {"development", "staging", "production"}

    def test_default_log_level(self):
        settings = _make_settings()
        assert settings.log_level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

    def test_api_port_in_valid_range(self):
        settings = _make_settings()
        assert 1 <= settings.api_port <= 65535


class TestRecoveryPolicy:
    def test_policy_is_populated(self):
        """recovery_policy must be loaded from config.yaml."""
        settings = _make_settings()
        assert settings.recovery_policy is not None

    def test_max_retries_positive(self):
        settings = _make_settings()
        assert settings.recovery_policy.max_retries >= 1

    def test_max_retries_value(self):
        """config.yaml sets max_retries: 3."""
        settings = _make_settings()
        assert settings.recovery_policy.max_retries == 3

    def test_risk_hold_action(self):
        """config.yaml sets risk_hold_action: escalate_to_human."""
        settings = _make_settings()
        assert settings.recovery_policy.risk_hold_action == "escalate_to_human"

    def test_discount_min_tier(self):
        settings = _make_settings()
        assert settings.recovery_policy.discount_eligibility.min_tier == "enterprise"

    def test_discount_max_pct(self):
        settings = _make_settings()
        assert settings.recovery_policy.discount_eligibility.max_pct == 15

    def test_retry_delays_soft_decline(self):
        settings = _make_settings()
        assert settings.recovery_policy.retry_delays.soft_decline_min == 60

    def test_retry_delays_technical_failure(self):
        settings = _make_settings()
        assert settings.recovery_policy.retry_delays.technical_failure_min == 15

    def test_retry_strategies_keys_exist(self):
        settings = _make_settings()
        strategies = settings.recovery_policy.retry_strategies
        assert isinstance(strategies.soft_decline, list)
        assert isinstance(strategies.hard_decline, list)
        assert isinstance(strategies.technical_failure, list)

    def test_retry_strategies_non_empty(self):
        settings = _make_settings()
        strategies = settings.recovery_policy.retry_strategies
        assert len(strategies.soft_decline) > 0
        assert len(strategies.hard_decline) > 0
        assert len(strategies.technical_failure) > 0


class TestMissingSecrets:
    def test_missing_gemini_key_raises(self):
        """Settings must raise ValidationError when GEMINI_API_KEY is absent."""
        from pydantic import ValidationError

        env_without_key = {k: v for k, v in FAKE_ENV.items() if k != "GEMINI_API_KEY"}
        env_without_key.pop("GEMINI_API_KEY", None)

        # Remove from os.environ if present
        with patch.dict(os.environ, {}, clear=True):
            for k, v in env_without_key.items():
                os.environ[k] = v
            os.environ.pop("GEMINI_API_KEY", None)

            from recovery_platform import config as cfg_module
            cfg_module.get_settings.cache_clear()

            with pytest.raises((ValidationError, Exception)):
                from recovery_platform.config import Settings
                Settings(_env_file=None)

    def test_missing_database_url_raises(self):
        """Settings must raise ValidationError when DATABASE_URL is absent."""
        from pydantic import ValidationError

        with patch.dict(os.environ, {}, clear=True):
            os.environ["GEMINI_API_KEY"] = "test-key"
            os.environ.pop("DATABASE_URL", None)

            from recovery_platform import config as cfg_module
            cfg_module.get_settings.cache_clear()

            with pytest.raises((ValidationError, Exception)):
                from recovery_platform.config import Settings
                Settings(_env_file=None)

