"""Tests for the custom alignment sync service (app/services/alignment.py).

Alignment is territory-based: "my accounts" = all accounts in the selected
territories (no seller scoping).
"""

from unittest.mock import patch

import pytest


FY = "FY99"  # isolated fiscal-year label for tests


def _terr(tid, name):
    return {"msx_territory_id": tid, "territory_name": name}


class TestAlignmentModels:
    """Model-level behavior for alignment tables."""

    def test_create_territory_and_selection(self, app):
        with app.app_context():
            from app.models import AlignmentSelection, AlignmentTerritory, db

            db.session.add(AlignmentTerritory(
                msx_territory_id="t1", name="East.SMECC.SOU.0206.A",
                atu="East.SMECC.SOU",
            ))
            db.session.add(AlignmentSelection(
                fy_label=FY, msx_territory_id="t1",
                territory_name="East.SMECC.SOU.0206.A",
            ))
            db.session.commit()

            assert AlignmentTerritory.query.count() == 1
            sel = AlignmentSelection.query.first()
            assert sel.active is True
            assert sel.territory_name == "East.SMECC.SOU.0206.A"

    def test_selection_unique_constraint(self, app):
        with app.app_context():
            from app.models import AlignmentSelection, db

            db.session.add(AlignmentSelection(
                fy_label=FY, msx_territory_id="t1", territory_name="T1",
            ))
            db.session.commit()

            db.session.add(AlignmentSelection(
                fy_label=FY, msx_territory_id="t1", territory_name="T1 dup",
            ))
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()


class TestProbeTerritories:
    """Territory-universe probe + cache."""

    def test_probe_upserts_and_refreshes(self, app):
        with app.app_context():
            from app.models import AlignmentTerritory
            from app.services import alignment

            page = {
                "success": True,
                "records": [
                    {"territoryid": "t1", "name": "East.SMECC.SOU.0206.A",
                     "msp_accountteamunitname": "East.SMECC.SOU"},
                    {"territoryid": "t2", "name": "East.SMECC.HLA.0506.A",
                     "msp_accountteamunitname": "East.SMECC.HLA"},
                ],
                "next_link": None,
            }
            with patch.object(alignment, "query_entity", return_value=page):
                result = alignment.probe_territories(prefix="East.SMECC.")

            assert result["success"] is True
            assert result["created"] == 2
            assert AlignmentTerritory.query.count() == 2

            page2 = {
                "success": True,
                "records": [
                    {"territoryid": "t1", "name": "East.SMECC.SOU.0206.RENAMED",
                     "msp_accountteamunitname": "East.SMECC.SOU"},
                ],
                "next_link": None,
            }
            with patch.object(alignment, "query_entity", return_value=page2):
                result2 = alignment.probe_territories(prefix="East.SMECC.")

            assert result2["updated"] == 1
            assert AlignmentTerritory.query.count() == 2
            t1 = AlignmentTerritory.query.filter_by(msx_territory_id="t1").first()
            assert t1.name == "East.SMECC.SOU.0206.RENAMED"

    def test_probe_follows_pagination(self, app):
        with app.app_context():
            from app.models import AlignmentTerritory
            from app.services import alignment

            first = {
                "success": True,
                "records": [{"territoryid": "t1", "name": "East.SMECC.A.1",
                             "msp_accountteamunitname": "x"}],
                "next_link": "PAGE2",
            }
            second = {
                "success": True,
                "records": [{"territoryid": "t2", "name": "East.SMECC.A.2",
                             "msp_accountteamunitname": "x"}],
                "next_link": None,
            }
            with patch.object(alignment, "query_entity", return_value=first), \
                 patch.object(alignment, "query_next_page", return_value=second):
                result = alignment.probe_territories(prefix="East.SMECC.")

            assert result["total"] == 2
            assert AlignmentTerritory.query.count() == 2


