"""
Tests for the "start minimized" preference, the shell-prefs bridge file, the
Electron shell-rebuild detection, and the rebuild-shell endpoint.
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SHELL_PREFS = REPO_ROOT / 'data' / 'shell-prefs.json'
REBUILD_SENTINEL = REPO_ROOT / 'data' / 'electron-rebuild.request'


# ---------------------------------------------------------------------------
# Migration / model
# ---------------------------------------------------------------------------

class TestStartMinimizedColumn:
    def test_column_exists(self, app):
        """The start_minimized column is created by the migration."""
        from sqlalchemy import inspect
        from app.models import db
        with app.app_context():
            cols = [c['name'] for c in inspect(db.engine).get_columns('user_preferences')]
            assert 'start_minimized' in cols

    def test_defaults_false(self, app):
        from app.models import UserPreference
        with app.app_context():
            pref = UserPreference()
            # SQLAlchemy default applies on flush; the Python-side default is False.
            assert (pref.start_minimized or False) is False


# ---------------------------------------------------------------------------
# API round-trip + shell-prefs bridge
# ---------------------------------------------------------------------------

class TestStartMinimizedApi:
    def test_get_defaults_false(self, client):
        resp = client.get('/api/preferences/start-minimized')
        assert resp.status_code == 200
        assert resp.get_json()['start_minimized'] is False

    def test_post_persists_and_writes_bridge(self, client, app):
        if SHELL_PREFS.exists():
            SHELL_PREFS.unlink()
        try:
            resp = client.post(
                '/api/preferences/start-minimized',
                json={'start_minimized': True},
            )
            assert resp.status_code == 200
            assert resp.get_json()['start_minimized'] is True

            # Persisted in the DB.
            from app.models import UserPreference
            with app.app_context():
                assert UserPreference.query.first().start_minimized is True

            # Mirrored into the shell-prefs bridge file.
            assert SHELL_PREFS.exists()
            data = json.loads(SHELL_PREFS.read_text(encoding='utf-8'))
            assert data['start_minimized'] is True
        finally:
            if SHELL_PREFS.exists():
                SHELL_PREFS.unlink()

    def test_post_false_clears_bridge_flag(self, client):
        try:
            client.post('/api/preferences/start-minimized', json={'start_minimized': True})
            client.post('/api/preferences/start-minimized', json={'start_minimized': False})
            data = json.loads(SHELL_PREFS.read_text(encoding='utf-8'))
            assert data['start_minimized'] is False
        finally:
            if SHELL_PREFS.exists():
                SHELL_PREFS.unlink()


class TestShellPrefsWriter:
    def test_write_merges_existing_keys(self):
        from app.services.shell_prefs import write_shell_prefs
        try:
            SHELL_PREFS.parent.mkdir(parents=True, exist_ok=True)
            SHELL_PREFS.write_text(json.dumps({'future_key': 42}), encoding='utf-8')
            write_shell_prefs(True)
            data = json.loads(SHELL_PREFS.read_text(encoding='utf-8'))
            assert data['start_minimized'] is True
            assert data['future_key'] == 42  # unrelated keys preserved
        finally:
            if SHELL_PREFS.exists():
                SHELL_PREFS.unlink()

    def test_write_recovers_from_corrupt_file(self):
        from app.services.shell_prefs import write_shell_prefs
        try:
            SHELL_PREFS.parent.mkdir(parents=True, exist_ok=True)
            SHELL_PREFS.write_text('not json{{{', encoding='utf-8')
            write_shell_prefs(True)
            data = json.loads(SHELL_PREFS.read_text(encoding='utf-8'))
            assert data['start_minimized'] is True
        finally:
            if SHELL_PREFS.exists():
                SHELL_PREFS.unlink()


# ---------------------------------------------------------------------------
# Changelog shell-rebuild marker detection
# ---------------------------------------------------------------------------

class TestShellRebuildMarker:
    def test_bullet_marker_variants(self):
        from app.services.update_checker import _bullet_flags_shell_rebuild
        assert _bullet_flags_shell_rebuild('*Electron Shell Update* - did a thing')
        assert _bullet_flags_shell_rebuild('electron shell update - lowercase')
        assert _bullet_flags_shell_rebuild('  *Electron Shell Update* spacing')
        assert not _bullet_flags_shell_rebuild('A normal changelog bullet')
        assert not _bullet_flags_shell_rebuild('')

    def test_entries_require_shell_rebuild_true(self):
        from app.services.update_checker import entries_require_shell_rebuild
        entries = [
            {'commit': 'aaaaaaa', 'bullets': ['just a fix']},
            {'commit': 'bbbbbbb', 'bullets': ['*Electron Shell Update* - tray boot']},
        ]
        assert entries_require_shell_rebuild(entries, {'bbbbbbb'}) is True

    def test_entries_require_shell_rebuild_out_of_range(self):
        from app.services.update_checker import entries_require_shell_rebuild
        entries = [
            {'commit': 'bbbbbbb', 'bullets': ['*Electron Shell Update* - tray boot']},
        ]
        # Marker exists but its commit isn't in the pending set -> False.
        assert entries_require_shell_rebuild(entries, {'aaaaaaa'}) is False

    def test_entries_require_shell_rebuild_no_marker(self):
        from app.services.update_checker import entries_require_shell_rebuild
        entries = [{'commit': 'aaaaaaa', 'bullets': ['normal change']}]
        assert entries_require_shell_rebuild(entries, {'aaaaaaa'}) is False


# ---------------------------------------------------------------------------
# Rebuild-shell endpoint (failsafe)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != 'win32', reason='rebuild-shell is desktop/Windows-only')
class TestRebuildShellEndpoint:
    def test_electron_drops_sentinel(self, client):
        if REBUILD_SENTINEL.exists():
            REBUILD_SENTINEL.unlink()
        try:
            with patch.dict(os.environ, {'SALESBUDDY_ELECTRON': '1'}):
                resp = client.post('/api/admin/rebuild-shell')
            assert resp.status_code == 200
            assert resp.get_json()['success'] is True
            assert REBUILD_SENTINEL.exists()
        finally:
            if REBUILD_SENTINEL.exists():
                REBUILD_SENTINEL.unlink()

    def test_rejected_when_not_electron(self, client):
        with patch.dict(os.environ):
            os.environ.pop('SALESBUDDY_ELECTRON', None)
            resp = client.post('/api/admin/rebuild-shell')
        assert resp.status_code == 400
        assert not REBUILD_SENTINEL.exists()
