from app.config import Settings


def test_settings_reads_environment_on_each_instance(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///first.db")
    assert Settings().database_url == "sqlite:///first.db"

    monkeypatch.setenv("DATABASE_URL", "sqlite:///second.db")
    monkeypatch.setenv("SEED_DEV_DATA", "true")
    settings = Settings()

    assert settings.database_url == "sqlite:///second.db"
    assert settings.seed_dev_data is True
