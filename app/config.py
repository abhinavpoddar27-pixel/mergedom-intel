"""
Configuration module — loads all environment variables via pydantic-settings.

Settings are organised into nested groups (Singular, Meta, Slack, …) for
clarity.  A cached ``get_settings()`` accessor is the recommended entry-point;
the module-level ``settings`` singleton is kept for convenience and backward
compatibility.

On startup every critical API key is checked — empty keys emit a warning
via the standard logging module but do **not** crash the process so that
services can gracefully degrade.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


# ── Nested setting groups ────────────────────────────────────────────────────

class SingularSettings(BaseModel):
    api_key: str = ""


class MetaSettings(BaseModel):
    app_id: str = ""
    app_secret: str = ""
    access_token: str = ""
    ad_account_id: str = ""


class AppLovinSettings(BaseModel):
    api_key: str = ""
    sdk_key: str = ""


class GoogleAdsSettings(BaseModel):
    developer_token: str = ""
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    customer_id: str = ""


class MaxSettings(BaseModel):
    api_key: str = ""


class SlackSettings(BaseModel):
    bot_token: str = ""
    app_token: str = ""
    signing_secret: str = ""


class GmailSettings(BaseModel):
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    user_email: str = ""


class AirtableSettings(BaseModel):
    api_key: str = ""
    base_id: str = ""


class ClaudeSettings(BaseModel):
    model_config = {"protected_namespaces": ()}

    api_key: str = ""
    model_daily: str = "claude-sonnet-4-20250514"
    model_parse: str = "claude-haiku-4-5-20251001"


class AppSettings(BaseModel):
    timezone: str = "Asia/Kolkata"
    daily_brief_hour: int = 6
    daily_brief_minute: int = 35
    anomaly_check_interval_hours: int = 2
    max_alerts_per_day: int = 5
    anomaly_std_dev_threshold: float = 1.5
    natasha_slack_user_id: str = ""
    natasha_email: str = ""


# ── Root settings (flat env → nested groups) ─────────────────────────────────

class Settings(BaseSettings):
    """Application-wide settings sourced from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Singular ---
    singular_api_key: str = ""

    # --- Meta ---
    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_access_token: str = ""
    meta_ad_account_id: str = ""

    # --- AppLovin ---
    applovin_api_key: str = ""
    applovin_sdk_key: str = ""

    # --- Google Ads ---
    google_ads_developer_token: str = ""
    google_ads_client_id: str = ""
    google_ads_client_secret: str = ""
    google_ads_refresh_token: str = ""
    google_ads_customer_id: str = ""

    # --- MAX ---
    max_api_key: str = ""

    # --- Slack ---
    slack_bot_token: str = ""
    slack_app_token: str = ""
    slack_signing_secret: str = ""

    # --- Gmail ---
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""
    gmail_user_email: str = ""

    # --- Airtable ---
    airtable_api_key: str = ""
    airtable_base_id: str = ""

    # --- Anthropic / Claude ---
    anthropic_api_key: str = ""
    claude_model_daily: str = "claude-sonnet-4-20250514"
    claude_model_parse: str = "claude-haiku-4-5-20251001"

    # --- App-level ---
    natasha_slack_user_id: str = ""
    natasha_email: str = ""
    app_timezone: str = "Asia/Kolkata"
    daily_brief_hour: int = 6
    daily_brief_minute: int = 35
    anomaly_check_interval_hours: int = 2
    max_alerts_per_day: int = 5
    anomaly_std_dev_threshold: float = 1.5

    # ── Nested accessors ─────────────────────────────────────────────────

    @property
    def singular(self) -> SingularSettings:
        return SingularSettings(api_key=self.singular_api_key)

    @property
    def meta(self) -> MetaSettings:
        return MetaSettings(
            app_id=self.meta_app_id,
            app_secret=self.meta_app_secret,
            access_token=self.meta_access_token,
            ad_account_id=self.meta_ad_account_id,
        )

    @property
    def applovin(self) -> AppLovinSettings:
        return AppLovinSettings(
            api_key=self.applovin_api_key,
            sdk_key=self.applovin_sdk_key,
        )

    @property
    def google_ads(self) -> GoogleAdsSettings:
        return GoogleAdsSettings(
            developer_token=self.google_ads_developer_token,
            client_id=self.google_ads_client_id,
            client_secret=self.google_ads_client_secret,
            refresh_token=self.google_ads_refresh_token,
            customer_id=self.google_ads_customer_id,
        )

    @property
    def max(self) -> MaxSettings:
        return MaxSettings(api_key=self.max_api_key)

    @property
    def slack(self) -> SlackSettings:
        return SlackSettings(
            bot_token=self.slack_bot_token,
            app_token=self.slack_app_token,
            signing_secret=self.slack_signing_secret,
        )

    @property
    def gmail(self) -> GmailSettings:
        return GmailSettings(
            client_id=self.gmail_client_id,
            client_secret=self.gmail_client_secret,
            refresh_token=self.gmail_refresh_token,
            user_email=self.gmail_user_email,
        )

    @property
    def airtable(self) -> AirtableSettings:
        return AirtableSettings(
            api_key=self.airtable_api_key,
            base_id=self.airtable_base_id,
        )

    @property
    def claude(self) -> ClaudeSettings:
        return ClaudeSettings(
            api_key=self.anthropic_api_key,
            model_daily=self.claude_model_daily,
            model_parse=self.claude_model_parse,
        )

    @property
    def app(self) -> AppSettings:
        return AppSettings(
            timezone=self.app_timezone,
            daily_brief_hour=self.daily_brief_hour,
            daily_brief_minute=self.daily_brief_minute,
            anomaly_check_interval_hours=self.anomaly_check_interval_hours,
            max_alerts_per_day=self.max_alerts_per_day,
            anomaly_std_dev_threshold=self.anomaly_std_dev_threshold,
            natasha_slack_user_id=self.natasha_slack_user_id,
            natasha_email=self.natasha_email,
        )


# ── Critical-key validation ──────────────────────────────────────────────────

_CRITICAL_KEYS: list[tuple[str, str]] = [
    ("singular_api_key", "Singular"),
    ("meta_access_token", "Meta Ads"),
    ("applovin_api_key", "AppLovin"),
    ("google_ads_developer_token", "Google Ads"),
    ("max_api_key", "MAX Mediation"),
    ("slack_bot_token", "Slack"),
    ("gmail_refresh_token", "Gmail"),
    ("airtable_api_key", "Airtable"),
    ("anthropic_api_key", "Claude / Anthropic"),
]


def _warn_empty_keys(s: Settings) -> None:
    for attr, label in _CRITICAL_KEYS:
        if not getattr(s, attr, ""):
            logger.warning("Missing API key for %s — service will degrade gracefully", label)


# ── Cached accessor ──────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the application settings singleton (cached after first call)."""
    s = Settings()
    _warn_empty_keys(s)
    return s


# Module-level convenience alias (backward-compatible).
settings = get_settings()
