"""
Shell preferences bridge.

The Electron desktop shell (electron/main.js) must decide whether to boot to
the tray (minimized) or show a window BEFORE the Flask backend has finished
starting - so it can't read SQLite. Instead the backend mirrors the handful of
shell-relevant preferences into a small JSON file the shell reads synchronously
at startup, following the same data/ sentinel-file convention as
electron-update.request / shutdown.request.

Currently only ``start_minimized`` is mirrored, but the file is a JSON object so
future shell-relevant prefs can be added without changing the contract.
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _shell_prefs_path() -> Path:
    """Absolute path to data/shell-prefs.json in the repo root."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    return repo_root / 'data' / 'shell-prefs.json'


def write_shell_prefs(start_minimized: bool) -> None:
    """Write (or update) the shell-prefs.json file the Electron shell reads.

    Merges into any existing file so unrelated future keys are preserved. Never
    raises - a failure here must not block a preference save.
    """
    path = _shell_prefs_path()
    try:
        current = {}
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding='utf-8')) or {}
            except (ValueError, OSError):
                current = {}
        if not isinstance(current, dict):
            current = {}
        current['start_minimized'] = bool(start_minimized)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, indent=2), encoding='utf-8')
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"Could not write shell-prefs.json: {e}")


def reconcile_shell_prefs(pref) -> None:
    """Sync the shell-prefs file to the stored preference (DB is source of truth).

    Called on boot so the file self-heals after a restore, manual edit, or a
    version that predates the file. ``pref`` may be None (no prefs row yet), in
    which case we write the default (not minimized).
    """
    start_minimized = bool(getattr(pref, 'start_minimized', False)) if pref else False
    write_shell_prefs(start_minimized)
