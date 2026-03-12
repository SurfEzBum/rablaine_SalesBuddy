"""
Tests for CSAM and DAE features on customer view.
Covers: CustomerCSAM model, M2M relationships, CSAM selection endpoint,
DAE display, and template rendering.
"""
import pytest
from app.models import db, Customer, CustomerCSAM


@pytest.fixture
def csam_data(app, sample_data):
    """Create CSAM test data linked to sample customers."""
    with app.app_context():
        csam1 = CustomerCSAM(name='Carol CSAM', alias='carolc')
        csam2 = CustomerCSAM(name='Dave CSAM', alias='daved')
        csam3 = CustomerCSAM(name='Eve CSAM', alias='evee')
        db.session.add_all([csam1, csam2, csam3])
        db.session.flush()

        customer1 = db.session.get(Customer, sample_data['customer1_id'])
        customer1.dae_name = 'Frank DAE'
        customer1.dae_alias = 'frankd'
        customer1.available_csams.extend([csam1, csam2])

        customer2 = db.session.get(Customer, sample_data['customer2_id'])
        customer2.available_csams.append(csam3)

        db.session.commit()

        return {
            **sample_data,
            'csam1_id': csam1.id,
            'csam2_id': csam2.id,
            'csam3_id': csam3.id,
        }


class TestCustomerCSAMModel:
    """Tests for the CustomerCSAM model and relationships."""

    def test_csam_creation(self, app):
        """Test creating a CustomerCSAM record."""
        with app.app_context():
            csam = CustomerCSAM(name='Test CSAM', alias='testc')
            db.session.add(csam)
            db.session.commit()

            fetched = db.session.get(CustomerCSAM, csam.id)
            assert fetched.name == 'Test CSAM'
            assert fetched.alias == 'testc'
            assert fetched.created_at is not None

    def test_csam_get_email(self, app):
        """Test email generation from alias."""
        with app.app_context():
            csam = CustomerCSAM(name='Test', alias='testc')
            assert csam.get_email() == 'testc@microsoft.com'

    def test_csam_get_email_no_alias(self, app):
        """Test email returns None when no alias."""
        with app.app_context():
            csam = CustomerCSAM(name='Test')
            assert csam.get_email() is None

    def test_m2m_relationship(self, app, csam_data):
        """Test M2M between Customer and CustomerCSAM."""
        with app.app_context():
            customer = db.session.get(Customer, csam_data['customer1_id'])
            assert len(customer.available_csams) == 2
            names = {c.name for c in customer.available_csams}
            assert names == {'Carol CSAM', 'Dave CSAM'}

    def test_m2m_reverse(self, app, csam_data):
        """Test reverse M2M from CSAM to customers."""
        with app.app_context():
            csam = db.session.get(CustomerCSAM, csam_data['csam1_id'])
            assert len(csam.customers) == 1
            assert csam.customers[0].name == 'Acme Corp'

    def test_selected_csam(self, app, csam_data):
        """Test selecting a primary CSAM for a customer."""
        with app.app_context():
            customer = db.session.get(Customer, csam_data['customer1_id'])
            customer.csam_id = csam_data['csam1_id']
            db.session.commit()

            customer = db.session.get(Customer, csam_data['customer1_id'])
            assert customer.csam is not None
            assert customer.csam.name == 'Carol CSAM'

    def test_dae_fields(self, app, csam_data):
        """Test DAE fields on customer."""
        with app.app_context():
            customer = db.session.get(Customer, csam_data['customer1_id'])
            assert customer.dae_name == 'Frank DAE'
            assert customer.dae_alias == 'frankd'


