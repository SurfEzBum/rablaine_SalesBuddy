"""Tests for the custom alignment sync service (app/services/alignment.py)."""

from unittest.mock import patch

import pytest


FY = "FY99"  # isolated fiscal-year label for tests


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
                seller_msx_user_id="u1", seller_name="Tim O'Shea",
                seller_type="Acquisition",
            ))
            db.session.commit()

            assert AlignmentTerritory.query.count() == 1
            sel = AlignmentSelection.query.first()
            assert sel.active is True
            assert sel.seller_name == "Tim O'Shea"

    def test_selection_unique_constraint(self, app):
        with app.app_context():
            from app.models import AlignmentSelection, db

            db.session.add(AlignmentSelection(
                fy_label=FY, msx_territory_id="t1", territory_name="T1",
                seller_msx_user_id="u1", seller_name="A",
            ))
            db.session.commit()

            db.session.add(AlignmentSelection(
                fy_label=FY, msx_territory_id="t1", territory_name="T1",
                seller_msx_user_id="u1", seller_name="A dup",
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
                result = alignment.probe_territories()

            assert result["success"] is True
            assert result["created"] == 2
            assert AlignmentTerritory.query.count() == 2

            # Re-probe with a changed name -> update, not duplicate.
            page2 = {
                "success": True,
                "records": [
                    {"territoryid": "t1", "name": "East.SMECC.SOU.0206.RENAMED",
                     "msp_accountteamunitname": "East.SMECC.SOU"},
                ],
                "next_link": None,
            }
            with patch.object(alignment, "query_entity", return_value=page2):
                result2 = alignment.probe_territories()

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
                result = alignment.probe_territories()

            assert result["total"] == 2
            assert AlignmentTerritory.query.count() == 2


class TestSellerDiscovery:
    """discover_sellers_for_territories aggregation."""

    def test_sellers_grouped_by_territory(self, app):
        with app.app_context():
            from app.services import alignment

            accounts = {
                "success": True,
                "accounts": [
                    {"account_id": "a1", "territory_id": "t1", "territory_name": "T1"},
                    {"account_id": "a2", "territory_id": "t1", "territory_name": "T1"},
                    {"account_id": "a3", "territory_id": "t1", "territory_name": "T1"},
                ],
            }
            teams = {
                "success": True,
                "account_sellers": {
                    "a1": {"name": "Tim", "type": "Acquisition", "user_id": "u1"},
                    "a2": {"name": "Rick", "type": "Growth", "user_id": "u2"},
                    "a3": {"name": "Tim", "type": "Acquisition", "user_id": "u1"},
                },
            }
            with patch.object(alignment, "get_accounts_for_territories",
                              return_value=accounts), \
                 patch.object(alignment, "batch_query_account_teams",
                              return_value=teams):
                result = alignment.discover_sellers_for_territories(["T1"])

            assert result["success"] is True
            assert len(result["territories"]) == 1
            terr = result["territories"][0]
            assert terr["account_count"] == 3
            sellers = {s["user_id"]: s for s in terr["sellers"]}
            assert sellers["u1"]["account_count"] == 2  # Tim on a1 + a3
            assert sellers["u2"]["account_count"] == 1  # Rick on a2

    def test_empty_territory_list(self, app):
        with app.app_context():
            from app.services import alignment
            result = alignment.discover_sellers_for_territories([])
            assert result == {"success": True, "territories": []}


class TestSelectionPersistence:
    """save/get alignment selections."""

    def test_save_and_get(self, app):
        with app.app_context():
            from app.services import alignment

            pairs = [
                {"msx_territory_id": "t1", "territory_name": "T1",
                 "seller_msx_user_id": "u1", "seller_name": "Tim",
                 "seller_type": "Acquisition"},
                {"msx_territory_id": "t1", "territory_name": "T1",
                 "seller_msx_user_id": "u2", "seller_name": "Rick",
                 "seller_type": "Growth"},
            ]
            res = alignment.save_alignment_selections(pairs, fy_label=FY)
            assert res["added"] == 2

            saved = alignment.get_alignment_selections(FY)
            assert len(saved) == 2
            assert alignment.has_active_alignment(FY) is True

    def test_edit_deactivates_removed_pairs(self, app):
        with app.app_context():
            from app.models import AlignmentSelection
            from app.services import alignment

            pairs = [
                {"msx_territory_id": "t1", "territory_name": "T1",
                 "seller_msx_user_id": "u1", "seller_name": "Tim"},
                {"msx_territory_id": "t1", "territory_name": "T1",
                 "seller_msx_user_id": "u2", "seller_name": "Rick"},
            ]
            alignment.save_alignment_selections(pairs, fy_label=FY)

            # Re-save keeping only u1 -> u2 becomes inactive (not deleted).
            res = alignment.save_alignment_selections([pairs[0]], fy_label=FY)
            assert res["deactivated"] == 1

            active = alignment.get_alignment_selections(FY)
            assert len(active) == 1
            assert active[0]["seller_msx_user_id"] == "u1"

            # Row still exists in the table, just inactive (non-destructive).
            assert AlignmentSelection.query.filter_by(fy_label=FY).count() == 2

    def test_reactivate_previously_removed(self, app):
        with app.app_context():
            from app.services import alignment

            pair = {"msx_territory_id": "t1", "territory_name": "T1",
                    "seller_msx_user_id": "u1", "seller_name": "Tim"}
            alignment.save_alignment_selections([pair], fy_label=FY)
            alignment.save_alignment_selections([], fy_label=FY)  # deactivate
            res = alignment.save_alignment_selections([pair], fy_label=FY)  # bring back

            assert res["reactivated"] == 1
            assert alignment.has_active_alignment(FY) is True


class TestAccountDiscovery:
    """discover_accounts_from_alignment seller scoping."""

    def test_no_alignment_returns_empty(self, app):
        with app.app_context():
            from app.services import alignment
            result = alignment.discover_accounts_from_alignment(FY)
            assert result["success"] is True
            assert result["account_ids"] == []

    def test_scopes_accounts_to_selected_sellers(self, app):
        with app.app_context():
            from app.services import alignment

            # Select only seller u1 in territory t1.
            alignment.save_alignment_selections([
                {"msx_territory_id": "t1", "territory_name": "T1",
                 "seller_msx_user_id": "u1", "seller_name": "Tim"},
            ], fy_label=FY)

            accounts = {
                "success": True,
                "accounts": [
                    {"account_id": "a1", "territory_id": "t1", "territory_name": "T1"},
                    {"account_id": "a2", "territory_id": "t1", "territory_name": "T1"},
                    {"account_id": "a3", "territory_id": "t1", "territory_name": "T1"},
                ],
            }
            teams = {
                "success": True,
                "account_sellers": {
                    "a1": {"name": "Tim", "type": "Acq", "user_id": "u1"},
                    "a2": {"name": "Other", "type": "Growth", "user_id": "u2"},
                    "a3": {"name": "Tim", "type": "Acq", "user_id": "u1"},
                },
            }
            with patch.object(alignment, "get_accounts_for_territories",
                              return_value=accounts), \
                 patch.object(alignment, "batch_query_account_teams",
                              return_value=teams):
                result = alignment.discover_accounts_from_alignment(FY)

            assert result["success"] is True
            # a2 (seller u2, not selected) is dropped; a1 + a3 kept.
            assert set(result["account_ids"]) == {"a1", "a3"}
            assert result["territory_account_count"] == 3
            assert result["kept_account_count"] == 2

    def test_account_with_no_seller_is_dropped(self, app):
        with app.app_context():
            from app.services import alignment

            alignment.save_alignment_selections([
                {"msx_territory_id": "t1", "territory_name": "T1",
                 "seller_msx_user_id": "u1", "seller_name": "Tim"},
            ], fy_label=FY)

            accounts = {
                "success": True,
                "accounts": [
                    {"account_id": "a1", "territory_id": "t1", "territory_name": "T1"},
                    {"account_id": "a2", "territory_id": "t1", "territory_name": "T1"},
                ],
            }
            teams = {
                "success": True,
                "account_sellers": {
                    "a1": {"name": "Tim", "type": "Acq", "user_id": "u1"},
                    # a2 has no seller mapped at all
                },
            }
            with patch.object(alignment, "get_accounts_for_territories",
                              return_value=accounts), \
                 patch.object(alignment, "batch_query_account_teams",
                              return_value=teams):
                result = alignment.discover_accounts_from_alignment(FY)

            assert set(result["account_ids"]) == {"a1"}
