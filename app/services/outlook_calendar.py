"""Read historical calendar events from desktop Outlook."""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_CALENDAR_FOLDER = 9
_SMTP_ADDRESS_PROPERTY = (
    'http://schemas.microsoft.com/mapi/proptag/0x39FE001E'
)
_MAX_EVENTS_PER_DAY = 500
_OUTLOOK_RETRY_SECONDS = 4 * 60 * 60
_availability_lock = threading.Lock()
_unavailable_until = 0.0
_unavailable_reason: str | None = None


class OutlookCalendarUnavailable(RuntimeError):
    """Raised when a safe corporate Outlook calendar cannot be opened."""


def _configured_profile_name() -> str | None:
    """Return configured classic Outlook default profile without launching it."""
    import winreg

    for version in ('16.0', '15.0'):
        root_path = rf'Software\Microsoft\Office\{version}\Outlook'
        profiles_path = rf'{root_path}\Profiles'
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, root_path) as root:
                profile_name = str(
                    winreg.QueryValueEx(root, 'DefaultProfile')[0],
                ).strip()
            if not profile_name:
                continue
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                rf'{profiles_path}\{profile_name}',
            ):
                return profile_name
        except OSError:
            continue
    return None


def _corporate_calendar_items(namespace: Any) -> Any:
    """Return calendar items from an Outlook account owned by Microsoft."""
    accounts = namespace.Accounts
    for index in range(1, accounts.Count + 1):
        try:
            account = accounts.Item(index)
            smtp_address = str(getattr(account, 'SmtpAddress', '') or '').lower()
            if not smtp_address.endswith('@microsoft.com'):
                continue
            store = getattr(account, 'DeliveryStore', None)
            if store is not None:
                return store.GetDefaultFolder(_CALENDAR_FOLDER).Items
        except Exception:  # noqa: BLE001
            logger.debug('Could not inspect Outlook account %d', index, exc_info=True)
    raise OutlookCalendarUnavailable(
        'Classic Outlook has no configured Microsoft corporate account',
    )


def _outlook_datetime_iso(value: Any) -> str | None:
    """Serialize Outlook's local wall-clock datetime with the local UTC offset."""
    if value is None:
        return None
    local_value = value.replace(tzinfo=None).astimezone()
    return local_value.isoformat()


def _raise_if_circuit_open() -> None:
    """Avoid repeatedly launching or querying unusable Outlook during imports."""
    with _availability_lock:
        if time.monotonic() < _unavailable_until:
            raise OutlookCalendarUnavailable(
                _unavailable_reason or 'Outlook calendar is temporarily unavailable',
            )


def _mark_unavailable(reason: str) -> None:
    """Pause Outlook attempts so remaining dates fall back directly to WorkIQ."""
    global _unavailable_reason, _unavailable_until
    with _availability_lock:
        _unavailable_reason = reason
        _unavailable_until = time.monotonic() + _OUTLOOK_RETRY_SECONDS


def reset_outlook_availability() -> None:
    """Clear cached Outlook failure state, primarily for focused tests."""
    global _unavailable_reason, _unavailable_until
    with _availability_lock:
        _unavailable_reason = None
        _unavailable_until = 0.0


def corporate_outlook_available() -> bool:
    """Return whether a configured Microsoft Outlook calendar can be opened."""
    try:
        _raise_if_circuit_open()
        if not _configured_profile_name():
            raise OutlookCalendarUnavailable(
                'Classic Outlook has no configured default profile',
            )

        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            outlook = win32com.client.Dispatch('Outlook.Application')
            namespace = outlook.GetNamespace('MAPI')
            _corporate_calendar_items(namespace)
            return True
        finally:
            pythoncom.CoUninitialize()
    except Exception as exc:  # noqa: BLE001
        reason = str(exc) or exc.__class__.__name__
        _mark_unavailable(reason)
        logger.info('Corporate Outlook calendar unavailable: %s', reason)
        return False


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
    _raise_if_circuit_open()
    if not _configured_profile_name():
        reason = 'Classic Outlook has no configured default profile'
        _mark_unavailable(reason)
        raise OutlookCalendarUnavailable(reason)

    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        outlook = win32com.client.Dispatch('Outlook.Application')
        namespace = outlook.GetNamespace('MAPI')
        items = _corporate_calendar_items(namespace)
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
                    'start_time': _outlook_datetime_iso(start_time),
                    'end_time': _outlook_datetime_iso(end_time),
                    'organizer_email': _organizer_email(item),
                    'is_recurring': bool(getattr(item, 'IsRecurring', False)),
                    'attendees': _attendees(item),
                })
            item = matches.GetNext()
        return meetings
    except Exception as exc:
        _mark_unavailable(str(exc) or exc.__class__.__name__)
        raise
    finally:
        pythoncom.CoUninitialize()
