"""Custom alignment sync service.

Lets the user declare, per territory, which Cloud & AI sellers they support -
a set of (territory, seller) pairs - and use that as the source of truth for
"which accounts are mine" instead of the user's own ``msp_accountteams``
membership.

This module provides the read-only discovery/probe helpers plus persistence of
the user's selections. The account-import pipeline
(``/api/msx/import-stream``) calls :func:`discover_accounts_from_alignment` as an
alternate Phase 1 when an active alignment exists.

Nothing here fabricates records: it selects *which* live MSX accounts are the
user's; all account/seller data still comes from live MSX queries.
"""
from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from app.models import AlignmentSelection, AlignmentTerritory, db, utc_now
from app.services.msx_api import (
    batch_query_account_teams,
    get_accounts_for_territories,
    query_entity,
    query_next_page,
)

logger = logging.getLogger(__name__)

# How many accounts per msp_accountteams batch. Kept small: the server-side
# filter still returns ~20-30 rows per account, and small batches avoid URL
# length limits. Mirrors _TEAM_BATCH in the import route.
_TEAM_BATCH = 3

# Parallel workers for MSX team resolution (mirrors the import pipeline).
_PARALLEL_WORKERS = 3

# Default region prefix for the territory-universe probe.
_DEFAULT_TERRITORY_PREFIX = "East.SMECC."


def _split_chunks(items: List[Any], n: int) -> List[List[Any]]:
    """Split ``items`` into up to ``n`` roughly-equal chunks for parallelism."""
    if not items:
        return []
    size = math.ceil(len(items) / n)
    return [items[i:i + size] for i in range(0, len(items), size)]


