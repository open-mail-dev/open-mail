import importlib

import config


def test_db_dsn_and_slack_webhook_url_are_stripped(monkeypatch):
    """A trailing newline on a Secret Manager value has broken things before
    (see MAIL_TOKEN_SECRET / AWS creds) — MAIL_DB_DSN and SLACK_WEBHOOK_URL
    must be stripped the same way."""
    monkeypatch.setenv("MAIL_DB_DSN", "postgresql://example/db\n")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/x\n")
    try:
        importlib.reload(config)
        assert config.DB_DSN == "postgresql://example/db"
        assert config.SLACK_WEBHOOK_URL == "https://hooks.slack.example/x"
    finally:
        importlib.reload(config)
