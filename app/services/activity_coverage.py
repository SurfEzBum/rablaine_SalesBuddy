"""Meeting-to-MSX activity coverage workflow."""
from __future__ import annotations

import logging
import re
import threading
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

from flask import Flask

from app.models import (
    ActivityCoveragePopulation,
    Customer,
    Milestone,
    MsxTask,
    PrefetchedMeeting,
    db,
)
from app.services.msx_api import HOK_TASK_CATEGORIES, TASK_CATEGORIES

logger = logging.getLogger(__name__)

CUSTOMER_ENGAGEMENT = 861980000
INTERNAL = 861980012
_CATEGORY_VALUES = {item['value'] for item in TASK_CATEGORIES}
_CATEGORY_NAMES = {item['value']: item['label'] for item in TASK_CATEGORIES}
_DATE_SYNC_ATTEMPTS = 3
_DATE_RETRY_DELAYS = (2, 5)

_import_lock = threading.Lock()
_import_state_lock = threading.Lock()
_import_state: dict[str, Any] = {
    'running': False,
    'current_date': None,
    'completed_count': 0,
    'total_dates': 0,
    'attempt': 0,
    'retrying': False,
    'error': None,
}
_create_lock = threading.Lock()
_reconcile_lock = threading.Lock()
_reconcile_state_lock = threading.Lock()
_reconcile_state: dict[str, Any] = {
    'running': False,
    'phase': None,
    'scanned': 0,
    'linked': 0,
    'ambiguous': 0,
    'tasks_created': 0,
    'tasks_updated': 0,
    'error': None,
}


def _normalized_subject(value: str) -> str:
    """Normalize a meeting or activity subject for conservative comparison."""
    return ' '.join(re.findall(r'[a-z0-9]+', value.lower()))


def _subject_similarity(meeting: PrefetchedMeeting, task: MsxTask) -> float:
    """Return normalized similarity between meeting and activity subjects."""
    meeting_subject = _normalized_subject(meeting.draft_subject or meeting.subject)
    task_subject = _normalized_subject(task.subject)
    if not meeting_subject or not task_subject:
        return 0.0
    return SequenceMatcher(None, meeting_subject, task_subject).ratio()


def _task_activity_date(task: MsxTask) -> date | None:
    """Prefer linked note date because MSX task due dates are often one day later."""
    if task.note and task.note.call_date:
        return task.note.call_date.date()
    return task.due_date.date() if task.due_date else None


def fiscal_year_bounds(reference: date | None = None) -> tuple[date, date]:
    """Return current Microsoft fiscal-year start and end dates."""
    reference = reference or date.today()
    start_year = reference.year if reference.month >= 7 else reference.year - 1
    return date(start_year, 7, 1), date(start_year + 1, 6, 30)


def normalize_week_start(value: str | date | None = None) -> date:
    """Return Monday for a supplied date, defaulting to current week."""
    if isinstance(value, str):
        parsed = datetime.strptime(value, '%Y-%m-%d').date()
    else:
        parsed = value or date.today()
    return parsed - timedelta(days=parsed.weekday())


def _default_category(meeting: PrefetchedMeeting) -> int:
    subject = meeting.subject.lower()
    keyword_categories = (
        ('architecture', 861980004),
        ('whiteboard', 606820008),
        ('workshop', 861980001),
        ('demo', 861980002),
        ('proof of concept', 861980005),
        ('poc', 861980005),
        ('briefing', 861980008),
    )
    for keyword, category in keyword_categories:
        if keyword in subject:
            return category
    return (
        CUSTOMER_ENGAGEMENT
        if any(attendee.is_external for attendee in meeting.attendees)
        else INTERNAL
    )


def _default_duration(meeting: PrefetchedMeeting) -> int:
    if meeting.end_time and meeting.end_time > meeting.start_time:
        minutes = int((meeting.end_time - meeting.start_time).total_seconds() / 60)
        return max(1, min(minutes, 1440))
    return 60


