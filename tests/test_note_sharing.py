"""
Tests for note sharing — serialization, import, and API endpoints.
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import patch

from app.models import (
    db, Note, Customer, Seller, Territory, Topic, Partner, Milestone,
)
from app.services.note_sharing import serialize_note, import_shared_note


# ── Serialization tests ─────────────────────────────────────────────────────


class TestSerializeNote:
    """Test note → JSON serialization."""

    def test_serializes_full_note(self, app):
        """Note with customer, seller, territory, topics, milestone, partners."""
        with app.app_context():
            territory = Territory(name='US East')
            db.session.add(territory)
            db.session.flush()

            seller = Seller(name='Jane Doe', alias='janedoe', seller_type='Growth')
            db.session.add(seller)
            db.session.flush()
            seller.territories.append(territory)

            customer = Customer(
                name='Contoso', tpid=123456, tpid_url='https://msx/123456',
                website='contoso.com', favicon_b64='abc123',
                territory_id=territory.id, seller_id=seller.id,
            )
            db.session.add(customer)
            db.session.flush()

            topic = Topic(name='Azure SQL')
            db.session.add(topic)
            db.session.flush()

            milestone = Milestone(
                msx_milestone_id='ms-guid-001',
                milestone_number='7-100',
                url='https://msx/milestone/001',
                title='SQL Migration',
                msx_status='On Track',
                due_date=datetime(2026, 6, 1),
                dollar_value=150000.0,
                workload='Azure SQL',
                customer_id=customer.id,
            )
            db.session.add(milestone)
            db.session.flush()

            partner = Partner(name='Acme Consulting')
            db.session.add(partner)
            db.session.flush()

            note = Note(
                customer_id=customer.id,
                content='<p>Discussed SQL migration plan</p>',
                call_date=datetime(2026, 3, 14, 10, 30),
            )
            db.session.add(note)
            db.session.flush()
            note.topics.append(topic)
            note.milestones.append(milestone)
            note.partners.append(partner)
            db.session.commit()

            result = serialize_note(note)

            assert result['content'] == '<p>Discussed SQL migration plan</p>'
            assert '2026-03-14' in result['call_date']

            assert result['customer']['name'] == 'Contoso'
            assert result['customer']['tpid'] == 123456
            assert result['customer']['seller_name'] == 'Jane Doe'
            assert result['customer']['seller_alias'] == 'janedoe'
            assert result['customer']['seller_type'] == 'Growth'
            assert result['customer']['territory_name'] == 'US East'

            assert result['topics'] == ['Azure SQL']
            assert result['partners'] == ['Acme Consulting']

            ms = result['milestone']
            assert ms['msx_milestone_id'] == 'ms-guid-001'
            assert ms['title'] == 'SQL Migration'
            assert ms['dollar_value'] == 150000.0

    def test_serializes_minimal_note(self, app):
        """Note without customer or relationships serializes cleanly."""
        with app.app_context():
            note = Note(
                content='General note',
                call_date=datetime(2026, 3, 14),
            )
            db.session.add(note)
            db.session.commit()

            result = serialize_note(note)

            assert result['content'] == 'General note'
            assert 'customer' not in result
            assert 'topics' not in result
            assert 'milestone' not in result
            assert 'partners' not in result

    def test_skips_non_msx_milestones(self, app):
        """Milestones without msx_milestone_id are excluded."""
        with app.app_context():
            customer = Customer(name='TestCo', tpid=999999)
            db.session.add(customer)
            db.session.flush()

            # Milestone with no MSX ID (manually created)
            milestone = Milestone(
                url='https://example.com',
                title='Manual Milestone',
                customer_id=customer.id,
            )
            db.session.add(milestone)
            db.session.flush()

            note = Note(
                customer_id=customer.id,
                content='Test note',
                call_date=datetime(2026, 3, 14),
            )
            db.session.add(note)
            db.session.flush()
            note.milestones.append(milestone)
            db.session.commit()

            result = serialize_note(note)

            assert 'milestone' not in result


# ── Import tests ─────────────────────────────────────────────────────────────


class TestImportSharedNote:
    """Test importing a shared note into the local database."""

    def test_imports_with_new_customer(self, app):
        """Creates customer, seller, territory when TPID not found."""
        with app.app_context():
            note_data = {
                'content': '<p>Shared note content</p>',
                'call_date': '2026-03-14T10:30:00',
                'customer': {
                    'name': 'NewCorp',
                    'tpid': 777777,
                    'tpid_url': 'https://msx/777777',
                    'website': 'newcorp.com',
                    'territory_name': 'Central',
                    'seller_name': 'Bob Smith',
                    'seller_alias': 'bobsmith',
                    'seller_type': 'Acquisition',
                },
                'topics': ['Cosmos DB', 'Migration'],
            }

            result = import_shared_note(note_data, 'Alice')
            assert result['success'] is True
            assert result['customer_name'] == 'NewCorp'

            # Verify customer created
            customer = Customer.query.filter_by(tpid=777777).first()
            assert customer is not None
            assert customer.name == 'NewCorp'
            assert customer.website == 'newcorp.com'

            # Verify seller created and linked to territory
            seller = Seller.query.filter_by(alias='bobsmith').first()
            assert seller is not None
            assert seller.name == 'Bob Smith'
            assert seller.seller_type == 'Acquisition'
            territory = Territory.query.filter(
                db.func.lower(Territory.name) == 'central'
            ).first()
            assert territory is not None
            assert territory in seller.territories

            # Verify note created and linked
            note = Note.query.filter_by(customer_id=customer.id).first()
            assert note is not None
            assert '<p>Shared note content</p>' in note.content
            topic_names = {t.name for t in note.topics}
            assert 'Cosmos DB' in topic_names
            assert 'Migration' in topic_names

    def test_imports_with_existing_customer(self, app):
        """Attaches note to existing customer when TPID matches."""
        with app.app_context():
            # Pre-existing customer
            existing = Customer(name='ExistingCo', tpid=888888)
            db.session.add(existing)
            db.session.commit()
            existing_id = existing.id

            note_data = {
                'content': 'Note for existing customer',
                'call_date': '2026-03-14T10:30:00',
                'customer': {
                    'name': 'ExistingCo',
                    'tpid': 888888,
                    'territory_name': 'ShouldBeIgnored',
                    'seller_name': 'ShouldBeIgnored',
                },
            }

            result = import_shared_note(note_data, 'Bob')
            assert result['success'] is True

            note = Note.query.filter_by(customer_id=existing_id).first()
            assert note is not None
            assert note.content == 'Note for existing customer'

            # Should NOT have created a new territory or seller
            t = Territory.query.filter_by(name='ShouldBeIgnored').first()
            assert t is None

    def test_imports_with_milestone(self, app):
        """Creates milestone when msx_milestone_id not found."""
        with app.app_context():
            customer = Customer(name='MilestoneCo', tpid=555555)
            db.session.add(customer)
            db.session.commit()

            note_data = {
                'content': 'Note with milestone',
                'call_date': '2026-03-14T10:30:00',
                'customer': {
                    'name': 'MilestoneCo',
                    'tpid': 555555,
                },
                'milestone': {
                    'msx_milestone_id': 'ms-guid-new',
                    'milestone_number': '7-200',
                    'url': 'https://msx/milestone/new',
                    'title': 'New Milestone',
                    'msx_status': 'On Track',
                    'due_date': '2026-06-01T00:00:00',
                    'dollar_value': 50000.0,
                    'workload': 'Azure App Service',
                },
            }

            result = import_shared_note(note_data, 'Charlie')
            assert result['success'] is True

            milestone = Milestone.query.filter_by(msx_milestone_id='ms-guid-new').first()
            assert milestone is not None
            assert milestone.title == 'New Milestone'

            note = Note.query.get(result['note_id'])
            assert milestone in note.milestones

    def test_imports_with_existing_milestone(self, app):
        """Links to existing milestone when msx_milestone_id matches."""
        with app.app_context():
            customer = Customer(name='ExistMileCo', tpid=444444)
            db.session.add(customer)
            db.session.flush()

            existing_ms = Milestone(
                msx_milestone_id='ms-guid-existing',
                url='https://msx/milestone/existing',
                title='Existing MS',
                customer_id=customer.id,
            )
            db.session.add(existing_ms)
            db.session.commit()
            ms_id = existing_ms.id

            note_data = {
                'content': 'Note linking to existing milestone',
                'call_date': '2026-03-14T10:30:00',
                'customer': {'name': 'ExistMileCo', 'tpid': 444444},
                'milestone': {
                    'msx_milestone_id': 'ms-guid-existing',
                    'url': 'https://msx/milestone/existing',
                    'title': 'Existing MS',
                },
            }

            result = import_shared_note(note_data, 'Dana')
            assert result['success'] is True

            note = Note.query.get(result['note_id'])
            ms = Milestone.query.get(ms_id)
            assert ms in note.milestones

            # Should not have created a duplicate
            count = Milestone.query.filter_by(msx_milestone_id='ms-guid-existing').count()
            assert count == 1

    def test_links_existing_partners_by_name(self, app):
        """Partners that exist locally are linked to the imported note."""
        with app.app_context():
            partner = Partner(name='KnownPartner')
            db.session.add(partner)
            customer = Customer(name='PartnerCo', tpid=333333)
            db.session.add(customer)
            db.session.commit()

            note_data = {
                'content': 'Note with partner reference',
                'call_date': '2026-03-14T10:30:00',
                'customer': {'name': 'PartnerCo', 'tpid': 333333},
                'partners': ['KnownPartner', 'UnknownPartner'],
            }

            result = import_shared_note(note_data, 'Eve')
            assert result['success'] is True

            note = Note.query.get(result['note_id'])
            partner_names = {p.name for p in note.partners}
            assert 'KnownPartner' in partner_names
            assert 'UnknownPartner' not in partner_names

    def test_rejects_empty_content(self, app):
        """Note with no content is rejected."""
        with app.app_context():
            result = import_shared_note({'call_date': '2026-03-14T10:30:00'}, 'Fail')
            assert result['success'] is False

    def test_imports_without_customer(self, app):
        """Note with no customer data imports as a general note."""
        with app.app_context():
            note_data = {
                'content': 'General shared note',
                'call_date': '2026-03-14T10:30:00',
            }

            result = import_shared_note(note_data, 'Frank')
            assert result['success'] is True
            assert result['customer_name'] is None

            note = Note.query.get(result['note_id'])
            assert note.customer_id is None

    def test_reuses_existing_topic(self, app):
        """Existing topics are reused (case-insensitive), not duplicated."""
        with app.app_context():
            existing_topic = Topic(name='Azure SQL')
            db.session.add(existing_topic)
            db.session.commit()
            topic_id = existing_topic.id

            note_data = {
                'content': 'Note with existing topic',
                'call_date': '2026-03-14T10:30:00',
                'topics': ['azure sql'],  # Different case
            }

            result = import_shared_note(note_data, 'Grace')
            assert result['success'] is True

            note = Note.query.get(result['note_id'])
            assert len(note.topics) == 1
            assert note.topics[0].id == topic_id

    def test_reuses_existing_seller_by_alias(self, app):
        """Existing seller matched by alias is reused, not duplicated."""
        with app.app_context():
            seller = Seller(name='Old Name', alias='janedoe', seller_type='Growth')
            db.session.add(seller)
            db.session.commit()
            seller_id = seller.id

            note_data = {
                'content': 'Note triggering seller match',
                'call_date': '2026-03-14T10:30:00',
                'customer': {
                    'name': 'AliasCo',
                    'tpid': 222222,
                    'seller_name': 'Jane Doe',
                    'seller_alias': 'janedoe',
                    'seller_type': 'Growth',
                },
            }

            result = import_shared_note(note_data, 'Hank')
            assert result['success'] is True

            customer = Customer.query.filter_by(tpid=222222).first()
            assert customer.seller_id == seller_id


# ── API endpoint tests ───────────────────────────────────────────────────────


class TestNoteShareAPIEndpoints:
    """Test the note sharing API endpoints."""

    def test_serialize_note_endpoint(self, client, app):
        """GET /api/share/note/<id> returns serialized note."""
        with app.app_context():
            note = Note(content='API test note', call_date=datetime(2026, 3, 14))
            db.session.add(note)
            db.session.commit()
            nid = note.id

        resp = client.get(f'/api/share/note/{nid}')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['note']['content'] == 'API test note'

    def test_serialize_note_404(self, client):
        """GET /api/share/note/<id> returns 404 for missing note."""
        resp = client.get('/api/share/note/99999')
        assert resp.status_code == 404

    def test_receive_note_endpoint(self, client, app):
        """POST /api/share/receive-note imports a shared note."""
        resp = client.post('/api/share/receive-note', json={
            'sender_name': 'TestSender',
            'note': {
                'content': 'Received note content',
                'call_date': '2026-03-14T10:30:00',
                'customer': {
                    'name': 'APICo',
                    'tpid': 111111,
                },
            },
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['customer_name'] == 'APICo'

        with app.app_context():
            customer = Customer.query.filter_by(tpid=111111).first()
            assert customer is not None

    def test_receive_note_validation_empty(self, client):
        """POST /api/share/receive-note rejects empty payload."""
        resp = client.post('/api/share/receive-note', json={'note': None, 'sender_name': 'X'})
        assert resp.status_code == 400

    def test_receive_note_validation_no_body(self, client):
        """POST /api/share/receive-note rejects missing body."""
        resp = client.post('/api/share/receive-note',
                           data='not json',
                           content_type='text/plain')
        assert resp.status_code in (400, 415)
