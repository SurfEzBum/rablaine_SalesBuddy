"""Read historical calendar events from desktop Outlook."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_CALENDAR_FOLDER = 9
_SMTP_ADDRESS_PROPERTY = (
    'http://schemas.microsoft.com/mapi/proptag/0x39FE001E'
)
_MAX_EVENTS_PER_DAY = 500


def _smtp_address(address_entry: Any) -> str | None:
    """Return an SMTP address from an Outlook address entry."""
    if address_entry is None:
        return None
    try:
        address = address_entry.PropertyAccessor.GetProperty(
            _SMTP_ADDRESS_PROPERTY,
        )
        if address:
            return str(address).strip().lower()
    except Exception:  # noqa: BLE001
        pass
    try:
        address = address_entry.Address
        if address and not str(address).upper().startswith('/O='):
            return str(address).strip().lower()
    except Exception:  # noqa: BLE001
        pass
    return None


def _organizer_email(item: Any) -> str | None:
    """Return organizer SMTP address when Outlook exposes it."""
    try:
        return _smtp_address(item.GetOrganizer())
    except Exception:  # noqa: BLE001
        return None


def _attendees(item: Any) -> list[dict[str, str | None]]:
    """Shape Outlook recipients for meeting prefetch ingestion."""
    attendees = []
    try:
        recipients = item.Recipients
        for index in range(1, recipients.Count + 1):
            recipient = recipients.Item(index)
            attendees.append({
                'name': str(recipient.Name).strip() or None,
                'email': _smtp_address(recipient.AddressEntry),
            })
    except Exception:  # noqa: BLE001
        logger.debug('Could not read Outlook attendees', exc_info=True)
    return attendees


def fetch_outlook_meetings_for_date(target_date: date) -> list[dict[str, Any]]:
    """Return Outlook calendar events overlapping one local date.

    Raises when Outlook or its MAPI profile is unavailable so callers can
    fall back to WorkIQ.
    """
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        outlook = win32com.client.Dispatch('Outlook.Application')
        namespace = outlook.GetNamespace('MAPI')
        items = namespace.GetDefaultFolder(_CALENDAR_FOLDER).Items
        items.IncludeRecurrences = True
        items.Sort('[Start]')

        start = datetime.combine(target_date, datetime.min.time())
        end = start + timedelta(days=1)
        restriction = (
            f"[Start] < '{end.strftime('%m/%d/%Y %I:%M %p')}' AND "
            f"[End] > '{start.strftime('%m/%d/%Y %I:%M %p')}'"
        )
        matches = items.Restrict(restriction)
        meetings = []
        item = matches.GetFirst()
        while item is not None and len(meetings) < _MAX_EVENTS_PER_DAY:
            subject = str(getattr(item, 'Subject', '') or '').strip()
            start_time = getattr(item, 'Start', None)
            end_time = getattr(item, 'End', None)
            if subject and start_time:
                meetings.append({
                    'subject': subject,
                    'start_time': start_time.isoformat(),
                    'end_time': end_time.isoformat() if end_time else None,
                    'organizer_email': _organizer_email(item),
                    'is_recurring': bool(getattr(item, 'IsRecurring', False)),
                    'attendees': _attendees(item),
                })
            item = matches.GetNext()
        return meetings
    finally:
        pythoncom.CoUninitialize()