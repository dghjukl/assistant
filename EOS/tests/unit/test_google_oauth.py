from __future__ import annotations

from pathlib import Path

import pytest


def test_configure_fails_fast_when_default_credential_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    from core import google_oauth

    with pytest.raises(FileNotFoundError, match="config/google/\\*\\.json"):
        google_oauth.configure({"google": {"enabled": True}})


def test_configure_accepts_default_config_google_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    secret_dir = tmp_path / "config" / "google"
    secret_dir.mkdir(parents=True)
    secret_path = secret_dir / "client_secret_test.json"
    secret_path.write_text("{}", encoding="utf-8")

    from core import google_oauth

    google_oauth.configure({"google": {"enabled": True}})

    assert google_oauth.validate_client_secret_path() == secret_path.resolve()


def test_configure_accepts_explicit_client_secret_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    secret_path = tmp_path / "secrets" / "google-oauth.json"
    secret_path.parent.mkdir(parents=True)
    secret_path.write_text("{}", encoding="utf-8")

    from core import google_oauth

    google_oauth.configure(
        {"google": {"enabled": True, "client_secret_path": str(secret_path)}}
    )

    assert google_oauth.validate_client_secret_path() == secret_path
