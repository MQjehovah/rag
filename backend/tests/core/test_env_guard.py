import logging

import pytest

from app.core.env_guard import WEAK_VALUES, is_production, require_secret


def test_production_拒绝弱值(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        require_secret("JWT_SECRET_KEY", "change-me-in-production")


def test_production_拒绝带空白的弱值(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        require_secret("JWT_SECRET_KEY", " change-me ")


def test_production_拒绝空值(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError):
        require_secret("MINIO_SECRET_KEY", "")


def test_development_放行但告警(monkeypatch, caplog):
    monkeypatch.setenv("APP_ENV", "development")
    with caplog.at_level(logging.WARNING):
        assert require_secret("JWT_SECRET_KEY", "change-me-in-production") == "change-me-in-production"
    assert any(
        record.levelname == "WARNING" and "JWT_SECRET_KEY" in record.getMessage()
        for record in caplog.records
    )


def test_正常值直接返回(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    assert require_secret("JWT_SECRET_KEY", "a-very-long-random-secret") == "a-very-long-random-secret"


def test_is_production_识别(monkeypatch):
    for v in ("production", "PROD", "Prod"):
        monkeypatch.setenv("APP_ENV", v)
        assert is_production() is True
    monkeypatch.setenv("APP_ENV", "development")
    assert is_production() is False


def test_弱值清单包含已知默认值():
    for weak in ("change-me-in-production", "xzyz2022!", "123456", "default-key"):
        assert weak in WEAK_VALUES