def _default_description(meeting: PrefetchedMeeting) -> str:
    attendee_names = [
        attendee.name or attendee.email
        for attendee in meeting.attendees
        if attendee.name or attendee.email
    ]
    lines = [
        f"Customer meeting: {meeting.subject}",
        f"Date: {meeting.meeting_date.isoformat()}",
    ]
    if attendee_names:
        lines.append(f"Attendees: {', '.join(attendee_names)}")
    return '\n'.join(lines)


def _linked_task(meeting: PrefetchedMeeting) -> MsxTask | None:
    if meeting.activity:
        return meeting.activity
    if meeting.note_id and meeting.note:
        return meeting.note.msx_tasks.order_by(MsxTask.created_at.asc()).first()
    return None


def _status(meeting: PrefetchedMeeting, task: MsxTask | None) -> str:
    if task:
        return 'logged'
    if meeting.customer_id is None:
        return 'needs_customer'
    if meeting.milestone_id is None:
        return 'needs_milestone'
    return 'ready'


def _candidate_tasks(meeting: PrefetchedMeeting) -> list[dict[str, Any]]:
    if meeting.customer_id is None:
        return []
    tasks = (
        MsxTask.query.join(Milestone)
        .filter(Milestone.customer_id == meeting.customer_id)
        .filter(MsxTask.meeting_id.is_(None))
        .order_by(MsxTask.created_at.desc())
        .all()
    )
    tasks = [task for task in tasks if _task_activity_date(task) == meeting.meeting_date][:5]
    return [
        {
            'id': task.id,
            'subject': task.subject,
            'category': task.task_category_name,
            'milestone': task.milestone.display_text,
            'url': task.msx_task_url,
        }
        for task in tasks
    ]


def reconcile_existing_activities(today: date | None = None) -> dict[str, int]:
    """Link unique, high-confidence local MSX activities to fiscal-year meetings."""
    today = today or date.today()
    fiscal_start, fiscal_end = fiscal_year_bounds(today)
    meetings = (
        PrefetchedMeeting.query
        .filter(PrefetchedMeeting.meeting_date >= fiscal_start)
        .filter(PrefetchedMeeting.meeting_date <= min(today, fiscal_end))
        .filter(PrefetchedMeeting.dismissed.is_(False))
        .filter(~PrefetchedMeeting.activity.has())
        .all()
    )
    tasks = MsxTask.query.join(Milestone).filter(MsxTask.meeting_id.is_(None)).all()
    meetings_by_customer_date: dict[
        tuple[int, date], list[PrefetchedMeeting]
    ] = defaultdict(list)
    for meeting in meetings:
        if meeting.customer_id:
            meetings_by_customer_date[(meeting.customer_id, meeting.meeting_date)].append(
                meeting
            )

    task_matches: dict[int, list[PrefetchedMeeting]] = {}
    meeting_matches: dict[int, list[MsxTask]] = defaultdict(list)
    for task in tasks:
        activity_date = _task_activity_date(task)
        if not activity_date or not task.milestone:
            continue
        candidates = meetings_by_customer_date.get(
            (task.milestone.customer_id, activity_date),
            [],
        )
        if task.note_id:
            confident = candidates
        else:
            confident = [
                meeting for meeting in candidates
                if meeting.milestone_id == task.milestone_id
                or _subject_similarity(meeting, task) >= 0.68
            ]
        if confident:
            task_matches[task.id] = confident
            for meeting in confident:
                meeting_matches[meeting.id].append(task)

    linked = 0
    ambiguous = 0
    for task in tasks:
        candidates = task_matches.get(task.id, [])
        if len(candidates) != 1:
            ambiguous += int(bool(candidates))
            continue
        meeting = candidates[0]
        if len(meeting_matches[meeting.id]) != 1:
            ambiguous += 1
            continue
        task.meeting_id = meeting.id
        meeting.milestone_id = task.milestone_id
        if task.note_id and meeting.note_id is None:
            meeting.note_id = task.note_id
        linked += 1

    db.session.commit()
    return {'scanned': len(tasks), 'linked': linked, 'ambiguous': ambiguous}


def get_reconciliation_status() -> dict[str, Any]:
    """Return current activity refresh and reconciliation state."""
    with _reconcile_state_lock:
        return dict(_reconcile_state)