class TestRegionDerivation:
    """derive_territory_prefix - no hardcoded region."""

    def test_region_prefix_extraction(self, app):
        with app.app_context():
            from app.services import alignment
            assert alignment._region_prefix("East.SMECC.SOU.0206.A") == "East.SMECC."
            assert alignment._region_prefix("West.SMECC.MAA.0101") == "West.SMECC."
            assert alignment._region_prefix("Central.ENT.FOO.1") == "Central.ENT."
            assert alignment._region_prefix("single") is None
            assert alignment._region_prefix("") is None

    def test_derives_from_local_territories(self, app):
        with app.app_context():
            from app.models import Territory, db
            from app.services import alignment

            # A West-region user: prefix must derive to West, not a hardcoded East.
            db.session.add(Territory(name="West.SMECC.MAA.0101"))
            db.session.add(Territory(name="West.SMECC.MAA.0102"))
            db.session.add(Territory(name="West.SMECC.SOU.0203"))
            db.session.commit()

            assert alignment.derive_territory_prefix() == "West.SMECC."

    def test_probe_auto_derives_prefix(self, app):
        with app.app_context():
            from app.models import Territory, db
            from app.services import alignment

            db.session.add(Territory(name="West.SMECC.MAA.0101"))
            db.session.commit()

            page = {
                "success": True,
                "records": [{"territoryid": "w1", "name": "West.SMECC.MAA.0101",
                             "msp_accountteamunitname": "West.SMECC.MAA"}],
                "next_link": None,
            }
            captured = {}

            def fake_query(entity, **kwargs):
                captured["filter"] = kwargs.get("filter_query")
                return page

            with patch.object(alignment, "query_entity", side_effect=fake_query):
                result = alignment.probe_territories()  # no explicit prefix

            assert result["success"] is True
            assert result["prefix"] == "West.SMECC."
            assert "West.SMECC." in captured["filter"]

    def test_probe_errors_when_region_undeterminable(self, app):
        with app.app_context():
            from app.services import alignment
            # No local territories; MSX fallback returns nothing.
            with patch("app.services.msx_api.find_my_territories",
                       return_value={"success": True, "territories": []}):
                result = alignment.probe_territories()
            assert result["success"] is False
            assert "region" in result["error"].lower()

    def test_probe_all_regions_skips_filter(self, app):
        with app.app_context():
            from app.services import alignment

            page = {
                "success": True,
                "records": [
                    {"territoryid": "e1", "name": "East.SMECC.SOU.0206",
                     "msp_accountteamunitname": "East.SMECC.SOU"},
                    {"territoryid": "w1", "name": "West.SMECC.MAA.0101",
                     "msp_accountteamunitname": "West.SMECC.MAA"},
                ],
                "next_link": None,
            }
            captured = {}

            def fake_query(entity, **kwargs):
                captured["filter"] = kwargs.get("filter_query")
                return page

            with patch.object(alignment, "query_entity", side_effect=fake_query):
                result = alignment.probe_territories(all_regions=True)

            assert result["success"] is True
            assert result["all_regions"] is True
            assert result["prefix"] is None
            # No region filter applied - both regions pulled.
            assert captured["filter"] is None
            assert result["total"] == 2


class TestSelectionPersistence:
    """save/get territory selections."""

    def test_save_and_get(self, app):
        with app.app_context():
            from app.services import alignment

            territories = [_terr("t1", "T1"), _terr("t2", "T2")]
            res = alignment.save_alignment_selections(territories, fy_label=FY)
            assert res["added"] == 2

            saved = alignment.get_alignment_selections(FY)
            assert len(saved) == 2
            assert {s["territory_name"] for s in saved} == {"T1", "T2"}
            assert alignment.has_active_alignment(FY) is True

    def test_edit_deactivates_removed_territories(self, app):
        with app.app_context():
            from app.models import AlignmentSelection
            from app.services import alignment

            alignment.save_alignment_selections(
                [_terr("t1", "T1"), _terr("t2", "T2")], fy_label=FY)

            # Re-save keeping only t1 -> t2 becomes inactive (not deleted).
            res = alignment.save_alignment_selections([_terr("t1", "T1")], fy_label=FY)
            assert res["deactivated"] == 1

            active = alignment.get_alignment_selections(FY)
            assert len(active) == 1
            assert active[0]["msx_territory_id"] == "t1"

            # Row still exists, just inactive (non-destructive).
            assert AlignmentSelection.query.filter_by(fy_label=FY).count() == 2

    def test_reactivate_previously_removed(self, app):
        with app.app_context():
            from app.services import alignment

            alignment.save_alignment_selections([_terr("t1", "T1")], fy_label=FY)
            alignment.save_alignment_selections([], fy_label=FY)  # deactivate
            res = alignment.save_alignment_selections([_terr("t1", "T1")], fy_label=FY)

            assert res["reactivated"] == 1
            assert alignment.has_active_alignment(FY) is True


