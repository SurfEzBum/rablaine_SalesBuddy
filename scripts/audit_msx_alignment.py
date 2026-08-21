"""Read-only audit of what an MSX account sync would pull for the current user.

Mirrors the discovery phases of the real sync (``/api/msx/accounts/sync``) WITHOUT
writing anything to the database. Use this to verify a tentative alignment before
committing it: it reports the sellers and territories you're aligned to plus the
number of accounts the sync sees.

What it does (all read-only):
    Phase 1: scan_init()               -> your account assignments (msp_accountteams)
    Phase 2: batch_query_accounts      -> account name, TPID, territory, owner
    Phase 3: batch_query_territories   -> territory names / ATU
    Phase 4: batch_query_account_teams -> sellers aligned to those accounts

Usage:
    python scripts/audit_msx_alignment.py

Requires you to be signed in to Azure (same `az login` the wizard uses). No data
is written to salesbuddy.db - this only reads from MSX and prints a report.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow `from app...` imports when run as a plain script.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.services.msx_auth import get_msx_token  # noqa: E402
from app.services.msx_api import (  # noqa: E402
    scan_init,
    batch_query_accounts,
    batch_query_territories,
    batch_query_account_teams,
)

_ACCT_BATCH = 15
_TEAM_BATCH = 3


def _batched(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def run_audit() -> int:
    """Run the read-only alignment audit. Returns a process exit code."""
    token = get_msx_token()
    if not token:
        print("ERROR: Not signed in to MSX. Run `az login` (same as wizard Step 2) first.")
        return 1

    # ------------------------------------------------------------------
    # Phase 1: scan_init -> account assignments (msp_accountteams)
    # ------------------------------------------------------------------
    print("Phase 1: Fetching your account assignments from MSX...")
    init_result = scan_init()
    if not init_result.get("success"):
        print(f"ERROR: {init_result.get('error', 'Failed to initialize scan')}")
        return 1

    account_ids = init_result.get("account_ids", [])
    user_info = init_result.get("user", {})
    role = init_result.get("role", "Unknown")
    user_name = user_info.get("name", "Unknown")

    print(f"  User: {user_name}")
    print(f"  Role: {role}")
    print(f"  Account assignments found: {len(account_ids)}")

    if not account_ids:
        print("\nNo accounts found for this user. Nothing would be synced.")
        return 0

    # ------------------------------------------------------------------
    # Phase 2: query account details -> territory + TPID
    # ------------------------------------------------------------------
    print("\nPhase 2: Querying account details...")
    accounts_raw: dict = {}
    for batch in _batched(account_ids, _ACCT_BATCH):
        result = batch_query_accounts(batch, batch_size=len(batch))
        if result.get("success"):
            accounts_raw.update(result.get("accounts", {}))
    print(f"  Accounts retrieved: {len(accounts_raw)}")

    territory_ids = list({
        acct.get("_territoryid_value")
        for acct in accounts_raw.values()
        if acct.get("_territoryid_value")
    })

    # ------------------------------------------------------------------
    # Phase 3: query territory details -> names
    # ------------------------------------------------------------------
    print("\nPhase 3: Querying territory details...")
    territories_raw: dict = {}
    for batch in _batched(territory_ids, _ACCT_BATCH):
        result = batch_query_territories(batch, batch_size=len(batch))
        if result.get("success"):
            territories_raw.update(result.get("territories", {}))

    territories_seen: dict = {}
    for acct in accounts_raw.values():
        tid = acct.get("_territoryid_value")
        if tid and tid in territories_raw:
            terr = territories_raw[tid]
            name = terr.get("name", "")
            if name:
                territories_seen.setdefault(name, {
                    "name": name,
                    "atu": terr.get("msp_accountteamunitname"),
                    "account_count": 0,
                })
                territories_seen[name]["account_count"] += 1
    print(f"  Territories found: {len(territories_seen)}")

    # ------------------------------------------------------------------
    # Phase 4: query account teams -> sellers
    # ------------------------------------------------------------------
    print("\nPhase 4: Querying account teams for sellers...")
    sellers_seen: dict = {}
    for batch in _batched(account_ids, _TEAM_BATCH):
        result = batch_query_account_teams(batch, batch_size=len(batch))
        if result.get("success"):
            sellers_seen.update(result.get("unique_sellers", {}))
    print(f"  Sellers found: {len(sellers_seen)}")

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("MSX ALIGNMENT AUDIT (read-only - nothing written to the database)")
    print("=" * 70)
    print(f"User:                {user_name}")
    print(f"Role:                {role}")
    print(f"Total accounts:      {len(accounts_raw)}")
    print(f"Total territories:   {len(territories_seen)}")
    print(f"Total sellers:       {len(sellers_seen)}")

    print("\n--- TERRITORIES ALIGNED TO ---")
    for name in sorted(territories_seen):
        info = territories_seen[name]
        atu = f" [ATU: {info['atu']}]" if info.get("atu") else ""
        print(f"  {name}{atu}  ({info['account_count']} accounts)")

    print("\n--- SELLERS ALIGNED TO ---")
    for name in sorted(sellers_seen):
        info = sellers_seen[name]
        stype = info.get("type", "")
        suffix = f"  ({stype})" if stype else ""
        print(f"  {name}{suffix}")

    print("\n" + "=" * 70)
    print("No changes were made. This was an audit-only run.")
    print("=" * 70)
    return 0


def main() -> int:
    app = create_app()
    with app.app_context():
        return run_audit()


if __name__ == "__main__":
    raise SystemExit(main())
