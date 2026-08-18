"""
Tests for native account access team discovery.

Account discovery for the sync no longer reads the msp_accountteams custom
entity (MSX stopped populating it with current alignments). It reads the
user's account access team memberships via teammembership_association,
filtered to the Account Access Team template. These tests validate that
extraction, pagination, error handling, and the scan_init wiring.
"""
from unittest.mock import MagicMock, patch

from app.services.msx_api import (
    ACCOUNT_ACCESS_TEAM_TEMPLATE_ID,
    get_my_account_team_ids,
    scan_init,
)


def _resp(status_code=200, json_body=None, text=""):
    """Build a fake requests.Response for _msx_request mocking."""
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body or {}
    r.text = text
    return r


def _team(account_id, name=None):
    """A teammembership_association row for an account access team."""
    return {
        "teamid": f"team-{account_id}",
        "name": name if name is not None else f"{account_id}+{ACCOUNT_ACCESS_TEAM_TEMPLATE_ID}",
        "_regardingobjectid_value": account_id,
    }


class TestGetMyAccountTeamIds:
    """Unit tests for get_my_account_team_ids()."""

    @patch("app.services.msx_api._msx_request")
    @patch("app.services.msx_api.get_current_user_id", return_value="user-1")
    def test_extracts_account_ids_from_regarding_value(self, _uid, mock_req):
        mock_req.return_value = _resp(json_body={"value": [
            _team("acc-aaa"), _team("acc-bbb"), _team("acc-ccc"),
        ]})
        result = get_my_account_team_ids()
        assert result["success"] is True
        assert result["team_count"] == 3
        assert set(result["account_ids"]) == {"acc-aaa", "acc-bbb", "acc-ccc"}

    @patch("app.services.msx_api._msx_request")
    @patch("app.services.msx_api.get_current_user_id", return_value="user-1")
    def test_falls_back_to_parsing_team_name(self, _uid, mock_req):
        # Row with no regarding lookup value -> parse "{accid}+{template}"
        row = {
            "teamid": "team-x",
            "name": f"acc-fromname+{ACCOUNT_ACCESS_TEAM_TEMPLATE_ID}",
            "_regardingobjectid_value": None,
        }
        mock_req.return_value = _resp(json_body={"value": [row]})
        result = get_my_account_team_ids()
        assert result["success"] is True
        assert result["account_ids"] == ["acc-fromname"]

    @patch("app.services.msx_api._msx_request")
    @patch("app.services.msx_api.get_current_user_id", return_value="user-1")
    def test_dedupes_account_ids(self, _uid, mock_req):
        mock_req.return_value = _resp(json_body={"value": [
            _team("acc-dup"), _team("ACC-DUP"), _team("acc-dup"),
        ]})
        result = get_my_account_team_ids()
        # Lowercased + deduped
        assert result["account_ids"] == ["acc-dup"]

    @patch("app.services.msx_api._msx_request")
    @patch("app.services.msx_api.get_current_user_id", return_value="user-1")
    def test_follows_pagination(self, _uid, mock_req):
        page1 = _resp(json_body={
            "value": [_team("acc-1")],
            "@odata.nextLink": "https://msx/next-page",
        })
        page2 = _resp(json_body={"value": [_team("acc-2")]})
        mock_req.side_effect = [page1, page2]
        result = get_my_account_team_ids()
        assert result["success"] is True
        assert set(result["account_ids"]) == {"acc-1", "acc-2"}
        assert mock_req.call_count == 2

    @patch("app.services.msx_api._msx_request")
    @patch("app.services.msx_api.get_current_user_id", return_value="user-1")
    def test_filters_by_account_access_team_template(self, _uid, mock_req):
        mock_req.return_value = _resp(json_body={"value": []})
        get_my_account_team_ids()
        called_url = mock_req.call_args[0][1]
        assert ACCOUNT_ACCESS_TEAM_TEMPLATE_ID in called_url
        assert "teammembership_association" in called_url

    @patch("app.services.msx_api._msx_request")
    @patch("app.services.msx_api.get_current_user_id", return_value="user-1")
    def test_empty_result_is_success_with_no_accounts(self, _uid, mock_req):
        mock_req.return_value = _resp(json_body={"value": []})
        result = get_my_account_team_ids()
        assert result["success"] is True
        assert result["account_ids"] == []

    @patch("app.services.msx_api.is_vpn_blocked", return_value=True)
    @patch("app.services.msx_api._msx_request")
    @patch("app.services.msx_api.get_current_user_id", return_value="user-1")
    def test_vpn_blocked_surfaced(self, _uid, mock_req, _vpn):
        mock_req.return_value = _resp(status_code=403, text="blocked")
        result = get_my_account_team_ids()
        assert result["success"] is False
        assert result.get("vpn_blocked") is True
        assert result["account_ids"] == []

    @patch("app.services.msx_api._msx_request")
    @patch("app.services.msx_api.get_current_user_id", return_value="user-1")
    def test_http_error_surfaced(self, _uid, mock_req):
        mock_req.return_value = _resp(status_code=500, text="boom")
        result = get_my_account_team_ids()
        assert result["success"] is False
        assert "500" in result["error"]
        assert result["account_ids"] == []

    @patch("app.services.msx_api.get_current_user_id", return_value=None)
    def test_no_user_id(self, _uid):
        result = get_my_account_team_ids()
        assert result["success"] is False
        assert result["account_ids"] == []


class TestScanInitUsesAccessTeam:
    """scan_init() should discover accounts via the native access team path."""

    @patch("app.services.msx_api.get_my_account_team_ids")
    @patch("app.services.msx_api.get_current_user")
    def test_scan_init_returns_accounts_and_role(self, mock_user, mock_team):
        mock_user.return_value = {
            "success": True,
            "user_id": "user-1",
            "user": {"fullname": "Alex Blaine", "msp_qualifier2": "Cloud & AI Data"},
        }
        mock_team.return_value = {
            "success": True,
            "account_ids": ["acc-1", "acc-2"],
            "team_count": 2,
        }
        result = scan_init()
        assert result["success"] is True
        assert result["role"] == "Data SE"
        assert result["total_accounts"] == 2
        assert set(result["account_ids"]) == {"acc-1", "acc-2"}
        assert result["user"]["name"] == "Alex Blaine"

    @patch("app.services.msx_api.get_my_account_team_ids")
    @patch("app.services.msx_api.get_current_user")
    def test_scan_init_propagates_discovery_failure(self, mock_user, mock_team):
        mock_user.return_value = {
            "success": True,
            "user_id": "user-1",
            "user": {"fullname": "Alex Blaine", "msp_qualifier2": "Cloud & AI Data"},
        }
        mock_team.return_value = {
            "success": False,
            "error": "IP address is blocked — connect to VPN and retry.",
            "vpn_blocked": True,
            "account_ids": [],
        }
        result = scan_init()
        assert result["success"] is False
        assert result.get("vpn_blocked") is True

    @patch("app.services.msx_api.get_current_user")
    def test_scan_init_returns_user_error(self, mock_user):
        mock_user.return_value = {"success": False, "error": "Not authenticated."}
        result = scan_init()
        assert result["success"] is False
        assert "authenticated" in result["error"]
