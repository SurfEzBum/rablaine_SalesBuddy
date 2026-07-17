"""
Regression test for the milestone-sync connectivity guard.

When MSX is unreachable (e.g. off-VPN), the streaming sync used to sail through
the write phase and report every active milestone as "deactivated" (the scary
"690 deactivated" run). It must instead abort with a vpn_blocked event and touch
nothing.
"""
from unittest.mock import patch

from app.models import db, Customer, Milestone
from app.services import milestone_sync


def test_stream_aborts_on_wholesale_fetch_failure(app, sample_data):
    """Off-VPN / connectivity failure -> vpn_blocked event, nothing deactivated."""
    with app.app_context():
        cust = Customer.query.filter(
            Customer.tpid_url.isnot(None), Customer.tpid_url != ''
        ).first()

        ms = Milestone(
            url='https://example.com/ms',
            title='Keep me active',
            msx_milestone_id='guid-keepme-guard',
            msx_status='On Track',
            customer_id=cust.id,
            on_my_team=False,
        )
        db.session.add(ms)
        db.session.commit()
        ms_id = ms.id

        # Simulate off-VPN: account id extracts fine, but every opportunity
        # fetch fails.
        with patch.object(milestone_sync, 'extract_account_id_from_url',
                          return_value='acct-123'), \
                patch.object(milestone_sync, 'batch_get_opportunities',
                             return_value={'success': False}):
            events = list(milestone_sync.sync_all_customer_milestones_stream())

        joined = '\n'.join(events)
        # Aborted with a clear VPN message; never reached the deactivation phase.
        assert 'event: vpn_blocked' in joined
        assert 'event: complete' not in joined

        # The active milestone was NOT touched.
        refreshed = db.session.get(Milestone, ms_id)
        assert refreshed.last_synced_at is None
        assert refreshed.msx_status == 'On Track'
