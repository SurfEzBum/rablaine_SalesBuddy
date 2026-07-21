"""Custom alignment sync routes.

Serves the interactive alignment panel and its JSON APIs: probe/list the
territory universe, discover sellers for selected territories, persist the
user's (territory, seller) selections, and preview the accounts a sync would
pull under the current alignment. All MSX reads are read-only; saving records
intent only and does not run a sync.
"""
import logging

from flask import Blueprint, jsonify, render_template, request

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


@alignment_bp.route('/alignment')
def alignment_panel():
    """Render the interactive alignment configuration panel."""
    fy_label = alignment.current_fy_label()
    territories = alignment.list_territories()
    selections = alignment.get_alignment_selections(fy_label)
    return render_template(
        'alignment_panel.html',
        fy_label=fy_label,
        territory_count=len(territories),
        selection_count=len(selections),
    )


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


@alignment_bp.route('/alignment/api/sellers', methods=['POST'])
def api_sellers():
    """Discover the Cloud & AI sellers present in the selected territories."""
    guard = _require_msx()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    territory_names = data.get('territories', [])
    if not territory_names:
        return jsonify({"success": False, "error": "territories list required"}), 400
    result = alignment.discover_sellers_for_territories(territory_names)
    status = 200 if result.get('success') else 502
    return jsonify(result), status


@alignment_bp.route('/alignment/api/selections', methods=['GET'])
def api_get_selections():
    """Return the saved (territory, seller) selections for the current FY."""
    fy_label = request.args.get('fy_label') or alignment.current_fy_label()
    return jsonify({
        "success": True,
        "fy_label": fy_label,
        "selections": alignment.get_alignment_selections(fy_label),
    })


@alignment_bp.route('/alignment/api/selections', methods=['POST'])
def api_save_selections():
    """Persist the user's (territory, seller) selections (records intent only)."""
    data = request.get_json(silent=True) or {}
    pairs = data.get('pairs', [])
    fy_label = data.get('fy_label') or alignment.current_fy_label()
    result = alignment.save_alignment_selections(pairs, fy_label=fy_label)
    return jsonify(result)


@alignment_bp.route('/alignment/api/preview', methods=['POST'])
def api_preview():
    """Preview accounts a sync would pull under the saved alignment (read-only)."""
    guard = _require_msx()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    fy_label = data.get('fy_label') or alignment.current_fy_label()
    result = alignment.discover_accounts_from_alignment(fy_label)
    status = 200 if result.get('success') else 502
    return jsonify(result), status
