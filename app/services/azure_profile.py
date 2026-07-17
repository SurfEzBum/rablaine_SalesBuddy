"""
Isolated Azure CLI auth profile bootstrap for Sales Buddy.

Sales Buddy decouples its ``az login`` state from the machine-wide Azure CLI by
pointing the CLI at an isolated per-environment ``AZURE_CONFIG_DIR``. For that
isolation to actually hold, three things must happen on the config dir:

    1. ``AZURE_CONFIG_DIR`` must be set (env var).
    2. Existing machine-wide creds (``~/.azure``) should migrate in on first run.
    3. The Windows WAM broker must be **disabled** in the dir's ``config`` file -
       otherwise refresh tokens live in WAM keyed by ``{client_id, account}`` and
       are shared across every ``AZURE_CONFIG_DIR`` on the machine, so an
       ``az logout`` in any other shell flips the account to
       ``Status_AccountUnusable`` and every MSX/gateway call starts returning 401.

Historically only ``scripts/server.ps1`` did all three. Electron,
``supervisor.ps1``, the batch files, plain ``flask run`` and tests set (at most)
the env var, leaving the broker enabled and silently voiding the isolation.

This module centralizes the logic in Python so it runs on **every** startup
path. It is idempotent and defensive - it never raises.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

_BROKER_SETTING = "enable_broker_on_windows = false"


def _is_windows() -> bool:
    return os.name == "nt"


def _default_azure_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".azure"


def _derive_config_dir() -> Tuple[Path, Path]:
    """Return ``(salesbuddy_home, azure_config_dir)`` derived from FLASK_ENV.

    Mirrors ``scripts/server.ps1``: development uses ``SalesBuddyDev``,
    everything else uses ``SalesBuddy``, both under the user profile.
    """
    flask_env = os.environ.get("FLASK_ENV", "production").strip().lower()
    profile_folder = "SalesBuddyDev" if flask_env == "development" else "SalesBuddy"
    home = Path(os.path.expanduser("~")) / profile_folder
    return home, home / ".azure"


def _dir_is_empty(path: Path) -> bool:
    try:
        return not any(path.iterdir())
    except Exception:
        return True


def _migrate_default_creds(config_dir: Path) -> bool:
    """Copy the machine-wide ``~/.azure`` into the isolated dir on first run.

    Only runs when the isolated dir is missing or empty and a populated default
    ``~/.azure`` exists. Never overwrites a populated isolated dir. Returns True
    if a migration was performed.
    """
    if config_dir.exists() and not _dir_is_empty(config_dir):
        return False
    default_dir = _default_azure_dir()
    if not default_dir.exists() or _dir_is_empty(default_dir):
        return False
    try:
        shutil.copytree(default_dir, config_dir, dirs_exist_ok=True)
        logger.info("Migrated az credentials from %s into %s", default_dir, config_dir)
        return True
    except Exception:
        logger.warning("Could not migrate az credentials into %s", config_dir,
                       exc_info=True)
        return False


def _disable_broker(config_dir: Path) -> bool:
    """Ensure the isolated az ``config`` disables the WAM broker (Windows only).

    Idempotent: strips any existing ``enable_broker_on_windows`` line, ensures a
    ``[core]`` section, and places the setting under it. Returns True if the
    broker is (now) disabled or not applicable (non-Windows).
    """
    if not _is_windows():
        return True
    config_file = config_dir / "config"
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        lines = []
        if config_file.exists():
            lines = config_file.read_text(encoding="utf-8", errors="replace").splitlines()
            already = any(
                l.strip().lower().replace(" ", "") == "enable_broker_on_windows=false"
                for l in lines
            )
            if already:
                return True

        lines = [
            l for l in lines
            if not l.strip().lower().startswith("enable_broker_on_windows")
        ]

        has_core = any(l.strip().lower() == "[core]" for l in lines)
        if not has_core:
            if lines and lines[-1].strip() != "":
                lines.append("")
            lines.append("[core]")
            lines.append(_BROKER_SETTING)
        else:
            rebuilt = []
            inserted = False
            for l in lines:
                rebuilt.append(l)
                if not inserted and l.strip().lower() == "[core]":
                    rebuilt.append(_BROKER_SETTING)
                    inserted = True
            lines = rebuilt

        config_file.write_text("\n".join(lines) + "\n", encoding="ascii",
                               errors="replace")
        logger.info("Disabled az WAM broker in %s", config_file)
        return True
    except Exception:
        logger.warning("Could not disable az broker in %s", config_file, exc_info=True)
        return False


def ensure_azure_profile() -> None:
    """Bootstrap the isolated Azure CLI auth profile. Idempotent, never raises.

    Resolves ``AZURE_CONFIG_DIR`` (respecting an already-set value, otherwise
    deriving it from ``FLASK_ENV`` and setting it), migrates existing creds on
    first run, disables the WAM broker, and logs the resolved auth state. Safe
    to call on every startup path (Electron, supervisor, bare ``flask run``).
    """
    try:
        env_dir = os.environ.get("AZURE_CONFIG_DIR")
        if env_dir:
            config_dir = Path(env_dir)
            source = "AZURE_CONFIG_DIR"
        else:
            home, config_dir = _derive_config_dir()
            os.environ["AZURE_CONFIG_DIR"] = str(config_dir)
            os.environ.setdefault("SALESBUDDY_HOME", str(home))
            source = "derived from FLASK_ENV"

        migrated = _migrate_default_creds(config_dir)
        broker_disabled = _disable_broker(config_dir)

        logger.info(
            "Azure profile ready: dir=%s (%s) exists=%s migrated=%s broker_disabled=%s",
            config_dir, source, config_dir.exists(), migrated, broker_disabled,
        )
    except Exception:
        logger.warning(
            "Azure profile bootstrap failed - az CLI may fall back to the "
            "machine-wide login (isolation disabled)", exc_info=True,
        )
