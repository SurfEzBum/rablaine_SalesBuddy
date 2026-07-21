"""Alignment override routes.

JSON APIs behind the admin panel's Alignment Override section and its territory
modal: probe/list the territory universe, persist the user's territory
selections, toggle the override on/off, and preview how many accounts/customers
the alignment resolves to. All MSX reads are read-only; saving records intent
only and does not run a sync.
"""
import logging

from flask import Blueprint, jsonify, request

from app.services import alignment
from app.services.msx_auth import get_msx_token

logger = logging.getLogger(__name__)

alignment_bp = Blueprint('alignment', __name__)


def _require_msx():
    """Return an error response tuple if MSX auth is missing, else None."""
    if not get_msx_token():
        return jsonify({
            "success": False,
            "error": "Not signed in to Azure. Complete Azure sign-in first.",
            "auth_required": True,
        }), 401
    return None


@alignment_bp.route('/alignment/api/status')
def api_status():
    """Return override state + selection summary for the admin card."""
    fy_label = alignment.current_fy_label()
    selections = alignment.get_alignment_selections(fy_label)
    return jsonify({
        "success": True,
        "fy_label": fy_label,
        "override_active": alignment.is_override_active(),
        "selection_count": len(selections),
        "territories": [s["territory_name"] for s in selections],
        "territory_cache_count": len(alignment.list_territories()),
    })


@alignment_bp.route('/alignment/api/override', methods=['POST'])
def api_set_override():
    """Turn the alignment override on or off."""
    data = request.get_json(silent=True) or {}
    active = bool(data.get('active'))
    new_state = alignment.set_override_active(active)
    return jsonify({"success": True, "override_active": new_state})


@alignment_bp.route('/alignment/api/territories')
def api_territories():
    """Return the cached territory universe for the picker."""
    return jsonify({
        "success": True,
        "territories": alignment.list_territories(),
    })


@alignment_bp.route('/alignment/api/probe', methods=['POST'])
def api_probe():
    """Probe MSX for the territory universe and refresh the local cache."""
    guard = _require_msx()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    prefix = (data.get('prefix') or '').strip() or 'East.SMECC.'
    result = alignment.probe_territories(prefix=prefix)
    status = 200 if result.get('success') else 502
    return jsonify(result), status


@alignment_bp.route('/alignment/api/selections', methods=['GET'])
def api_get_selections():
    """Return the saved territory selections for the current FY."""
    fy_label = request.args.get('fy_label') or alignment.current_fy_label()
    return jsonify({
        "success": True,
        "fy_label": fy_label,
        "selections": alignment.get_alignment_selections(fy_label),
    })


@alignment_bp.route('/alignment/api/selections', methods=['POST'])
def api_save_selections():
    """Persist the user's territory selections (records intent only)."""
    data = request.get_json(silent=True) or {}
    territories = data.get('territories', [])
    fy_label = data.get('fy_label') or alignment.current_fy_label()
    result = alignment.save_alignment_selections(territories, fy_label=fy_label)
    return jsonify(result)


@alignment_bp.route('/alignment/api/preview', methods=['POST'])
def api_preview():
    """Preview accounts/customers an alignment would pull (read-only).

    If ``territories`` (the in-modal selection) is provided, preview those -
    so the user can check the count before saving. Otherwise fall back to the
    saved alignment for the current FY.
    """
    guard = _require_msx()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    territories = data.get('territories')
    if territories:
        names = [
            (t.get('territory_name') if isinstance(t, dict) else t)
            for t in territories
        ]
        names = [n for n in names if n]
        result = alignment.summarize_accounts_for_territories(names)
    else:
        fy_label = data.get('fy_label') or alignment.current_fy_label()
        result = alignment.discover_accounts_from_alignment(fy_label)
    status = 200 if result.get('success') else 502
    return jsonify(result), status
