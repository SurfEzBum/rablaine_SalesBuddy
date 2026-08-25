"""Tests for meeting-to-MSX activity coverage."""
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from app.models import (
    ActivityCoveragePopulation,
    Customer,
    DailyMeetingCache,
    Job,
    Milestone,
    MsxTask,
    Note,
    PrefetchedMeeting,
    PrefetchedMeetingAttendee,
    db,
)
from app.services import activity_coverage
from app.services import activity_enrichment


@pytest.fixture
def coverage_data(app):
    """Create one matched meeting and milestone for coverage tests."""
    with app.app_context():
        customer = Customer(name='Coverage Customer', tpid=987654321)
        db.session.add(customer)
        db.session.flush()
        milestone = Milestone(
            msx_milestone_id='coverage-milestone-guid',
            url='https://example.test/milestone',
            title='Deploy Fabric',
            msx_status='On Track',
            customer_id=customer.id,
        )
        db.session.add(milestone)
        db.session.flush()
        meeting = PrefetchedMeeting(
            workiq_id='coverage-meeting',
            subject='Fabric architecture workshop',
            start_time=datetime.combine(date.today(), datetime.min.time())
            + timedelta(hours=14, minutes=30),
            end_time=datetime.combine(date.today(), datetime.min.time())
            + timedelta(hours=14, minutes=30)
            + timedelta(minutes=45),
            meeting_date=date.today(),
            customer_id=customer.id,
            expires_at=datetime.now() + timedelta(days=5),
        )
        meeting.attendees.append(PrefetchedMeetingAttendee(
            name='Customer Person',
            email='person@coverage.test',
            domain='coverage.test',
            is_external=True,
        ))
        db.session.add(meeting)
        db.session.commit()
        ids = {
            'customer_id': customer.id,
            'milestone_id': milestone.id,
            'meeting_id': meeting.id,
        }
        yield ids
        MsxTask.query.filter_by(meeting_id=meeting.id).delete()
        PrefetchedMeetingAttendee.query.filter_by(meeting_id=meeting.id).delete()
        db.session.delete(db.session.get(PrefetchedMeeting, meeting.id))
        db.session.delete(db.session.get(Milestone, milestone.id))
        db.session.delete(db.session.get(Customer, customer.id))
        db.session.commit()


def test_report_status_and_defaults(app, coverage_data):
    """Matched meeting starts ready only after milestone selection."""
    with app.app_context():
        report = activity_coverage.get_report_data()
        row = next(item for item in report['meetings']
                   if item['id'] == coverage_data['meeting_id'])
        assert row['status'] == 'needs_milestone'
        assert row['draft_task_category'] == 861980004
        assert row['draft_duration_minutes'] == 45
        assert 'Customer Person' in row['draft_description']

        activity_coverage.update_meeting_draft(row['id'], {
            'customer_id': coverage_data['customer_id'],
            'milestone_id': coverage_data['milestone_id'],
            'subject': 'Architecture session with Coverage Customer',
            'description': 'Reviewed target Fabric architecture.',
            'task_category': 861980004,
            'duration_minutes': 45,
        })
        updated = activity_coverage.get_report_data()
        updated_row = next(item for item in updated['meetings']
                           if item['id'] == row['id'])
        assert updated_row['status'] == 'ready'


def test_create_activity_is_idempotent(app, coverage_data):
    """One meeting can create at most one MSX activity."""
    with app.app_context():
        activity_coverage.update_meeting_draft(coverage_data['meeting_id'], {
            'customer_id': coverage_data['customer_id'],
            'milestone_id': coverage_data['milestone_id'],
            'subject': 'Coverage activity',
            'description': 'Customer call details',
            'task_category': 861980000,
            'duration_minutes': 30,
        })
        result = {
            'success': True,
            'task_id': 'coverage-task-guid',
            'task_url': 'https://example.test/task',
        }
        with patch('app.services.msx_api.create_task', return_value=result) as create:
            first = activity_coverage.create_meeting_activity(
                coverage_data['meeting_id'],
            )
            second = activity_coverage.create_meeting_activity(
                coverage_data['meeting_id'],
            )

        assert first.id == second.id
        create.assert_called_once_with(
            milestone_id='coverage-milestone-guid',
            subject='Coverage activity',
            task_category=861980000,
            duration_minutes=30,
            description='Customer call details',
            start_date=f'{date.today().isoformat()}T14:30:00+00:00',
            due_date=f'{date.today().isoformat()}T15:00:00+00:00',
        )
        assert first.due_date == datetime.combine(
            date.today(), datetime.min.time(),
        ) + timedelta(hours=15)


