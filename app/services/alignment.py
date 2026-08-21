"""Custom alignment sync service.

Lets the user declare the territories they're aligned to for a fiscal year and
use that as the source of truth for "which accounts are mine" instead of the
user's own ``msp_accountteams`` membership. Alignment is territory-based: the
org aligns SEs to whole territories regardless of seller, so "my accounts" =
all accounts in the selected territories.

This module provides the read-only probe/discovery helpers plus persistence of
the user's territory selections. The account-import pipeline
(``/api/msx/accounts/sync``) calls :func:`discover_accounts_from_alignment` as an
alternate Phase 1 when an active alignment exists.

Nothing here fabricates records: it selects *which* live MSX accounts are the
user's; all account data still comes from live MSX queries.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.models import AlignmentSelection, AlignmentTerritory, db, utc_now
from app.services.msx_api import (
    get_accounts_for_territories,
    get_accounts_for_territory_ids,
    query_entity,
    query_next_page,
)

logger = logging.getLogger(__name__)


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
    pref = UserPreference.query.first()
    last_completed = pref.fy_last_completed if pref else None
    if last_completed == labels["next_fy"]:
        return labels["next_fy"]

    today = _date.today()
    if today.month in (7, 8):
        return labels["next_fy"]
    return labels["current_fy"]


# ---------------------------------------------------------------------------
# Territory universe (picker seed)
# ---------------------------------------------------------------------------


def _region_prefix(name: str) -> Optional[str]:
    """Return the ``Region.Segment.`` prefix of a territory name.

    e.g. ``East.SMECC.SOU.0206.A`` -> ``East.SMECC.``. Returns None if the
    name doesn't have at least two non-empty leading segments.
    """
    parts = (name or "").split(".")
    if len(parts) >= 2 and parts[0].strip() and parts[1].strip():
        return f"{parts[0]}.{parts[1]}."
    return None


def derive_territory_prefix(local_only: bool = False) -> Optional[str]:
    """Determine the territory-name prefix that scopes the picker to the user's
    region (e.g. ``East.SMECC.``) - no hardcoded region.

    Resolution order:
    1. The most common ``Region.Segment`` prefix among the user's existing
       local territories (the ``Territory`` table - their actual book, not the
       cached territory *universe*). Fast, no MSX call, correct for anyone who
       has synced before.
    2. Fall back to the user's MSX account-team territories
       (``find_my_territories``); the region there is stable even if the
       specific territory numbers are stale. Skipped when ``local_only``.

    Returns None if neither source yields a prefix.
    """
    from collections import Counter

    from app.models import Territory

    counter: Counter = Counter()
    for t in Territory.query.all():
        p = _region_prefix(t.name)
        if p:
            counter[p] += 1
    if counter:
        return counter.most_common(1)[0][0]

    if local_only:
        return None

    # Fallback: derive from the user's MSX account-team territories.
    try:
        from app.services.msx_api import find_my_territories
        result = find_my_territories()
        if result.get("success"):
            for terr in result.get("territories", []):
                p = _region_prefix(terr.get("name", ""))
                if p:
                    counter[p] += 1
        if counter:
            return counter.most_common(1)[0][0]
    except Exception:
        logger.exception("Failed to derive territory prefix from MSX")

    return None


def probe_territories(
    prefix: Optional[str] = None,
    all_regions: bool = False,
) -> Dict[str, Any]:
    """Probe MSX for territories and cache them locally.

    Upserts into ``alignment_territories`` so the picker is instant and works
    even when MSX is slow/blocked. Idempotent - safe to re-run to refresh.

    Args:
        prefix: Territory name prefix to probe (e.g. "East.SMECC."). When None,
            it is derived from the user's own data via
            :func:`derive_territory_prefix` so this works for any region.
        all_regions: When True, skip the region filter and pull every territory
            (for the rare cross-region user). Overrides ``prefix``.

    Returns:
        Dict with success, created, updated, total, prefix, all_regions
        (or error).
    """
    filter_query = None
    used_prefix = None
    if not all_regions:
        if not prefix:
            prefix = derive_territory_prefix()
        if not prefix:
            return {
                "success": False,
                "error": (
                    "Could not determine your territory region. Run a normal "
                    "account sync first (so Sales Buddy knows your territories), "
                    "then try again."
                ),
            }
        used_prefix = prefix
        safe_prefix = prefix.replace("'", "''")
        filter_query = f"startswith(name, '{safe_prefix}')"

    result = query_entity(
        "territories",
        select=["territoryid", "name", "msp_accountteamunitname"],
        filter_query=filter_query,
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
        "Probed territories (%s): %d created, %d updated",
        "all regions" if all_regions else used_prefix, created, updated,
    )
    return {
        "success": True,
        "created": created,
        "updated": updated,
        "total": len(records),
        "prefix": used_prefix,
        "all_regions": all_regions,
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
# Selection persistence
# ---------------------------------------------------------------------------


def get_alignment_selections(fy_label: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return saved territory selections for a fiscal year."""
    fy_label = fy_label or current_fy_label()
    rows = (
        AlignmentSelection.query
        .filter_by(fy_label=fy_label, active=True)
        .order_by(AlignmentSelection.territory_name.asc())
        .all()
    )
    return [
        {
            "fy_label": s.fy_label,
            "msx_territory_id": s.msx_territory_id,
            "territory_name": s.territory_name,
        }
        for s in rows
    ]