def _resolve_account_sellers(account_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Resolve the Cloud & AI seller for each account via msp_accountteams.

    Runs the team queries across ``_PARALLEL_WORKERS`` threads (mirroring the
    import pipeline) so large territory sets don't take minutes. Returns
    ``{account_id: {name, type, user_id}}`` for accounts that have a seller.
    """
    account_sellers: Dict[str, Dict[str, Any]] = {}
    chunks = _split_chunks(account_ids, _PARALLEL_WORKERS)
    if not chunks:
        return account_sellers
    with ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as pool:
        futures = [
            pool.submit(batch_query_account_teams, chunk, _TEAM_BATCH)
            for chunk in chunks if chunk
        ]
        for f in futures:
            r = f.result()
            if r.get("success"):
                account_sellers.update(r.get("account_sellers", {}))
    return account_sellers


def current_fy_label() -> str:
    """Return the fiscal-year label the alignment should target.

    Resolution order:
    1. During an active FY transition, the FY being moved *into* (transition
       label, e.g. "FY27") - that's the alignment being configured.
    2. During FY changeover season (Jul-Aug) when the next FY hasn't been
       completed yet, the next FY - so the panel targets the year you're
       aligning for before you formally start the transition.
    3. Otherwise, the current fiscal year.
    """
    from datetime import date as _date

    from app.models import UserPreference
    from app.services.fy_cutover import get_fiscal_year_labels, get_transition_state

    state = get_transition_state()
    if state.get("in_transition") and state.get("fy_label"):
        return state["fy_label"]

    labels = get_fiscal_year_labels()
    today = _date.today()
    if today.month in (7, 8):
        pref = UserPreference.query.first()
        last_completed = pref.fy_last_completed if pref else None
        if last_completed != labels["next_fy"]:
            return labels["next_fy"]
    return labels["current_fy"]


# ---------------------------------------------------------------------------
# Territory universe (picker seed)
# ---------------------------------------------------------------------------


def probe_territories(prefix: str = _DEFAULT_TERRITORY_PREFIX) -> Dict[str, Any]:
    """Probe MSX for all territories under ``prefix`` and cache them locally.

    Upserts into ``alignment_territories`` so the picker is instant and works
    even when MSX is slow/blocked. Idempotent - safe to re-run to refresh.

    Args:
        prefix: Territory name prefix to probe (e.g. "East.SMECC.").

    Returns:
        Dict with success, created, updated, total counts (or error).
    """
    safe_prefix = prefix.replace("'", "''")
    result = query_entity(
        "territories",
        select=["territoryid", "name", "msp_accountteamunitname"],
        filter_query=f"startswith(name, '{safe_prefix}')",
        top=1000,
        order_by="name asc",
    )
    if not result.get("success"):
        return result

    records = list(result.get("records", []))
    next_link = result.get("next_link")
    while next_link:
        page = query_next_page(next_link)
        if not page.get("success"):
            break
        records.extend(page.get("records", []))
        next_link = page.get("next_link")

    created = 0
    updated = 0
    for rec in records:
        msx_id = rec.get("territoryid")
        name = rec.get("name")
        atu = rec.get("msp_accountteamunitname")
        if not msx_id or not name:
            continue
        existing = AlignmentTerritory.query.filter_by(msx_territory_id=msx_id).first()
        if existing:
            existing.name = name
            existing.atu = atu
            existing.last_probed_at = utc_now()
            updated += 1
        else:
            db.session.add(AlignmentTerritory(
                msx_territory_id=msx_id,
                name=name,
                atu=atu,
            ))
            created += 1

    db.session.commit()
    logger.info(
        "Probed territories under '%s': %d created, %d updated",
        prefix, created, updated,
    )
    return {
        "success": True,
        "created": created,
        "updated": updated,
        "total": len(records),
    }


def list_territories() -> List[Dict[str, Any]]:
    """Return the cached territory universe for the picker (from local DB)."""
    rows = AlignmentTerritory.query.order_by(AlignmentTerritory.name.asc()).all()
    return [
        {
            "msx_territory_id": t.msx_territory_id,
            "name": t.name,
            "atu": t.atu,
        }
        for t in rows
    ]


# ---------------------------------------------------------------------------
# Seller discovery for selected territories (panel Step B)
# ---------------------------------------------------------------------------


def discover_sellers_for_territories(
    territory_names: List[str],
) -> Dict[str, Any]:
    """Find the Cloud & AI sellers present in the given territories.

    For each selected territory, returns the unique sellers (Growth + Acq) that
    sit on those accounts' Cloud & AI teams, with per-seller account counts.
    Backs the panel's per-territory checkbox list.

    Args:
        territory_names: Territory names the user selected.

    Returns:
        Dict with success and a ``territories`` list; each entry has
        territory_id, territory_name, account_count, and a ``sellers`` list of
        {user_id, name, type, account_count}.
    """
    if not territory_names:
        return {"success": True, "territories": []}

    acct_result = get_accounts_for_territories(territory_names)
    if not acct_result.get("success"):
        return acct_result

    accounts = acct_result.get("accounts", [])
    # account_id -> (territory_id, territory_name)
    acct_terr: Dict[str, tuple] = {
        a["account_id"]: (a.get("territory_id"), a.get("territory_name"))
        for a in accounts
        if a.get("account_id")
    }
    account_ids = list(acct_terr.keys())

    # Resolve the Cloud & AI seller per account via msp_accountteams (parallel).
    account_sellers = _resolve_account_sellers(account_ids)

    # Aggregate per territory -> per seller.
    # territory_id -> {"name":..., "account_count":N, "sellers": {uid: {...}}}
    terr_map: Dict[Any, Dict[str, Any]] = {}
    for aid, (tid, tname) in acct_terr.items():
        entry = terr_map.setdefault(tid, {
            "territory_id": tid,
            "territory_name": tname,
            "account_count": 0,
            "sellers": {},
        })
        entry["account_count"] += 1
        seller = account_sellers.get(aid)
        if not seller:
            continue
        uid = seller.get("user_id")
        if not uid:
            continue
        s_entry = entry["sellers"].setdefault(uid, {
            "user_id": uid,
            "name": seller.get("name"),
            "type": seller.get("type"),
            "account_count": 0,
        })
        s_entry["account_count"] += 1

    territories = []
    for entry in terr_map.values():
        sellers = sorted(
            entry["sellers"].values(),
            key=lambda s: (s.get("name") or "").lower(),
        )
        territories.append({
            "territory_id": entry["territory_id"],
            "territory_name": entry["territory_name"],
            "account_count": entry["account_count"],
            "sellers": sellers,
        })
    territories.sort(key=lambda t: (t.get("territory_name") or ""))

    return {"success": True, "territories": territories}


# ---------------------------------------------------------------------------
# Selection persistence
# ---------------------------------------------------------------------------


def get_alignment_selections(fy_label: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return saved (territory, seller) selections for a fiscal year."""
    fy_label = fy_label or current_fy_label()
    rows = (
        AlignmentSelection.query
        .filter_by(fy_label=fy_label, active=True)
        .order_by(AlignmentSelection.territory_name.asc(),
                  AlignmentSelection.seller_name.asc())
        .all()
    )
    return [
        {
            "fy_label": s.fy_label,
            "msx_territory_id": s.msx_territory_id,
            "territory_name": s.territory_name,
            "seller_msx_user_id": s.seller_msx_user_id,
            "seller_name": s.seller_name,
            "seller_type": s.seller_type,
        }
        for s in rows
    ]


def save_alignment_selections(
    pairs: List[Dict[str, Any]],
    fy_label: Optional[str] = None,
) -> Dict[str, Any]:
    """Replace the active alignment selections for a fiscal year.

    Deactivates any current selections for ``fy_label`` not present in
    ``pairs``, then upserts the provided pairs as active. Non-destructive to
    other fiscal years. Saving records intent only - it does not sync.

    Args:
        pairs: List of dicts, each with msx_territory_id, territory_name,
            seller_msx_user_id, seller_name, and optional seller_type.
        fy_label: Fiscal year label (defaults to current).

    Returns:
        Dict with success and counts.
    """
    fy_label = fy_label or current_fy_label()

    # Normalize incoming pairs and index by (territory_id, seller_user_id).
    incoming: Dict[tuple, Dict[str, Any]] = {}
    for p in pairs:
        tid = p.get("msx_territory_id")
        uid = p.get("seller_msx_user_id")
        if not tid or not uid:
            continue
        incoming[(tid, uid)] = p

    existing = AlignmentSelection.query.filter_by(fy_label=fy_label).all()
    existing_by_key = {
        (s.msx_territory_id, s.seller_msx_user_id): s for s in existing
    }

    added = 0
    reactivated = 0
    deactivated = 0

    # Deactivate rows no longer selected.
    for key, row in existing_by_key.items():
        if key not in incoming and row.active:
            row.active = False
            deactivated += 1

    # Upsert selected pairs as active.
    for key, p in incoming.items():
        row = existing_by_key.get(key)
        if row:
            if not row.active:
                row.active = True
                reactivated += 1
            row.territory_name = p.get("territory_name") or row.territory_name
            row.seller_name = p.get("seller_name") or row.seller_name
            row.seller_type = p.get("seller_type") or row.seller_type
        else:
            db.session.add(AlignmentSelection(
                fy_label=fy_label,
                msx_territory_id=key[0],
                territory_name=p.get("territory_name") or "",
                seller_msx_user_id=key[1],
                seller_name=p.get("seller_name") or "",
                seller_type=p.get("seller_type"),
                active=True,
            ))
            added += 1

    db.session.commit()
    return {
        "success": True,
        "fy_label": fy_label,
        "added": added,
        "reactivated": reactivated,
        "deactivated": deactivated,
        "active_total": added + reactivated + (
            len(existing_by_key) - deactivated - reactivated
        ),
    }


def has_active_alignment(fy_label: Optional[str] = None) -> bool:
    """Return True if any active alignment selection exists for the FY."""
    fy_label = fy_label or current_fy_label()
    return (
        AlignmentSelection.query
        .filter_by(fy_label=fy_label, active=True)
        .first()
        is not None
    )


# ---------------------------------------------------------------------------
# Seller-scoped account discovery (alternate sync Phase 1)
# ---------------------------------------------------------------------------


def discover_accounts_from_alignment(
    fy_label: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the account IDs implied by the saved (territory, seller) pairs.

    "My accounts" = accounts in a selected territory whose Cloud & AI seller is
    a selected seller for that territory. This is the seller scoping that a
    whole-territory sync cannot express.

    Returns a shape compatible with the import pipeline's Phase 1:
        {"success": True, "account_ids": [...], "user": {...}, "role": ...}
    """
    fy_label = fy_label or current_fy_label()

    selections = (
        AlignmentSelection.query
        .filter_by(fy_label=fy_label, active=True)
        .all()
    )
    if not selections:
        return {
            "success": True,
            "account_ids": [],
            "message": f"No active alignment for {fy_label}",
        }

    territory_names = sorted({s.territory_name for s in selections if s.territory_name})
    # (territory_id, seller_user_id) pairs the user supports.
    selected_pairs = {
        (s.msx_territory_id, s.seller_msx_user_id) for s in selections
    }

    acct_result = get_accounts_for_territories(territory_names)
    if not acct_result.get("success"):
        return acct_result

    accounts = acct_result.get("accounts", [])
    # account_id -> territory_id
    acct_terr = {
        a["account_id"]: a.get("territory_id")
        for a in accounts
        if a.get("account_id")
    }
    account_ids = list(acct_terr.keys())

    # Resolve Cloud & AI seller per account (parallel).
    account_sellers = _resolve_account_sellers(account_ids)

    kept: List[str] = []
    for aid, tid in acct_terr.items():
        seller = account_sellers.get(aid)
        if not seller:
            continue
        uid = seller.get("user_id")
        if uid and (tid, uid) in selected_pairs:
            kept.append(aid)

    logger.info(
        "Alignment discovery (%s): %d territory accounts -> %d kept after seller scoping",
        fy_label, len(account_ids), len(kept),
    )
    return {
        "success": True,
        "account_ids": kept,
        "fy_label": fy_label,
        "territory_account_count": len(account_ids),
        "kept_account_count": len(kept),
        "source": "alignment",
    }