def test_link_existing_activity(app, coverage_data):
    """Imported MSX task can be confirmed as meeting coverage."""
    with app.app_context():
        task = MsxTask(
            msx_task_id='existing-coverage-task',
            subject='Existing customer activity',
            task_category=861980000,
            task_category_name='Customer Engagement',
            duration_minutes=60,
            is_hok=False,
            due_date=datetime.combine(date.today(), datetime.min.time()),
            milestone_id=coverage_data['milestone_id'],
        )
        db.session.add(task)
        db.session.commit()

        linked = activity_coverage.link_existing_activity(
            coverage_data['meeting_id'], task.id,
        )
        assert linked.meeting_id == coverage_data['meeting_id']
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        assert meeting.milestone_id == coverage_data['milestone_id']
        db.session.delete(task)
        db.session.commit()


def test_reconcile_uses_note_call_date(app, coverage_data):
    """Unique note-backed activity links even when its due date is next day."""
    with app.app_context():
        note = Note(
            customer_id=coverage_data['customer_id'],
            call_date=datetime.combine(date.today(), datetime.min.time()),
            content='Customer meeting notes',
        )
        db.session.add(note)
        db.session.flush()
        task = MsxTask(
            msx_task_id='historical-note-task',
            subject='Different but valid activity subject',
            task_category=861980000,
            task_category_name='Customer Engagement',
            duration_minutes=60,
            is_hok=False,
            due_date=datetime.combine(
                date.today() + timedelta(days=1), datetime.min.time(),
            ),
            note_id=note.id,
            milestone_id=coverage_data['milestone_id'],
        )
        db.session.add(task)
        db.session.commit()

        result = activity_coverage.reconcile_existing_activities()

        assert result['linked'] == 1
        assert task.meeting_id == coverage_data['meeting_id']
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        assert meeting.note_id == note.id
        db.session.delete(task)
        db.session.delete(note)
        db.session.commit()


def test_reconcile_leaves_ambiguous_note_activity_unlinked(app, coverage_data):
    """Multiple same-customer meetings require explicit user confirmation."""
    with app.app_context():
        second_meeting = PrefetchedMeeting(
            workiq_id='second-coverage-meeting',
            subject='Second customer meeting',
            start_time=datetime.combine(date.today(), datetime.min.time()),
            meeting_date=date.today(),
            customer_id=coverage_data['customer_id'],
            expires_at=datetime.now() + timedelta(days=5),
        )
        note = Note(
            customer_id=coverage_data['customer_id'],
            call_date=datetime.combine(date.today(), datetime.min.time()),
            content='Ambiguous customer meeting notes',
        )
        db.session.add_all([second_meeting, note])
        db.session.flush()
        task = MsxTask(
            msx_task_id='ambiguous-note-task',
            subject='Customer follow-up',
            task_category=861980000,
            duration_minutes=60,
            is_hok=False,
            due_date=datetime.combine(date.today(), datetime.min.time()),
            note_id=note.id,
            milestone_id=coverage_data['milestone_id'],
        )
        db.session.add(task)
        db.session.commit()

        result = activity_coverage.reconcile_existing_activities()

        assert result['linked'] == 0
        assert result['ambiguous'] == 1
        assert task.meeting_id is None
        db.session.delete(task)
        db.session.delete(second_meeting)
        db.session.delete(note)
        db.session.commit()


def test_sync_and_reconcile_refreshes_tasks_before_matching(app):
    """Reconciliation refreshes local MSX tasks before matching meetings."""
    def task_sync():
        yield 1, 1, 'Tasks batch 1/1', 'ok'
        return {
            'success': True,
            'tasks_created': 2,
            'tasks_updated': 3,
            'error': '',
        }

    with app.app_context(), patch(
        'app.services.milestone_sync._sync_all_tasks',
        side_effect=task_sync,
    ), patch(
        'app.services.activity_coverage.reconcile_existing_activities',
        return_value={'scanned': 5, 'linked': 4, 'ambiguous': 1},
    ) as reconcile:
        activity_coverage._sync_and_reconcile()

    reconcile.assert_called_once_with()
    state = activity_coverage.get_reconciliation_status()
    assert state['tasks_created'] == 2
    assert state['tasks_updated'] == 3
    assert state['linked'] == 4
    assert state['ambiguous'] == 1


