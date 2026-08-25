"""Tests for native account v-team enrichment and account sync transport."""

from urllib.parse import parse_qs, urlparse
from unittest.mock import MagicMock, patch

from app.routes.msx import _derive_pod_name
from app.services.msx_api import (
    ACCOUNT_ACCESS_TEAM_TEMPLATE_ID,
    batch_query_account_teams,
)


def _response(records, next_link=None, status_code=200):
    """Build a fake Dataverse response."""
    response = MagicMock()
    response.status_code = status_code
    response.text = ""
    body = {"value": records}
    if next_link:
        body["@odata.nextLink"] = next_link
    response.json.return_value = body
    return response


def _member(
    account_id,
    name,
    user_id,
    qualifier2,
    title,
    *,
    qualifier1="Corporate",
    disabled=False,
):
    """Build one aliased FetchXML v-team result row."""
    alias = name.lower().replace(" ", ".")
    return {
        "_regardingobjectid_value": account_id,
        "member.systemuserid": user_id,
        "member.fullname": name,
        "member.internalemailaddress": f"{alias}@microsoft.com",
        "member.domainname": f"{alias}@microsoft.com",
        "member.title": title,
        "member.msp_qualifier1": qualifier1,
        "member.msp_qualifier2": qualifier2,
        "member.isdisabled": disabled,
    }


class TestNativeAccountVTeam:
    """Validate native v-team query construction and classification."""

    @patch("app.services.msx_api._msx_request")
    def test_maps_growth_acquisition_and_core_ses(self, mock_request):
        mock_request.return_value = _response([
            _member("acc-1", "Growth Seller", "seller-1", "Cloud & AI",
                    "Sr Digital Specialist"),
            _member("acc-2", "Acq Seller", "seller-2", "Cloud & AI-Acq",
                    "Digital Specialist C&AI LD"),
            _member("acc-1", "Data SE", "se-1", "Cloud & AI Data",
                    "Sr Digital Sol Engineer"),
            _member("acc-1", "Infra SE", "se-2", "Cloud & AI Infrastructure",
                    "Digital Sol Engineer"),
            _member("acc-1", "Apps SE", "se-3", "Cloud & AI Apps",
                    "Digital Sol Engineer"),
        ])

        result = batch_query_account_teams(["acc-1", "acc-2"])

        assert result["success"] is True
        assert result["account_sellers"]["acc-1"]["type"] == "Growth"
        assert result["account_sellers"]["acc-2"]["type"] == "Acquisition"
        assert result["account_sellers"]["acc-1"]["alias"] == "growth.seller"
        assert result["account_ses"]["acc-1"]["data_se"][0]["name"] == "Data SE"
        assert result["account_ses"]["acc-1"]["infra_se"][0]["name"] == "Infra SE"
        assert result["account_ses"]["acc-1"]["apps_se"][0]["name"] == "Apps SE"

        query = parse_qs(urlparse(mock_request.call_args.args[1]).query)
        fetchxml = query["fetchXml"][0]
        assert ACCOUNT_ACCESS_TEAM_TEMPLATE_ID in fetchxml
        assert 'name="teammembership"' in fetchxml
        assert 'name="systemuser"' in fetchxml
        assert "%Digital Specialist%" in fetchxml
        assert "%Sol Engineer%" in fetchxml

    @patch("app.services.msx_api._msx_request")
    def test_excludes_disabled_noncorporate_and_management_users(self, mock_request):
        mock_request.return_value = _response([
            _member("acc-1", "Disabled", "u1", "Cloud & AI",
                    "Digital Specialist", disabled=True),
            _member("acc-1", "Director", "u2", "Cloud & AI",
                    "Digital Specialist Director"),
            _member("acc-1", "Manager", "u3", "Cloud & AI-Acq",
                    "Digital Specialist Manager"),
            _member("acc-1", "Wrong Qualifier", "u4", "Cloud & AI",
                    "Digital Specialist", qualifier1="SME&C"),
        ])

        result = batch_query_account_teams(["acc-1"])

        assert result["account_sellers"] == {}
        assert result["unique_sellers"] == {}

    @patch("app.services.msx_api._msx_request")
    def test_multiple_sellers_leave_account_unassigned(self, mock_request, caplog):
        mock_request.return_value = _response([
            _member("acc-1", "Seller One", "u1", "Cloud & AI",
                    "Digital Specialist"),
            _member("acc-1", "Seller Two", "u2", "Cloud & AI-Acq",
                    "Digital Specialist"),
        ])

        result = batch_query_account_teams(["acc-1"])

        assert "acc-1" not in result["account_sellers"]
        assert "preserving any local assignment" in caplog.text

    @patch("app.services.msx_api._msx_request")
    def test_deduplicates_solution_engineers_by_user_id(self, mock_request):
        member = _member("acc-1", "Data SE", "se-1", "Cloud & AI Data",
                         "Digital Sol Engineer")
        mock_request.return_value = _response([member, member.copy()])

        result = batch_query_account_teams(["acc-1"])

        assert len(result["account_ses"]["acc-1"]["data_se"]) == 1


class TestPodDerivation:
    """POD is encoded in territory naming rather than an MSX relationship."""

    def test_standard_territory(self):
        assert _derive_pod_name("East.SMECC.MAA.0601") == "East POD 06"

    def test_suffixed_territory(self):
        assert _derive_pod_name("East.SMECC.HLA.0610.A") == "East POD 06"

    def test_malformed_territory(self):
        assert _derive_pod_name("Unassigned") is None


class TestAccountSyncTransport:
    """Account sync exposes the same generator through SSE and headless modes."""

    @patch("app.routes.msx.threading.Thread")
    @patch("app.routes.msx.get_msx_token", return_value="token")
    def test_headless_request_starts_background_sync(self, _token, mock_thread, client):
        response = client.post("/api/msx/accounts/sync")

        assert response.status_code == 202
        assert response.get_json()["async"] is True
        mock_thread.return_value.start.assert_called_once_with()

    @patch("app.routes.msx.get_msx_token", return_value="token")
    def test_sse_request_returns_event_stream(self, _token, client):
        response = client.post(
            "/api/msx/accounts/sync",
            headers={"Accept": "text/event-stream"},
            buffered=False,
        )

        assert response.status_code == 200
        assert response.mimetype == "text/event-stream"
        response.close()

    @patch("app.routes.msx.sync_accounts")
    def test_internal_headless_sync_consumes_sse_stream(self, mock_sync, app):
        consumed = []

        def stream():
            consumed.append('started')
            yield 'data: {"progress": 100}\n\n'
            consumed.append('finished')

        with app.app_context():
            from flask import Response
            from app.routes.msx import run_account_sync_headless

            mock_sync.return_value = Response(stream(), mimetype='text/event-stream')
            run_account_sync_headless()

        assert consumed == ['started', 'finished']
