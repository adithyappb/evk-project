"""Application configuration, loaded from environment variables.

EVK runs in one of two modes:

* **local**  — every external dependency is stubbed with a local equivalent:
  file-backed Firestore, file-logged Inkbox "sends", and a pattern-matching
  Gemini stub. Zero real credentials needed; perfect for dev and demos.
  If `GOOGLE_API_KEY` is set, the stub is upgraded to the real Gemini
  Developer API (free tier, no GCP project required).

* **production** — uses real Inkbox, real Vertex AI Gemini, and real
  Firestore. Requires all corresponding env vars.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Mode --------------------------------------------------------------
    evk_mode: Literal["local", "production"] = "local"

    # ---- Inkbox ------------------------------------------------------------
    # In local mode these can be any placeholder values.
    inkbox_api_key: str = "ApiKey_local_stub"
    inkbox_agent_handle: str = "evk-agent"
    inkbox_signing_key: str = "whsec_local_stub_signing_key_change_me"
    inkbox_webhook_tolerance_seconds: int = 300

    # ---- Gemini ------------------------------------------------------------
    # Option A: Vertex AI — requires project + creds (production mode)
    google_cloud_project: str = "evk-local"
    google_cloud_location: str = "us-central1"
    # Option B: Gemini Developer API — free key from ai.google.dev (local mode)
    google_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"

    # ---- Firestore (production mode only) ---------------------------------
    firestore_project: str | None = None
    firestore_database: str = "(default)"

    # ---- Local mode data dir -----------------------------------------------
    # Where file-backed stores live; created on first use.
    local_data_dir: str = "./data"

    # ---- App ---------------------------------------------------------------
    app_env: Literal["dev", "staging", "prod"] = "dev"
    app_host: str = "0.0.0.0"
    app_port: int = 8080
    app_log_level: str = "INFO"
    require_approval: bool = True
    admin_base_url: str = "http://localhost:8080"
    admin_email: str = "admin@example.com"
    reminder_days_before_raw: str = Field(default="7,2", alias="reminder_days_before")
    session_cookie_name: str = "evk_session"
    session_ttl_hours: int = 12
    login_code_ttl_minutes: int = 10
    auth_local_demo_password: str = "ChangeMe123!"
    auth_smtp_host: str = "localhost"
    auth_smtp_port: int = 1025
    auth_smtp_username: str = ""
    auth_smtp_password: str = ""
    auth_smtp_sender: str = "login@evkids.local"
    auth_email_delivery_mode: Literal["terminal", "smtp"] = "terminal"

    # ---- Admin auth (optional bearer token; if empty, no auth enforced) ----
    admin_api_token: str | None = None

    # ---- Privacy / pseudonymisation ---------------------------------------
    # Salt used for hashing student IDs before they're sent to Gemini. Rotate
    # periodically; doing so invalidates any prior IDs (by design).
    privacy_salt: str = "evk-local-dev-salt-rotate-me"

    # ---- Delivery --------------------------------------------------------
    # Per-mandate: Gmail batch ≤ 45 recipients, 0.2 s delay between sends.
    delivery_batch_size: int = Field(default=45, ge=1, le=45)
    delivery_delay_seconds: float = Field(default=0.2, ge=0.0)
    # Soft daily quota per mailbox — defensive guard, trips a circuit breaker.
    delivery_daily_quota: int = 2000

    # ---- Classifier threshold --------------------------------------------
    classifier_min_confidence: float = 0.75  # publish-through threshold

    # ---- Dedup -----------------------------------------------------------
    dedup_deadline_window_days: int = 30  # pre-filter before any fuzzy match

    @computed_field  # type: ignore[misc]
    @property
    def reminder_days_before(self) -> list[int]:
        return [int(x.strip()) for x in self.reminder_days_before_raw.split(",") if x.strip()]

    @property
    def effective_firestore_project(self) -> str:
        return self.firestore_project or self.google_cloud_project

    @property
    def is_local(self) -> bool:
        return self.evk_mode == "local"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