class TestCsamSelectionEndpoint:
    """Tests for the CSAM selection AJAX endpoint."""

    def test_select_valid_csam(self, client, csam_data):
        """Test selecting a valid CSAM from available list."""
        cid = csam_data['customer1_id']
        resp = client.post(
            f'/customer/{cid}/csam',
            json={'csam_id': csam_data['csam1_id']},
            headers={'X-Requested-With': 'XMLHttpRequest'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['csam_id'] == csam_data['csam1_id']

    def test_select_invalid_csam(self, client, csam_data):
        """Test selecting a CSAM not in the customer's available list."""
        cid = csam_data['customer1_id']
        resp = client.post(
            f'/customer/{cid}/csam',
            json={'csam_id': csam_data['csam3_id']},  # csam3 belongs to customer2
            headers={'X-Requested-With': 'XMLHttpRequest'},
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data['success'] is False

    def test_clear_csam_selection(self, app, client, csam_data):
        """Test clearing the CSAM selection."""
        cid = csam_data['customer1_id']
        # First set a CSAM
        with app.app_context():
            customer = db.session.get(Customer, cid)
            customer.csam_id = csam_data['csam1_id']
            db.session.commit()

        # Clear it
        resp = client.post(
            f'/customer/{cid}/csam',
            json={'csam_id': None},
            headers={'X-Requested-With': 'XMLHttpRequest'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['csam_id'] is None

    def test_clear_csam_with_empty_string(self, client, csam_data):
        """Test clearing CSAM with empty string (from form select default)."""
        cid = csam_data['customer1_id']
        resp = client.post(
            f'/customer/{cid}/csam',
            json={'csam_id': ''},
            headers={'X-Requested-With': 'XMLHttpRequest'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['csam_id'] is None

    def test_nonexistent_customer(self, client):
        """Test CSAM update on nonexistent customer returns 404."""
        resp = client.post(
            '/customer/99999/csam',
            json={'csam_id': 1},
        )
        assert resp.status_code == 404


class TestCustomerViewTemplate:
    """Tests for DAE and CSAM display in customer view template."""

    def test_dae_displayed(self, client, csam_data):
        """Test DAE name and mailto link render on customer view."""
        cid = csam_data['customer1_id']
        resp = client.get(f'/customer/{cid}')
        assert resp.status_code == 200
        assert b'Frank DAE' in resp.data
        assert b'frankd@microsoft.com' in resp.data
        assert b'DAE (Account Owner)' in resp.data

    def test_csam_dropdown_rendered(self, client, csam_data):
        """Test CSAM dropdown renders when multiple CSAMs available."""
        cid = csam_data['customer1_id']
        resp = client.get(f'/customer/{cid}')
        assert resp.status_code == 200
        assert b'csamSelect' in resp.data
        assert b'Carol CSAM' in resp.data
        assert b'Dave CSAM' in resp.data

    def test_single_csam_has_dropdown_with_no_csam_option(self, client, csam_data):
        """Test single CSAM renders a dropdown with a 'No CSAM' option."""
        cid = csam_data['customer2_id']
        resp = client.get(f'/customer/{cid}')
        assert resp.status_code == 200
        assert b'Eve CSAM' in resp.data
        assert b'csamSelect' in resp.data
        assert 'No CSAM'.encode() in resp.data

    def test_no_csam_no_section(self, client, csam_data):
        """Test no CSAM section when customer has no available CSAMs."""
        cid = csam_data['customer3_id']
        resp = client.get(f'/customer/{cid}')
        assert resp.status_code == 200
        # Customer3 has no CSAMs, so CSAM label shouldn't appear
        # (checking that the label only appears in csam sections)
        html = resp.data.decode()
        assert 'csamSelect' not in html

    def test_no_dae_no_section(self, client, csam_data):
        """Test DAE section hidden when customer has no DAE."""
        cid = csam_data['customer3_id']
        resp = client.get(f'/customer/{cid}')
        assert resp.status_code == 200
        assert b'DAE (Account Owner)' not in resp.data

    def test_csam_preselected(self, app, client, csam_data):
        """Test dropdown shows correct CSAM as selected."""
        cid = csam_data['customer1_id']
        with app.app_context():
            customer = db.session.get(Customer, cid)
            customer.csam_id = csam_data['csam1_id']
            db.session.commit()

        resp = client.get(f'/customer/{cid}')
        html = resp.data.decode()
        # The selected option should have 'selected' attribute
        assert f'value="{csam_data["csam1_id"]}" selected' in html