def save_alignment_selections(
    territories: List[Dict[str, Any]],
    fy_label: Optional[str] = None,
    set_override: Optional[bool] = None,
) -> Dict[str, Any]:
    """Replace the active territory selections for a fiscal year.

    Deactivates any current selections for ``fy_label`` not present in
    ``territories``, then upserts the provided territories as active.
    Non-destructive to other fiscal years. Saving records intent only - it
    does not run a sync.

    Saving is also the commit point for the override toggle: pass
    ``set_override`` to set the override flag atomically with the selections.
    The invariant "override on requires >= 1 territory" is enforced here, so
    the persisted state is always coherent - an account sync (even one
    triggered via API) can never see override-on with zero territories.

    Args:
        territories: List of dicts, each with msx_territory_id and
            territory_name.
        fy_label: Fiscal year label (defaults to current).
        set_override: When not None, set the override flag - but only to True
            if >= 1 territory ends up active; otherwise forced off.

    Returns:
        Dict with success and counts.
    """
    fy_label = fy_label or current_fy_label()

    incoming: Dict[str, Dict[str, Any]] = {}
    for t in territories:
        tid = t.get("msx_territory_id")
        if not tid:
            continue
        incoming[tid] = t

    existing = AlignmentSelection.query.filter_by(fy_label=fy_label).all()
    existing_by_id = {s.msx_territory_id: s for s in existing}

    added = 0
    reactivated = 0
    deactivated = 0

    # Deactivate territories no longer selected.
    for tid, row in existing_by_id.items():
        if tid not in incoming and row.active:
            row.active = False
            deactivated += 1

    # Upsert selected territories as active.
    for tid, t in incoming.items():
        row = existing_by_id.get(tid)
        if row:
            if not row.active:
                row.active = True
                reactivated += 1
            row.territory_name = t.get("territory_name") or row.territory_name
        else:
            db.session.add(AlignmentSelection(
                fy_label=fy_label,
                msx_territory_id=tid,
                territory_name=t.get("territory_name") or "",
                active=True,
            ))
            added += 1

    override_active = None
    if set_override is not None:
        from app.models import UserPreference
        pref = UserPreference.query.first()
        if pref:
            # Invariant: on requires >= 1 active territory.
            override_active = bool(set_override) and len(incoming) > 0
            pref.alignment_override_active = override_active

    db.session.commit()
    return {
        "success": True,
        "fy_label": fy_label,
        "added": added,
        "reactivated": reactivated,
        "deactivated": deactivated,
        "active_total": len(incoming),
        "override_active": override_active,
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

def is_override_active() -> bool:
    """Return True if the alignment override is switched on.

    When on, the account sync uses the declared territory alignment instead of
    the user's msp_accountteams membership.
    """
    from app.models import UserPreference
    pref = UserPreference.query.first()
    return bool(pref and pref.alignment_override_active)


def set_override_active(active: bool) -> bool:
    """Turn the alignment override on or off. Returns the new state."""
    from app.models import UserPreference
    pref = UserPreference.query.first()
    if pref:
        pref.alignment_override_active = bool(active)
        db.session.commit()
    return is_override_active()

# ---------------------------------------------------------------------------
# Territory-based account discovery (alternate sync Phase 1)
# ---------------------------------------------------------------------------


def summarize_accounts_for_territory_ids(
    territory_ids: List[str],
) -> Dict[str, Any]:
    """Return account/customer counts for the given territory GUIDs (read-only).

    Preferred over the name-based variant: querying by territory id skips the
    fragile name -> id lookup, so a stale/renamed cached name or a transient
    MSX error can't masquerade as "no territories found". Always returns the
    count fields (zeros when empty).
    """
    ids = [i for i in (territory_ids or []) if i]
    if not ids:
        return {
            "success": True,
            "account_ids": [],
            "territory_count": 0,
            "kept_account_count": 0,
            "customer_count": 0,
        }

    acct_result = get_accounts_for_territory_ids(ids)
    if not acct_result.get("success"):
        return acct_result

    accounts = acct_result.get("accounts", [])
    account_ids = [a["account_id"] for a in accounts if a.get("account_id")]
    # The territory query returns canonical top-level records. Keep TPID
    # deduplication defensive in case MSX ever returns duplicate parents.
    unique_tpids = {a.get("tpid") for a in accounts if a.get("tpid")}

    return {
        "success": True,
        "account_ids": account_ids,
        "territory_count": len(set(ids)),
        "kept_account_count": len(account_ids),
        "customer_count": len(unique_tpids),
    }


def summarize_accounts_for_territories(
    territory_names: List[str],
) -> Dict[str, Any]:
    """Name-based account/customer summary (legacy).

    Prefer :func:`summarize_accounts_for_territory_ids` where ids are available
    - the name lookup is fragile. Kept for callers that only have names.
    """
    names = sorted({n for n in (territory_names or []) if n})
    if not names:
        return {
            "success": True,
            "account_ids": [],
            "territory_count": 0,
            "kept_account_count": 0,
            "customer_count": 0,
        }

    acct_result = get_accounts_for_territories(names)
    if not acct_result.get("success"):
        return acct_result

    accounts = acct_result.get("accounts", [])
    account_ids = [a["account_id"] for a in accounts if a.get("account_id")]
    unique_tpids = {a.get("tpid") for a in accounts if a.get("tpid")}

    return {
        "success": True,
        "account_ids": account_ids,
        "territory_count": len(names),
        "kept_account_count": len(account_ids),
        "customer_count": len(unique_tpids),
    }


def discover_accounts_from_alignment(
    fy_label: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the account IDs in the user's saved territory alignment.

    "My accounts" = all accounts in the selected territories. This replaces the
    ``msp_accountteams``-based discovery (scan_init) with the user's declared
    territory alignment.

    Returns a shape compatible with the import pipeline's Phase 1:
        {"success": True, "account_ids": [...], ...}
    """
    fy_label = fy_label or current_fy_label()

    selections = (
        AlignmentSelection.query
        .filter_by(fy_label=fy_label, active=True)
        .all()
    )
    territory_ids = sorted({s.msx_territory_id for s in selections if s.msx_territory_id})

    result = summarize_accounts_for_territory_ids(territory_ids)
    if not result.get("success"):
        return result

    result["fy_label"] = fy_label
    result["source"] = "alignment"
    if not territory_ids:
        result["message"] = f"No active alignment for {fy_label}"

    logger.info(
        "Alignment discovery (%s): %d territories -> %d accounts, %d customers",
        fy_label, len(territory_ids),
        result.get("kept_account_count", 0), result.get("customer_count", 0),
    )
    return result