def _sync_and_reconcile() -> None:
    """Refresh MSX tasks for known milestones, then reconcile local meetings."""
    from app.services.milestone_sync import _sync_all_tasks

    with _reconcile_state_lock:
        _reconcile_state['phase'] = 'syncing'
    task_sync = _sync_all_tasks()
    try:
        while True:
            next(task_sync)
    except StopIteration as stop:
        sync_result = stop.value
    if not sync_result.get('success'):
        raise RuntimeError(sync_result.get('error') or 'MSX activity sync failed')

    with _reconcile_state_lock:
        _reconcile_state.update({
            'phase': 'matching',
            'tasks_created': sync_result.get('tasks_created', 0),
            'tasks_updated': sync_result.get('tasks_updated', 0),
        })
    result = reconcile_existing_activities()
    with _reconcile_state_lock:
        _reconcile_state.update(result)


def _reconciliation_worker(app: Flask) -> None:
    """Run activity reconciliation inside an application context."""
    try:
        with app.app_context():
            _sync_and_reconcile()
    except Exception as exc:
        logger.exception('Activity coverage reconciliation failed')
        with _reconcile_state_lock:
            _reconcile_state['error'] = str(exc)
    finally:
        with _reconcile_state_lock:
            _reconcile_state['running'] = False
            _reconcile_state['phase'] = None
        _reconcile_lock.release()


def start_reconciliation(app: Flask) -> bool:
    """Start one background MSX refresh and reconciliation pass."""
    if not _reconcile_lock.acquire(blocking=False):
        return False
    with _reconcile_state_lock:
        _reconcile_state.update({
            'running': True,
            'phase': 'starting',
            'scanned': 0,
            'linked': 0,
            'ambiguous': 0,
            'tasks_created': 0,
            'tasks_updated': 0,
            'error': None,
        })
    thread = threading.Thread(
        target=_reconciliation_worker,
        args=(app,),
        daemon=True,
        name='activity-coverage-reconciliation',
    )
    thread.start()
    return True


def _serialize_meeting(meeting: PrefetchedMeeting) -> dict[str, Any]:
    task = _linked_task(meeting)
    duration_minutes = _default_duration(meeting)
    milestones = []
    if meeting.customer_id:
        milestones = (
            Milestone.query.filter_by(customer_id=meeting.customer_id)
            .filter(Milestone.msx_milestone_id.isnot(None))
            .order_by(
                Milestone.on_my_team.desc(),
                Milestone.due_date.desc(),
                Milestone.title.asc(),
            )
            .all()
        )
    return {
        'id': meeting.id,
        'subject': meeting.subject,
        'start_time': meeting.start_time,
        'end_time': meeting.end_time,
        'is_all_day': duration_minutes >= 1440,
        'meeting_date': meeting.meeting_date,
        'is_recurring': meeting.is_recurring,
        'customer': meeting.customer,
        'customer_id': meeting.customer_id,
        'matched_via': meeting.matched_via,
        'attendees': meeting.attendees,
        'status': _status(meeting, task),
        'milestone_id': meeting.milestone_id,
        'selected_milestone': meeting.selected_milestone,
        'milestones': milestones,
        'draft_subject': meeting.draft_subject or meeting.subject,
        'draft_description': (
            meeting.draft_description
            if meeting.draft_description is not None
            else _default_description(meeting)
        ),
        'draft_task_category': meeting.draft_task_category or _default_category(meeting),
        'draft_duration_minutes': (
            meeting.draft_duration_minutes or duration_minutes
        ),
        'activity': task,
        'candidate_tasks': [] if task else _candidate_tasks(meeting),
        'note_id': meeting.note_id,
        'enrichment_status': meeting.enrichment_status,
        'enrichment_summary': meeting.enrichment_summary,
        'enrichment_error': meeting.enrichment_error,
        'enriched_at': meeting.enriched_at,
        'suggested_milestone': meeting.suggested_milestone,
        'milestone_match_reason': meeting.milestone_match_reason,
    }


