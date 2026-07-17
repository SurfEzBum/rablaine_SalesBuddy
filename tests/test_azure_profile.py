"""Tests for the isolated Azure CLI profile bootstrap (app/services/azure_profile.py)."""
import os

import pytest

from app.services import azure_profile as ap


@pytest.fixture(autouse=True)
def _restore_env():
    """Restore env vars that ensure_azure_profile() mutates directly."""
    keys = ('AZURE_CONFIG_DIR', 'SALESBUDDY_HOME', 'FLASK_ENV')
    saved = {k: os.environ.get(k) for k in keys}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_derive_config_dir_development(monkeypatch, tmp_path):
    monkeypatch.setenv('FLASK_ENV', 'development')
    monkeypatch.setattr(os.path, 'expanduser', lambda _p: str(tmp_path))
    home, cfg = ap._derive_config_dir()
    assert home.name == 'SalesBuddyDev'
    assert cfg == home / '.azure'


def test_derive_config_dir_production(monkeypatch, tmp_path):
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.setattr(os.path, 'expanduser', lambda _p: str(tmp_path))
    home, _cfg = ap._derive_config_dir()
    assert home.name == 'SalesBuddy'


@pytest.mark.skipif(os.name != 'nt', reason='WAM broker is Windows-only')
def test_disable_broker_creates_and_is_idempotent(tmp_path):
    cfg = tmp_path / '.azure'
    assert ap._disable_broker(cfg) is True
    content = (cfg / 'config').read_text(encoding='utf-8')
    assert '[core]' in content
    assert 'enable_broker_on_windows = false' in content

    ap._disable_broker(cfg)  # second run
    content2 = (cfg / 'config').read_text(encoding='utf-8')
    assert content2.count('enable_broker_on_windows') == 1


@pytest.mark.skipif(os.name != 'nt', reason='WAM broker is Windows-only')
def test_disable_broker_strips_existing_true(tmp_path):
    cfg = tmp_path / '.azure'
    cfg.mkdir()
    (cfg / 'config').write_text(
        '[core]\nenable_broker_on_windows = true\nother = 1\n', encoding='utf-8'
    )
    ap._disable_broker(cfg)
    content = (cfg / 'config').read_text(encoding='utf-8')
    assert 'enable_broker_on_windows = false' in content
    assert 'enable_broker_on_windows = true' not in content
    assert 'other = 1' in content


def test_migrate_copies_when_isolated_empty(tmp_path, monkeypatch):
    default = tmp_path / '.azure'
    default.mkdir()
    (default / 'azureProfile.json').write_text('{}', encoding='utf-8')
    monkeypatch.setattr(ap, '_default_azure_dir', lambda: default)

    iso = tmp_path / 'SalesBuddyDev' / '.azure'
    assert ap._migrate_default_creds(iso) is True
    assert (iso / 'azureProfile.json').exists()


def test_migrate_skips_when_isolated_populated(tmp_path, monkeypatch):
    default = tmp_path / '.azure'
    default.mkdir()
    (default / 'x').write_text('1', encoding='utf-8')
    monkeypatch.setattr(ap, '_default_azure_dir', lambda: default)

    iso = tmp_path / 'iso'
    iso.mkdir()
    (iso / 'existing').write_text('keep', encoding='utf-8')

    assert ap._migrate_default_creds(iso) is False
    assert not (iso / 'x').exists()  # not overwritten
    assert (iso / 'existing').exists()


def test_ensure_sets_config_dir_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv('AZURE_CONFIG_DIR', raising=False)
    monkeypatch.setenv('FLASK_ENV', 'development')
    monkeypatch.setattr(os.path, 'expanduser', lambda _p: str(tmp_path))
    monkeypatch.setattr(ap, '_default_azure_dir', lambda: tmp_path / 'nonexistent')

    ap.ensure_azure_profile()

    assert os.environ.get('AZURE_CONFIG_DIR', '').endswith(
        os.path.join('SalesBuddyDev', '.azure')
    )


def test_ensure_respects_existing_config_dir(tmp_path, monkeypatch):
    target = tmp_path / 'custom' / '.azure'
    monkeypatch.setenv('AZURE_CONFIG_DIR', str(target))
    monkeypatch.setattr(ap, '_default_azure_dir', lambda: tmp_path / 'nope')

    ap.ensure_azure_profile()

    assert os.environ['AZURE_CONFIG_DIR'] == str(target)