def test_enrichment_prefers_on_team_milestones():
    """Matcher tries team milestones before considering off-team milestones."""
    milestones = [
        {
            'local_id': 1, 'id': 'team-id', 'name': 'Team milestone',
            'status': 'On Track', 'opportunity': '', 'workload': 'Fabric',
            'on_my_team': True,
        },
        {
            'local_id': 2, 'id': 'other-id', 'name': 'Other milestone',
            'status': 'On Track', 'opportunity': '', 'workload': 'AI',
            'on_my_team': False,
        },
    ]
    with patch('app.services.activity_enrichment.gateway_call', return_value={
        'milestone_id': 'team-id',
        'reason': 'Strong team match',
    }) as gateway:
        result = activity_enrichment._match_milestone('Fabric workshop', milestones)

    assert result['milestone_id'] == 1
    assert result['on_my_team'] is True
    gateway.assert_called_once()


def test_enrichment_allows_off_team_fallback():
    """Matcher considers off-team milestones when team choices have no fit."""
    milestones = [
        {
            'local_id': 1, 'id': 'team-id', 'name': 'Unrelated milestone',
            'status': 'On Track', 'opportunity': '', 'workload': 'Security',
            'on_my_team': True,
        },
        {
            'local_id': 2, 'id': 'other-id', 'name': 'Relevant milestone',
            'status': 'At Risk', 'opportunity': '', 'workload': 'Fabric',
            'on_my_team': False,
        },
    ]
    with patch('app.services.activity_enrichment.gateway_call', side_effect=[
        {'milestone_id': None},
        {'milestone_id': 'other-id', 'reason': 'Best content match'},
    ]) as gateway:
        result = activity_enrichment._match_milestone('Fabric workshop', milestones)

    assert result['milestone_id'] == 2
    assert result['on_my_team'] is False
    assert gateway.call_count == 2


@pytest.mark.parametrize(('text', 'expected'), [
    ('Fabric architecture design session', 861980004),
    ('Customer L300 demo', 606820009),
    ('Resolve deployment blocker', 861980006),
    ('Azure adoption planning', 861980007),
    ('Build a rapid prototype', 606820006),
])
def test_enrichment_prefers_hok_task_categories(text, expected):
    """Prepared activity types always prefer an HoK-credit category."""
    category = activity_enrichment._category_for_text(text)

    assert category == expected
    assert category in activity_enrichment.HOK_TASK_CATEGORIES


@pytest.mark.parametrize(('text', 'expected'), [
    ('Complete customer readiness assessment', 861980014),
    ('Review RFP response', 861980009),
    ('Routine customer conversation', 861980000),
])
def test_enrichment_uses_non_hok_fallback_when_needed(text, expected):
    """Unmatched intent keeps an accurate non-HoK category."""
    category = activity_enrichment._category_for_text(text)

    assert category == expected
    assert category not in activity_enrichment.HOK_TASK_CATEGORIES


def test_enrichment_refreshes_local_milestones_before_matching():
    """Preparation consumes the batched sync and requires its completion."""
    events = iter([
        'event: start\ndata: {"total": 2}\n\n',
        'event: complete\ndata: {"success": true, "synced": 2}\n\n',
    ])
    with patch(
        'app.services.milestone_sync.sync_all_customer_milestones_stream',
        return_value=events,
    ) as sync:
        result = activity_enrichment._refresh_local_milestones()

    sync.assert_called_once_with()
    assert result == {'success': True, 'synced': 2}