def get_report_data(
    week_start: date | None = None,
    view_all: bool = False,
) -> dict[str, Any]:
    """Return weekly or full-fiscal-year meetings plus coverage totals."""
    today = date.today()
    fiscal_start, fiscal_end = fiscal_year_bounds(today)
    first_week = normalize_week_start(fiscal_start)
    current_week = normalize_week_start(today)
    requested_week = normalize_week_start(week_start)
    selected_start = max(first_week, min(requested_week, current_week))
    selected_end = selected_start + timedelta(days=6)

    visible_start = fiscal_start if view_all else max(selected_start, fiscal_start)
    visible_end = min(today, fiscal_end) if view_all else min(selected_end, today, fiscal_end)
    visible_rows = (
        PrefetchedMeeting.query
        .filter(PrefetchedMeeting.meeting_date >= visible_start)
        .filter(PrefetchedMeeting.meeting_date <= visible_end)
        .filter(PrefetchedMeeting.dismissed.is_(False))
        .order_by(PrefetchedMeeting.start_time.asc())
        .all()
    )
    fiscal_rows = (
        PrefetchedMeeting.query
        .filter(PrefetchedMeeting.meeting_date >= fiscal_start)
        .filter(PrefetchedMeeting.meeting_date <= min(fiscal_end, today))
        .filter(PrefetchedMeeting.dismissed.is_(False))
        .all()
    )
    statuses = [_status(row, _linked_task(row)) for row in fiscal_rows]
    logged = statuses.count('logged')
    total = len(statuses)
    return {
        'meetings': [_serialize_meeting(row) for row in visible_rows],
        'view_all': view_all,
        'week_start': selected_start,
        'week_end': selected_end,
        'previous_week': selected_start - timedelta(days=7),
        'next_week': selected_start + timedelta(days=7),
        'can_go_previous': selected_start > first_week,
        'can_go_next': selected_start < current_week,
        'today': today,
        'fiscal_start': fiscal_start,
        'fiscal_end': fiscal_end,
        'fiscal_year_label': f'FY{fiscal_end.year % 100:02d}',
        'summary': {
            'total': total,
            'logged': logged,
            'ready': statuses.count('ready'),
            'needs_attention': total - logged - statuses.count('ready'),
            'coverage_percent': round((logged / total) * 100) if total else 0,
        },
        'customers': Customer.query.order_by(Customer.name.asc()).all(),
        'task_categories': TASK_CATEGORIES,
    }


def update_meeting_draft(meeting_id: int, data: dict[str, Any]) -> PrefetchedMeeting:
    """Validate and persist editable matching and activity draft fields."""
    meeting = db.session.get(PrefetchedMeeting, meeting_id)
    if meeting is None:
        raise ValueError('Meeting not found')
    if _linked_task(meeting):
        raise ValueError('Meeting already has an MSX activity')

    customer_id = int(data['customer_id']) if data.get('customer_id') else None
    if customer_id and db.session.get(Customer, customer_id) is None:
        raise ValueError('Customer not found')

    milestone_id = data.get('milestone_id')
    milestone = db.session.get(Milestone, int(milestone_id)) if milestone_id else None
    if milestone and milestone.customer_id != customer_id:
        raise ValueError('Milestone does not belong to selected customer')
    meeting.customer_id = customer_id
    meeting.milestone_id = milestone.id if milestone else None

    subject = (data.get('subject') or '').strip()
    if not subject:
        raise ValueError('Activity subject is required')
    meeting.draft_subject = subject
    meeting.draft_description = (data.get('description') or '').strip()

    category = int(data.get('task_category') or 0)
    if category not in _CATEGORY_VALUES:
        raise ValueError('Valid activity type is required')
    meeting.draft_task_category = category

    duration = int(data.get('duration_minutes') or 0)
    if duration < 1 or duration > 1440:
        raise ValueError('Duration must be between 1 and 1440 minutes')
    meeting.draft_duration_minutes = duration
    db.session.commit()
    return meeting


def link_existing_activity(meeting_id: int, task_id: int) -> MsxTask:
    """Confirm an imported MSX task as coverage for a meeting."""
    meeting = db.session.get(PrefetchedMeeting, meeting_id)
    task = db.session.get(MsxTask, task_id)
    if meeting is None or task is None:
        raise ValueError('Meeting or activity not found')
    if _linked_task(meeting):
        raise ValueError('Meeting already has an MSX activity')
    if task.meeting_id and task.meeting_id != meeting.id:
        raise ValueError('Activity is already linked to another meeting')
    if meeting.customer_id and task.milestone.customer_id != meeting.customer_id:
        raise ValueError('Activity belongs to a different customer')
    task.meeting_id = meeting.id
    meeting.milestone_id = task.milestone_id
    db.session.commit()
    return task


