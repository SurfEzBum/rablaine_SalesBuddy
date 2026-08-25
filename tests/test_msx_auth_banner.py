"""Tests for the global reactive MSX authentication banner."""
from bs4 import BeautifulSoup


def _dismiss_onboarding():
    """Dismiss onboarding so it does not interfere with page parsing."""
    from app.models import UserPreference, db

    preference = UserPreference.query.first()
    preference.first_run_modal_dismissed = True
    db.session.commit()


class TestMsxAuthBannerPresence:
    """Tests that the global MSX auth banner is present on integrated pages."""

    def _assert_banner(self, response):
        assert response.status_code == 200
        soup = BeautifulSoup(response.data, 'html.parser')
        assert soup.find(id='authRequiredBanner') is not None
        assert soup.find(id='authSignInBtn') is not None

    def test_note_form_has_auth_banner(self, app, client, sample_data):
        """Test that the new note form includes the global auth banner."""
        with app.app_context():
            _dismiss_onboarding()
            from app.models import Customer

            customer = Customer.query.first()
            self._assert_banner(client.get(f'/note/new?customer_id={customer.id}'))

    def test_fill_my_day_has_auth_banner(self, app, client):
        """Test that Fill My Day includes the global auth banner."""
        with app.app_context():
            _dismiss_onboarding()
            self._assert_banner(client.get('/fill-my-day'))

    def test_milestone_tracker_has_auth_banner(self, app, client):
        """Test that Milestone Tracker includes the global auth banner."""
        with app.app_context():
            _dismiss_onboarding()
            self._assert_banner(client.get('/reports/milestone-tracker'))

    def test_milestone_view_has_auth_banner(self, app, client, sample_data):
        """Test that Milestone View includes the global auth banner."""
        with app.app_context():
            _dismiss_onboarding()
            from app.models import Customer, Milestone, db

            customer = Customer.query.first()
            milestone = Milestone(
                title='Test Milestone',
                customer_id=customer.id,
                url='https://example.com',
            )
            db.session.add(milestone)
            db.session.commit()
            self._assert_banner(client.get(f'/milestone/{milestone.id}'))


class TestMsxAuthBannerStructure:
    """Tests for the structure and behavior of the global auth banner."""

    def _get_fill_my_day(self, app, client):
        with app.app_context():
            _dismiss_onboarding()
            return client.get('/fill-my-day')

    def test_banner_has_status_element(self, app, client):
        """Test that the banner has a sign-in progress status element."""
        soup = BeautifulSoup(self._get_fill_my_day(app, client).data, 'html.parser')
        assert soup.find(id='authBannerStatus') is not None

    def test_banner_has_sign_in_button(self, app, client):
        """Test that the banner has a Sign In to Azure button."""
        soup = BeautifulSoup(self._get_fill_my_day(app, client).data, 'html.parser')
        button = soup.find(id='authSignInBtn')
        assert button is not None
        assert 'Sign In to Azure' in button.get_text()

    def test_banner_uses_reactive_sign_in_flow(self, app, client):
        """Test that JavaScript starts and completes browser sign-in."""
        html = self._get_fill_my_day(app, client).data.decode('utf-8')
        assert 'window.startAuthSignIn' in html
        assert 'completeAuth' in html

    def test_banner_detects_auth_failures_reactively(self, app, client):
        """Test that failed MSX requests trigger the global banner."""
        html = self._get_fill_my_day(app, client).data.decode('utf-8')
        assert 'window.isAuthFailure' in html
        assert 'window.showAuthBanner' in html

    def test_banner_starts_hidden(self, app, client):
        """Test that the banner starts hidden."""
        soup = BeautifulSoup(self._get_fill_my_day(app, client).data, 'html.parser')
        banner = soup.find(id='authRequiredBanner')
        assert 'd-none' in banner.get('class', [])

    def test_banner_has_js_endpoints(self, app, client):
        """Test that banner JavaScript references current auth endpoints."""
        html = self._get_fill_my_day(app, client).data.decode('utf-8')
        assert '/api/msx/az-status' in html
        assert '/api/msx/az-login/start' in html
        assert '/api/msx/az-login/complete' in html


class TestAdminPanelAuthFlow:
    """Tests that the admin panel has the browser-based sign-in flow."""

    def _make_admin(self, app):
        """Make test user admin and dismiss onboarding."""
        from app.models import db, User, UserPreference
        user = User.query.first()
        user.is_admin = True
        pref = UserPreference.query.first()
        pref.first_run_modal_dismissed = True
        db.session.commit()

    def test_admin_panel_has_sign_in_button(self, app, client):
        """Test that admin panel has the Sign In to Azure button."""
        with app.app_context():
            self._make_admin(app)
            response = client.get('/admin')
            assert response.status_code == 200
            soup = BeautifulSoup(response.data, 'html.parser')
            btn = soup.find(id='adminStartAuthBtn')
            assert btn is not None, "Admin panel should have Sign In button"
            assert 'Sign In to Azure' in btn.get_text()

    def test_admin_panel_no_manual_instructions(self, app, client):
        """Test that admin panel no longer has manual CLI instructions."""
        with app.app_context():
            self._make_admin(app)
            response = client.get('/admin')
            html = response.data.decode('utf-8')
            assert 'How to Authenticate' not in html, "Manual instructions should be removed"
            assert 'az login --tenant' not in html, "CLI command should not appear"

    def test_admin_panel_has_auth_states(self, app, client):
        """Test that admin panel has all auth flow states."""
        with app.app_context():
            self._make_admin(app)
            response = client.get('/admin')
            soup = BeautifulSoup(response.data, 'html.parser')

            assert soup.find(id='adminAuthInitial') is not None
            assert soup.find(id='adminAuthWaiting') is not None
            assert soup.find(id='adminAuthSuccess') is not None
            assert soup.find(id='adminAuthError') is not None
            assert soup.find(id='adminAuthNoCli') is not None

    def test_admin_panel_has_cancel_button(self, app, client):
        """Test that admin panel has a cancel button during auth waiting."""
        with app.app_context():
            self._make_admin(app)
            response = client.get('/admin')
            soup = BeautifulSoup(response.data, 'html.parser')
            btn = soup.find(id='adminAuthCancelBtn')
            assert btn is not None
            assert 'Cancel' in btn.get_text()

    def test_admin_panel_still_has_test_connection_button(self, app, client):
        """Test that admin panel still has the Test Connection button."""
        with app.app_context():
            self._make_admin(app)
            response = client.get('/admin')
            soup = BeautifulSoup(response.data, 'html.parser')
            test_btn = soup.find(id='msxTestBtn')
            assert test_btn is not None, "Test Connection button should still exist"

    def test_admin_panel_auth_js_endpoints(self, app, client):
        """Test that admin panel JS references the correct auth endpoints."""
        with app.app_context():
            self._make_admin(app)
            response = client.get('/admin')
            html = response.data.decode('utf-8')
            assert '/api/msx/az-login/start' in html
            assert '/api/msx/az-login/complete' in html
            assert '/api/msx/az-status' in html