def test_enrichment_status_reports_msx_refresh_phase(app, coverage_data):
    """Running job with meetings still queued reports MSX refresh feedback."""
    with app.app_context():
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        meeting.enrichment_status = activity_enrichment.STATUS_QUEUED
        job = Job(
            job_type=activity_enrichment.JOB_TYPE,
            status=Job.STATUS_RUNNING,
            dedupe_key=activity_enrichment.JOB_DEDUPE_KEY,
        )
        db.session.add(job)
        db.session.commit()

        status = activity_enrichment.get_enrichment_status()

        assert status['phase'] == 'refreshing_msx'
        db.session.delete(job)
        meeting.enrichment_status = None
        db.session.commit()


def test_enrichment_job_persists_result(app, coverage_data):
    """Completed enrichment fills drafts and suggested milestone durably."""
    with app.app_context():
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        meeting.enrichment_status = activity_enrichment.STATUS_QUEUED
        db.session.commit()
        enriched = {
            'summary': 'Discussed Fabric architecture and implementation steps.',
            'task_subject': 'Document Fabric architecture',
            'task_description': 'Capture the agreed implementation plan.',
            'task_category': 861980004,
            'milestone_id': coverage_data['milestone_id'],
            'match_reason': 'Architecture work advances this milestone.',
            'matched_on_team': True,
            'used_fallback': False,
        }
        with patch(
            'app.services.activity_enrichment._enrich_external',
            return_value=enriched,
        ), patch(
            'app.services.activity_enrichment._refresh_local_milestones',
            return_value={'success': True},
        ):
            result = activity_enrichment.process_enrichment_job({
                'meeting_ids': [meeting.id],
            })

        assert result == {'total': 1, 'completed': 1, 'failed': 0}
        assert meeting.enrichment_status == activity_enrichment.STATUS_COMPLETE
        assert meeting.enrichment_summary.startswith('Discussed Fabric')
        assert meeting.suggested_milestone_id == coverage_data['milestone_id']
        assert meeting.milestone_id == coverage_data['milestone_id']
        assert meeting.draft_subject == 'Document Fabric architecture'
        assert meeting.draft_task_category == 861980004
        meeting.enrichment_status = None
        meeting.enrichment_summary = None
        meeting.suggested_milestone_id = None
        meeting.milestone_id = None
        meeting.draft_subject = None
        meeting.draft_description = None
        meeting.draft_task_category = None
        meeting.enriched_at = None
        db.session.commit()


def test_enrichment_preserves_manual_draft_choices(app, coverage_data):
    """Batch stores its suggestion without replacing user-reviewed fields."""
    with app.app_context():
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        meeting.enrichment_status = activity_enrichment.STATUS_QUEUED
        meeting.milestone_id = coverage_data['milestone_id']
        meeting.draft_subject = 'Keep reviewed subject'
        meeting.draft_description = 'Keep reviewed description'
        meeting.draft_task_category = 861980002
        db.session.commit()
        enriched = {
            'summary': 'Stored source context.',
            'task_subject': 'Replacement subject',
            'task_description': 'Replacement description',
            'task_category': 861980004,
            'milestone_id': coverage_data['milestone_id'],
            'match_reason': 'Suggested from transcript.',
            'matched_on_team': True,
            'used_fallback': False,
        }
        with patch(
            'app.services.activity_enrichment._enrich_external',
            return_value=enriched,
        ), patch(
            'app.services.activity_enrichment._refresh_local_milestones',
            return_value={'success': True},
        ):
            activity_enrichment.process_enrichment_job({
                'meeting_ids': [meeting.id],
            })

        assert meeting.draft_subject == 'Keep reviewed subject'
        assert meeting.draft_description == 'Keep reviewed description'
        assert meeting.draft_task_category == 861980002
        assert meeting.suggested_milestone_id == coverage_data['milestone_id']
        meeting.enrichment_status = None
        meeting.enrichment_summary = None
        meeting.suggested_milestone_id = None
        meeting.milestone_id = None
        meeting.draft_subject = None
        meeting.draft_description = None
        meeting.draft_task_category = None
        meeting.enriched_at = None
        db.session.commit()