def create_meeting_activity(meeting_id: int) -> MsxTask:
    """Create one MSX activity from a saved meeting draft."""
    from app.services.msx_api import create_task

    with _create_lock:
        meeting = db.session.get(PrefetchedMeeting, meeting_id)
        if meeting is None:
            raise ValueError('Meeting not found')
        existing = _linked_task(meeting)
        if existing:
            return existing
        if not meeting.milestone_id or not meeting.selected_milestone:
            raise ValueError('Select a milestone before creating activity')
        if not meeting.selected_milestone.msx_milestone_id:
            raise ValueError('Selected milestone has no MSX ID')

        subject = meeting.draft_subject or meeting.subject
        description = (
            meeting.draft_description
            if meeting.draft_description is not None
            else _default_description(meeting)
        )
        category = meeting.draft_task_category or _default_category(meeting)
        duration = meeting.draft_duration_minutes or _default_duration(meeting)
        scheduled_start = meeting.start_time
        if scheduled_start.tzinfo is None:
            scheduled_start = scheduled_start.replace(tzinfo=timezone.utc)
        scheduled_end = scheduled_start + timedelta(minutes=duration)
        result = create_task(
            milestone_id=meeting.selected_milestone.msx_milestone_id,
            subject=subject,
            task_category=category,
            duration_minutes=duration,
            description=description or None,
            start_date=scheduled_start.isoformat(),
            due_date=scheduled_end.isoformat(),
        )
        if not result.get('success'):
            raise RuntimeError(result.get('error') or 'MSX activity creation failed')

        task = MsxTask(
            msx_task_id=result['task_id'],
            msx_task_url=result.get('task_url', ''),
            subject=subject,
            description=description or None,
            task_category=category,
            task_category_name=_CATEGORY_NAMES[category],
            duration_minutes=duration,
            is_hok=category in HOK_TASK_CATEGORIES,
            due_date=scheduled_end,
            note_id=meeting.note_id,
            meeting_id=meeting.id,
            milestone_id=meeting.milestone_id,
        )
        db.session.add(task)
        db.session.commit()
        return task


def _population_dates(
    populated_through: date | None,
    today: date,
) -> list[date]:
    """Return unpopulated fiscal-year weekdays through today."""
    fiscal_start, fiscal_end = fiscal_year_bounds(today)
    start = fiscal_start
    if populated_through and populated_through >= fiscal_start:
        start = populated_through + timedelta(days=1)
    end = min(today, fiscal_end)
    dates = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            dates.append(current)
        current += timedelta(days=1)
    return dates


def _population_row(today: date, create: bool = False) -> ActivityCoveragePopulation | None:
    """Return current-FY checkpoint, resetting stale prior-FY state when requested."""
    _, fiscal_end = fiscal_year_bounds(today)
    row = db.session.get(ActivityCoveragePopulation, 1)
    if row and row.fiscal_year_end != fiscal_end.year:
        if not create:
            return None
        row.fiscal_year_end = fiscal_end.year
        row.populated_through = None
        row.last_started_at = None
        row.last_completed_at = None
        row.last_error = None
    elif row is None and create:
        row = ActivityCoveragePopulation(id=1, fiscal_year_end=fiscal_end.year)
        db.session.add(row)
    return row


