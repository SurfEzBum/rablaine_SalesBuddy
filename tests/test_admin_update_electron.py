"""
Tests for the Electron-managed update delegation in /api/admin/update-apply.

Under the Electron shell the route must NOT spawn server.ps1 (that would race
with Electron's own supervisor-restart handler). Instead it drops a sentinel
file the shell watches for.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SENTINEL = REPO_ROOT / 'data' / 'electron-update.request'

pytestmark = pytest.mark.skipif(
    sys.platform != 'win32', reason='update-apply is Windows-only'
)


class TestUpdateApplyElectron:
    """POST /api/admin/update-apply under the Electron shell."""

    def test_electron_writes_sentinel_and_skips_server_script(self, client):
        """With SALESBUDDY_ELECTRON set, drop the sentinel, don't spawn ps1."""
        if SENTINEL.exists():
            SENTINEL.unlink()

        with patch.dict(os.environ, {'SALESBUDDY_ELECTRON': '1'}), \
                patch('app.routes.admin.subprocess.Popen') as mock_popen, \
                patch('app.routes.admin.threading.Timer') as mock_timer:
            resp = client.post('/api/admin/update-apply')

        try:
            assert resp.status_code == 200
            assert resp.get_json()['success'] is True
            mock_popen.assert_not_called()
            mock_timer.assert_not_called()
            assert SENTINEL.exists()
        finally:
            if SENTINEL.exists():
                SENTINEL.unlink()

    def test_non_electron_spawns_server_script(self, client):
        """Without SALESBUDDY_ELECTRON, fall back to the server.ps1 path."""
        with patch.dict(os.environ):
            os.environ.pop('SALESBUDDY_ELECTRON', None)
            with patch('app.routes.admin.subprocess.Popen') as mock_popen, \
                    patch('app.routes.admin.threading.Timer') as mock_timer:
                resp = client.post('/api/admin/update-apply')

        assert resp.status_code == 200
        mock_popen.assert_called_once()
        # No sentinel should be written on the monolithic path.
        assert not SENTINEL.exists()


class TestMigrateToElectron:
    """POST /api/admin/migrate-to-electron - the 'Move to desktop app' button."""

    def test_launches_visible_console_when_flask(self, client):
        """In Flask mode, spawns the migration script in a new console."""
        with patch.dict(os.environ):
            os.environ.pop('SALESBUDDY_ELECTRON', None)
            with patch('app.routes.admin.subprocess.Popen') as mock_popen:
                resp = client.post('/api/admin/migrate-to-electron')

        assert resp.status_code == 200
        assert resp.get_json()['success'] is True
        mock_popen.assert_called_once()
        # Launched in its own visible console window (CREATE_NEW_CONSOLE = 0x10).
        _, kwargs = mock_popen.call_args
        assert kwargs.get('creationflags', 0) & 0x00000010

    def test_rejected_when_already_electron(self, client):
        """Under the Electron shell, the migrate endpoint is a no-op 400."""
        with patch.dict(os.environ, {'SALESBUDDY_ELECTRON': '1'}), \
                patch('app.routes.admin.subprocess.Popen') as mock_popen:
            resp = client.post('/api/admin/migrate-to-electron')

        assert resp.status_code == 400
        mock_popen.assert_not_called()