def test_enrichment_job_resumes_interrupted_running_row(app, coverage_data):
    """A reclaimed durable job processes rows left running by a dead worker."""
    with app.app_context():
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        meeting.enrichment_status = activity_enrichment.STATUS_RUNNING
        db.session.commit()
        enriched = {
            'summary': 'Recovered summary.',
            'task_subject': 'Recovered task',
            'task_description': 'Recovered description',
            'task_category': 861980000,
            'milestone_id': coverage_data['milestone_id'],
            'match_reason': 'Recovered match.',
            'matched_on_team': True,
            'used_fallback': False,
        }
        with patch(
            'app.services.activity_enrichment._enrich_external',
            return_value=enriched,
        ), patch(
            'app.services.activity_enrichment._refresh_local_milestones',
            return_value={'success': True},
        ):
            result = activity_enrichment.process_enrichment_job({
                'meeting_ids': [meeting.id],
            })

        assert result['completed'] == 1
        assert meeting.enrichment_status == activity_enrichment.STATUS_COMPLETE
        meeting.enrichment_status = None
        meeting.enrichment_summary = None
        meeting.suggested_milestone_id = None
        meeting.milestone_id = None
        meeting.draft_subject = None
        meeting.draft_description = None
        meeting.draft_task_category = None
        meeting.enriched_at = None
        db.session.commit()


def test_population_imports_fiscal_year_then_catches_up(app):
    """First run starts July 1; later run starts after durable checkpoint."""
    with app.app_context():
        ActivityCoveragePopulation.query.delete()
        db.session.commit()
        try:
            with patch(
                'app.services.meeting_sync.sync_meetings_for_date',
                return_value=([], None),
            ) as sync:
                result = activity_coverage.populate_fiscal_year(date(2026, 7, 3))

            assert result == {'completed_count': 3, 'error': None}
            assert [call.args[0] for call in sync.call_args_list] == [
                '2026-07-01', '2026-07-02', '2026-07-03',
            ]
            row = db.session.get(ActivityCoveragePopulation, 1)
            assert row.populated_through == date(2026, 7, 3)

            with patch(
                'app.services.meeting_sync.sync_meetings_for_date',
                return_value=([], None),
            ) as sync:
                result = activity_coverage.populate_fiscal_year(date(2026, 7, 6))

            assert result == {'completed_count': 1, 'error': None}
            sync.assert_called_once_with('2026-07-06')
            assert row.populated_through == date(2026, 7, 6)
            status = activity_coverage.get_population_status(date(2026, 7, 6))
            assert status['label'] == 'Up to date'
            assert status['can_start'] is False
        finally:
            ActivityCoveragePopulation.query.delete()
            db.session.commit()


def test_population_retries_transient_failure(app):
    """A transient date failure recovers without pausing the population."""
    with app.app_context():
        ActivityCoveragePopulation.query.delete()
        db.session.commit()
        try:
            with patch(
                'app.services.meeting_sync.sync_meetings_for_date',
                side_effect=[
                    ([], None),
                    ([], 'Temporary Outlook error'),
                    ([], None),
                    ([], None),
                ],
            ) as sync, patch(
                'app.services.activity_coverage._wait_before_retry',
            ) as wait:
                result = activity_coverage.populate_fiscal_year(date(2026, 7, 3))

            assert result == {'completed_count': 3, 'error': None}
            assert [call.args[0] for call in sync.call_args_list] == [
                '2026-07-01', '2026-07-02', '2026-07-02', '2026-07-03',
            ]
            wait.assert_called_once_with(2)
        finally:
            ActivityCoveragePopulation.query.delete()
            db.session.commit()


def test_population_pauses_after_retries_and_resumes(app):
    """Retry exhaustion preserves checkpoint so Catch up retries failed date."""
    with app.app_context():
        ActivityCoveragePopulation.query.delete()
        db.session.commit()
        try:
            with patch(
                'app.services.meeting_sync.sync_meetings_for_date',
                side_effect=[
                    ([], None),
                    ([], 'WorkIQ timeout'),
                    ([], 'WorkIQ timeout'),
                    ([], 'WorkIQ timeout'),
                ],
            ) as sync, patch(
                'app.services.activity_coverage._wait_before_retry',
            ) as wait:
                result = activity_coverage.populate_fiscal_year(date(2026, 7, 3))

            assert result['completed_count'] == 1
            assert result['error'] == (
                'Paused at 2026-07-02 after 3 attempts: WorkIQ timeout'
            )
            assert [call.args[0] for call in sync.call_args_list] == [
                '2026-07-01', '2026-07-02', '2026-07-02', '2026-07-02',
            ]
            assert [call.args[0] for call in wait.call_args_list] == [2, 5]
            row = db.session.get(ActivityCoveragePopulation, 1)
            assert row.populated_through == date(2026, 7, 1)
            status = activity_coverage.get_population_status(date(2026, 7, 3))
            assert status['label'] == 'Retry Calendar Import'
            assert status['can_start'] is True

            with patch(
                'app.services.meeting_sync.sync_meetings_for_date',
                return_value=([], None),
            ) as sync:
                activity_coverage.populate_fiscal_year(date(2026, 7, 3))

            assert [call.args[0] for call in sync.call_args_list] == [
                '2026-07-02', '2026-07-03',
            ]
            assert row.populated_through == date(2026, 7, 3)
        finally:
            ActivityCoveragePopulation.query.delete()
            db.session.commit()


