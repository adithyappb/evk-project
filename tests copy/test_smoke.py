"""Import smoke tests — catches circular imports & missing deps early."""

from __future__ import annotations


def test_import_all_modules():
    import evk
    import evk.agents
    import evk.agents.classifier
    import evk.agents.distributor
    import evk.agents.ingestion
    import evk.agents.personalizer
    import evk.agents.reminder
    import evk.api
    import evk.cli
    import evk.config
    import evk.firestore_repo
    import evk.gemini_client
    import evk.inkbox_client
    import evk.logging
    import evk.models
    import evk.seed  # noqa: F401


def test_settings_load_with_defaults():
    from evk.config import get_settings

    s = get_settings()
    assert s.inkbox_api_key == "ApiKey_test"
    assert s.reminder_days_before == [7, 2]
    assert s.effective_firestore_project == "test-project"


def test_cli_app_constructs():
    from evk.cli import app

    assert app is not None