class TestOverrideToggle:
    """The alignment override on/off switch (UserPreference flag)."""

    def test_default_off(self, app):
        with app.app_context():
            from app.services import alignment
            assert alignment.is_override_active() is False

    def test_toggle_on_and_off(self, app):
        with app.app_context():
            from app.services import alignment
            assert alignment.set_override_active(True) is True
            assert alignment.is_override_active() is True
            assert alignment.set_override_active(False) is False
            assert alignment.is_override_active() is False


class TestAccountDiscovery:
    """discover_accounts_from_alignment - all accounts in selected territories."""

    def test_no_alignment_returns_empty(self, app):
        with app.app_context():
            from app.services import alignment
            result = alignment.discover_accounts_from_alignment(FY)
            assert result["success"] is True
            assert result["account_ids"] == []

    def test_returns_all_accounts_in_selected_territories(self, app):
        with app.app_context():
            from app.services import alignment

            alignment.save_alignment_selections(
                [_terr("t1", "T1"), _terr("t2", "T2")], fy_label=FY)

            accounts = {
                "success": True,
                "accounts": [
                    {"account_id": "a1", "territory_id": "t1", "territory_name": "T1", "tpid": 100},
                    {"account_id": "a2", "territory_id": "t1", "territory_name": "T1", "tpid": 100},
                    {"account_id": "a3", "territory_id": "t2", "territory_name": "T2", "tpid": 200},
                ],
            }
            with patch.object(alignment, "get_accounts_for_territories",
                              return_value=accounts) as mock_get:
                result = alignment.discover_accounts_from_alignment(FY)

            # Whole-territory: all account records kept, no seller scoping.
            assert set(result["account_ids"]) == {"a1", "a2", "a3"}
            assert result["territory_count"] == 2
            assert result["kept_account_count"] == 3
            # a1 and a2 share TPID 100, so 2 unique customers, not 3.
            assert result["customer_count"] == 2
            # Queried by the two selected territory names.
            called_names = sorted(mock_get.call_args[0][0])
            assert called_names == ["T1", "T2"]

    def test_discovery_propagates_msx_error(self, app):
        with app.app_context():
            from app.services import alignment

            alignment.save_alignment_selections([_terr("t1", "T1")], fy_label=FY)
            err = {"success": False, "error": "boom"}
            with patch.object(alignment, "get_accounts_for_territories",
                              return_value=err):
                result = alignment.discover_accounts_from_alignment(FY)
            assert result["success"] is False


class TestPreviewUnsaved:
    """summarize_accounts_for_territories - preview before saving."""

    def test_empty_returns_zeros_not_undefined(self, app):
        with app.app_context():
            from app.services import alignment
            result = alignment.summarize_accounts_for_territories([])
            assert result["success"] is True
            assert result["customer_count"] == 0
            assert result["territory_count"] == 0
            assert result["kept_account_count"] == 0

    def test_previews_arbitrary_names_without_saving(self, app):
        with app.app_context():
            from app.models import AlignmentSelection
            from app.services import alignment

            accounts = {
                "success": True,
                "accounts": [
                    {"account_id": "a1", "territory_id": "t1", "territory_name": "T1", "tpid": 100},
                    {"account_id": "a2", "territory_id": "t1", "territory_name": "T1", "tpid": 100},
                    {"account_id": "a3", "territory_id": "t2", "territory_name": "T2", "tpid": 200},
                ],
            }
            with patch.object(alignment, "get_accounts_for_territories",
                              return_value=accounts):
                result = alignment.summarize_accounts_for_territories(["T2", "T1"])

            assert result["customer_count"] == 2   # TPIDs 100, 200
            assert result["kept_account_count"] == 3
            assert result["territory_count"] == 2
            # Nothing was persisted by a preview.
            assert AlignmentSelection.query.filter_by(fy_label=FY).count() == 0

    def test_no_alignment_discovery_has_count_fields(self, app):
        with app.app_context():
            from app.services import alignment
            # No saved selections -> discovery must still return zeroed counts.
            result = alignment.discover_accounts_from_alignment(FY)
            assert result["customer_count"] == 0
            assert result["territory_count"] == 0
            assert result["kept_account_count"] == 0