def test_report_week_is_clamped_to_current_fiscal_year(app):
    """Report navigation cannot escape current fiscal-year boundaries."""
    with app.app_context():
        data = activity_coverage.get_report_data(date(2020, 1, 1))
        fiscal_start, _ = activity_coverage.fiscal_year_bounds()
        assert data['week_start'] == activity_coverage.normalize_week_start(fiscal_start)
        assert data['can_go_previous'] is False


def test_full_fiscal_year_view_includes_other_weeks(app, coverage_data):
    """Full FY mode returns meetings outside the selected week."""
    with app.app_context():
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        meeting.meeting_date = date.today() - timedelta(days=14)
        meeting.start_time = datetime.combine(meeting.meeting_date, datetime.min.time())
        db.session.commit()

        weekly = activity_coverage.get_report_data(date.today())
        full_year = activity_coverage.get_report_data(date.today(), view_all=True)

        assert all(row['id'] != meeting.id for row in weekly['meetings'])
        assert any(row['id'] == meeting.id for row in full_year['meetings'])
        assert full_year['view_all'] is True


def test_calendar_resync_preserves_logged_meeting(app, coverage_data):
    """Canceled-meeting cleanup must retain durable activity coverage rows."""
    with app.app_context():
        task = MsxTask(
            msx_task_id='resync-preserved-task',
            subject='Logged activity',
            task_category=861980000,
            task_category_name='Customer Engagement',
            duration_minutes=60,
            is_hok=False,
            due_date=datetime.combine(date.today(), datetime.min.time()),
            meeting_id=coverage_data['meeting_id'],
            milestone_id=coverage_data['milestone_id'],
        )
        db.session.add(task)
        db.session.commit()

        with patch(
            'app.services.meeting_prefetch.prefetch_for_date_full',
            return_value=(1, [{'id': 'different-meeting'}], None),
        ):
            from app.services.meeting_sync import sync_meetings_for_date
            _, error = sync_meetings_for_date(date.today().isoformat())

        assert error is None
        assert db.session.get(PrefetchedMeeting, coverage_data['meeting_id']) is not None
        db.session.delete(task)
        DailyMeetingCache.query.filter_by(meeting_date=date.today()).delete()
        db.session.commit()


def test_report_page_and_hub_registration(client, coverage_data):
    """Report renders meeting workbench and is discoverable from reports hub."""
    response = client.get('/reports/activity-coverage')
    assert response.status_code == 200
    assert b'Activity Coverage' in response.data
    assert b'customer-picker-input' in response.data
    assert b'milestone-picker-input' in response.data
    assert b'\xe2\x98\x85 Architecture Design Session' in response.data
    assert b'Fabric architecture workshop' in response.data
    assert b'Create Activity' in response.data
    assert b'Find Existing Activities' in response.data
    assert b'Match Milestones' in response.data
    assert b'Import calendar meetings from the last completed day through today' in response.data
    assert b'Expand All' in response.data
    assert b'Weekly' in response.data
    assert b'Full FY' in response.data

    full_year = client.get('/reports/activity-coverage?view=all')
    assert full_year.status_code == 200
    assert b'coverage-view-toggle' in full_year.data
    assert b'meeting-month-heading' in full_year.data

    hub = client.get('/reports')
    assert hub.status_code == 200
    assert b'/reports/activity-coverage' in hub.data


