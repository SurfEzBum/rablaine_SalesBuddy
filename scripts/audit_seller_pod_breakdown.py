"""One-off: break down the current user's MSX accounts by seller and POD.

Tests the "you're split across PODs because Tim O'Shea's Acq book spans both"
justification. Read-only - writes nothing. Mirrors the sync's reads.

Usage:
    python scripts/audit_seller_pod_breakdown.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

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


def _batched(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _pod_from_territory(name: str):
    parts = (name or "").split(".")
    if len(parts) >= 4 and len(parts[3]) >= 2:
        return f"{parts[0]} POD {parts[3][:2]}"
    return None


def run():
    if not get_msx_token():
        print("ERROR: not signed in to MSX. Run `az login` first.")
        return 1

    init = scan_init()
    if not init.get("success"):
        print("ERROR:", init.get("error"))
        return 1
    account_ids = init.get("account_ids", [])
    print(f"Accounts: {len(account_ids)}")

    # accounts -> territory
    accounts_raw = {}
    for b in _batched(account_ids, _ACCT_BATCH):
        r = batch_query_accounts(b, batch_size=len(b))
        if r.get("success"):
            accounts_raw.update(r.get("accounts", {}))

    terr_ids = list({a.get("_territoryid_value") for a in accounts_raw.values()
                     if a.get("_territoryid_value")})
    terr_raw = {}
    for b in _batched(terr_ids, _ACCT_BATCH):
        r = batch_query_territories(b, batch_size=len(b))
        if r.get("success"):
            terr_raw.update(r.get("territories", {}))

    # account -> seller
    account_sellers = {}
    for b in _batched(account_ids, _TEAM_BATCH):
        r = batch_query_account_teams(b, batch_size=len(b))
        if r.get("success"):
            account_sellers.update(r.get("account_sellers", {}))

    # Build seller -> pod -> count, and seller -> territories
    seller_pod = defaultdict(lambda: defaultdict(int))
    seller_terr = defaultdict(set)
    seller_total = defaultdict(int)
    pod_total = defaultdict(int)
    no_seller = 0

    for aid, acct in accounts_raw.items():
        tid = acct.get("_territoryid_value")
        tname = terr_raw.get(tid, {}).get("name", "") if tid else ""
        pod = _pod_from_territory(tname) or "(no pod)"
        pod_total[pod] += 1
        s = account_sellers.get(aid)
        if not s:
            no_seller += 1
            continue
        sname = f"{s['name']} ({s.get('type', '?')})"
        seller_pod[sname][pod] += 1
        seller_terr[sname].add(tname)
        seller_total[sname] += 1

    print("\n=== ACCOUNTS BY POD ===")
    for pod in sorted(pod_total):
        print(f"  {pod}: {pod_total[pod]}")

    print(f"\n  Accounts with NO seller mapped: {no_seller}")

    print("\n=== SELLER x POD BREAKDOWN ===")
    for sname in sorted(seller_total, key=lambda x: -seller_total[x]):
        pods = seller_pod[sname]
        spans = len(pods)
        flag = "  <-- SPANS MULTIPLE PODS" if spans > 1 else ""
        print(f"\n  {sname}: {seller_total[sname]} accounts across {spans} POD(s){flag}")
        for pod in sorted(pods):
            print(f"      {pod}: {pods[pod]}")
        print(f"      territories: {', '.join(sorted(t for t in seller_terr[sname] if t))}")

    return 0


def main():
    app = create_app()
    with app.app_context():
        return run()


if __name__ == "__main__":
    raise SystemExit(main())
