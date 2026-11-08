"""Bootstrap can be disabled without touching the database or hashing a password."""

from unittest.mock import Mock

from app.cli import seed as bootstrap
from app.core.config import Settings


async def test_unconfigured_bootstrap_skips_database_and_password_work(monkeypatch, capsys):
    settings = Settings(
        _env_file=None,
        database_url="mysql+asyncmy://test:test@localhost/bootstrap_test",
        jwt_secret="bootstrap-unit-test-secret-at-least-thirty-two-characters",
        bootstrap_admin_email=None,
        bootstrap_admin_password=None,
    )
    database = Mock(side_effect=AssertionError("No database is needed without bootstrap"))
    hash_password = Mock(side_effect=AssertionError("No password should be hashed"))
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)
    monkeypatch.setattr(bootstrap, "create_database", database)
    monkeypatch.setattr(bootstrap, "hash_password", hash_password)

    await bootstrap.seed()

    database.assert_not_called()
    hash_password.assert_not_called()
    assert "seed skipped" in capsys.readouterr().out