def get_population_status(today: date | None = None) -> dict[str, Any]:
    """Return durable checkpoint plus live fiscal population progress."""
    today = today or date.today()
    row = _population_row(today)
    populated_through = row.populated_through if row else None
    pending = _population_dates(populated_through, today)
    _, fiscal_end = fiscal_year_bounds(today)
    with _import_state_lock:
        live = dict(_import_state)

    if live['running']:
        label = 'Populating'
        detail = f"{live['completed_count']} of {live['total_dates']} days"
    elif populated_through is None:
        label = f'Import FY{fiscal_end.year % 100:02d} Calendar'
        detail = f'{len(pending)} weekdays through today'
    elif pending:
        label = 'Retry Calendar Import' if row and row.last_error else 'Catch Up Calendar'
        detail = (
            row.last_error
            if row and row.last_error
            else f'Since {populated_through.strftime("%b %d")} · {len(pending)} weekdays'
        )
    else:
        label = 'Up to date'
        detail = f'Through {populated_through.strftime("%b %d")}'

    return {
        **live,
        'label': label,
        'detail': detail,
        'can_start': bool(pending) and not live['running'],
        'populated_through': populated_through.isoformat() if populated_through else None,
        'last_completed_at': row.last_completed_at.isoformat() if row and row.last_completed_at else None,
        'pending_count': len(pending),
        'error': live['error'] or (row.last_error if row else None),
    }


def _set_import_state(**values: Any) -> None:
    with _import_state_lock:
        _import_state.update(values)


def _wait_before_retry(seconds: int) -> None:
    """Wait between source retries without busy-spinning."""
    threading.Event().wait(seconds)


def _sync_date_with_retries(target_str: str) -> str | None:
    """Sync one calendar date, returning the final error after retries."""
    from app.services.meeting_sync import sync_meetings_for_date

    last_error = None
    for attempt in range(1, _DATE_SYNC_ATTEMPTS + 1):
        _set_import_state(attempt=attempt, retrying=attempt > 1)
        try:
            _, last_error = sync_meetings_for_date(target_str)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                'Activity coverage sync attempt %d failed for %s',
                attempt, target_str,
            )
            last_error = str(exc) or exc.__class__.__name__
        if not last_error:
            _set_import_state(attempt=0, retrying=False)
            return None
        logger.warning(
            'Activity coverage sync attempt %d/%d failed for %s: %s',
            attempt, _DATE_SYNC_ATTEMPTS, target_str, last_error,
        )
        if attempt < _DATE_SYNC_ATTEMPTS:
            _wait_before_retry(_DATE_RETRY_DELAYS[attempt - 1])
    _set_import_state(attempt=0, retrying=False)
    return last_error


def populate_fiscal_year(today: date | None = None) -> dict[str, Any]:
    """Populate fiscal weekdays, pausing safely after retry exhaustion."""
    today = today or date.today()
    row = _population_row(today, create=True)
    dates = _population_dates(row.populated_through, today)
    row.last_started_at = datetime.now(timezone.utc)
    row.last_error = None
    db.session.commit()
    _set_import_state(
        total_dates=len(dates), completed_count=0,
        attempt=0, retrying=False, error=None,
    )

    for index, target in enumerate(dates, start=1):
        target_str = target.isoformat()
        _set_import_state(current_date=target_str)
        error = _sync_date_with_retries(target_str)
        if error:
            row.last_error = (
                f'Paused at {target_str} after {_DATE_SYNC_ATTEMPTS} attempts: {error}'
            )
            db.session.commit()
            _set_import_state(error=row.last_error)
            return {'completed_count': index - 1, 'error': row.last_error}
        row.populated_through = target
        row.last_error = None
        db.session.commit()
        _set_import_state(completed_count=index)

    row.last_completed_at = datetime.now(timezone.utc)
    db.session.commit()
    return {'completed_count': len(dates), 'error': None}


def _population_worker(app: Flask) -> None:
    with _import_lock:
        try:
            with app.app_context():
                populate_fiscal_year()
        except Exception as exc:  # noqa: BLE001
            logger.exception('Activity coverage population failed')
            _set_import_state(error=str(exc))
        finally:
            _set_import_state(running=False, current_date=None)


def start_population(app: Flask) -> bool:
    """Start one resumable background fiscal-year population."""
    status = get_population_status()
    if _import_lock.locked() or status['running'] or not status['can_start']:
        return False
    _set_import_state(
        running=True,
        current_date=None,
        completed_count=0,
        total_dates=0,
        attempt=0,
        retrying=False,
        error=None,
    )
    thread = threading.Thread(
        target=_population_worker,
        args=(app,),
        daemon=True,
        name='activity-coverage-import',
    )
    thread.start()
    return True