def test_f1_help_explains_activity_coverage_workflow():
    """Contextual help distinguishes imports, matching, reconciliation, and creation."""
    help_script = Path('static/js/page-help.js').read_text(encoding='utf-8')

    assert "title: 'Activity Coverage'" in help_script
    assert '<strong>Re-run Matching</strong>' in help_script
    assert '<strong>Find Existing Activities</strong>' in help_script
    assert '<strong>Catch Up Calendar</strong>' in help_script
    assert '<strong>Full FY</strong>' in help_script
    assert 'qualifies for HoK credit' in help_script
    assert 'Nothing is created until you click it' in help_script


def test_reconciliation_routes(client):
    """Report can start reconciliation and poll its status."""
    with patch(
        'app.services.activity_coverage.start_reconciliation',
        return_value=True,
    ) as start:
        response = client.post('/api/reports/activity-coverage/reconcile')

    assert response.status_code == 200
    assert response.get_json()['success'] is True
    start.assert_called_once()

    status = client.get('/api/reports/activity-coverage/reconcile-status')
    assert status.status_code == 200
    assert status.get_json()['success'] is True


def test_enrichment_routes(client):
    """Report can queue enrichment and poll durable progress."""
    with patch(
        'app.services.activity_enrichment.start_enrichment',
        return_value={'started': True, 'job_id': 42, 'queued': 8},
    ) as start:
        response = client.post(
            '/api/reports/activity-coverage/match-milestones',
            json={'force': True},
        )

    assert response.status_code == 200
    assert response.get_json() == {
        'success': True,
        'started': True,
        'job_id': 42,
        'queued': 8,
    }
    start.assert_called_once_with(force=True)

    status = client.get('/api/reports/activity-coverage/match-status')
    assert status.status_code == 200
    assert status.get_json()['success'] is True


def test_force_enrichment_clears_prior_preparation(app, coverage_data):
    """Re-running replaces generated drafts but preserves meeting identity and customer."""
    with app.app_context(), patch(
        'app.services.activity_enrichment.enqueue',
    ) as enqueue:
        meeting = db.session.get(PrefetchedMeeting, coverage_data['meeting_id'])
        meeting.milestone_id = coverage_data['milestone_id']
        meeting.draft_subject = 'Old generated subject'
        meeting.draft_description = 'Old generated description'
        meeting.draft_task_category = 861980000
        meeting.enrichment_status = activity_enrichment.STATUS_COMPLETE
        meeting.enrichment_summary = 'Old summary'
        meeting.suggested_milestone_id = coverage_data['milestone_id']
        meeting.milestone_match_reason = 'Old reason'
        meeting.enrichment_attempts = 2
        db.session.commit()
        enqueue.return_value.id = 99

        result = activity_enrichment.start_enrichment(force=True)

        assert result == {'started': True, 'job_id': 99, 'queued': 1}
        assert meeting.customer_id == coverage_data['customer_id']
        assert meeting.milestone_id is None
        assert meeting.draft_subject is None
        assert meeting.draft_description is None
        assert meeting.draft_task_category is None
        assert meeting.enrichment_summary is None
        assert meeting.suggested_milestone_id is None
        assert meeting.milestone_match_reason is None
        assert meeting.enrichment_attempts == 0
        assert meeting.enrichment_status == activity_enrichment.STATUS_QUEUED
        enqueue.assert_called_once()


def test_milestone_options_include_team_membership(client, coverage_data):
    """Manual milestone choices expose team preference metadata."""
    response = client.get(
        '/api/reports/activity-coverage/customers/'
        f"{coverage_data['customer_id']}/milestones"
    )

    assert response.status_code == 200
    option = next(
        item for item in response.get_json()['milestones']
        if item['id'] == coverage_data['milestone_id']
    )
    assert isinstance(option['on_my_team'], bool)


def test_update_route_validates_required_subject(client, coverage_data):
    """Draft endpoint returns useful validation errors."""
    response = client.patch(
        f"/api/reports/activity-coverage/meetings/{coverage_data['meeting_id']}",
        json={
            'customer_id': coverage_data['customer_id'],
            'milestone_id': coverage_data['milestone_id'],
            'subject': '',
            'task_category': 861980000,
            'duration_minutes': 60,
        },
    )
    assert response.status_code == 400
    assert response.get_json()['error'] == 'Activity subject is required'