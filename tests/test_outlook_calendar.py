"""Tests for guarded classic Outlook calendar access."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.services import outlook_calendar


@pytest.fixture(autouse=True)
def clear_outlook_circuit():
    """Keep cached Outlook failures isolated between tests."""
    outlook_calendar.reset_outlook_availability()
    yield
    outlook_calendar.reset_outlook_availability()


def test_missing_profile_fails_before_outlook_launch():
    """Unconfigured classic Outlook must fall back without opening setup UI."""
    with patch.object(
        outlook_calendar,
        '_configured_profile_name',
        return_value=None,
    ), patch('win32com.client.Dispatch') as dispatch:
        with pytest.raises(
            outlook_calendar.OutlookCalendarUnavailable,
            match='no configured default profile',
        ):
            outlook_calendar.fetch_outlook_meetings_for_date(
                datetime(2026, 8, 17).date(),
            )

    dispatch.assert_not_called()


def test_unavailable_outlook_is_not_retried_during_import():
    """One eligibility failure must keep remaining import dates on WorkIQ."""
    with patch.object(
        outlook_calendar,
        '_configured_profile_name',
        return_value=None,
    ):
        with pytest.raises(outlook_calendar.OutlookCalendarUnavailable):
            outlook_calendar.fetch_outlook_meetings_for_date(
                datetime(2026, 8, 17).date(),
            )

    with patch.object(
        outlook_calendar,
        '_configured_profile_name',
        return_value='Outlook',
    ) as profile_check, patch('win32com.client.Dispatch') as dispatch:
        with pytest.raises(outlook_calendar.OutlookCalendarUnavailable):
            outlook_calendar.fetch_outlook_meetings_for_date(
                datetime(2026, 8, 18).date(),
            )

    profile_check.assert_not_called()
    dispatch.assert_not_called()


def test_corporate_account_delivery_store_is_selected():
    """Personal default calendars cannot be used for Sales Buddy imports."""
    personal_store = Mock()
    corporate_items = Mock()
    corporate_folder = SimpleNamespace(Items=corporate_items)
    corporate_store = Mock()
    corporate_store.GetDefaultFolder.return_value = corporate_folder
    accounts = Mock()
    accounts.Count = 2
    accounts.Item.side_effect = [
        SimpleNamespace(SmtpAddress='seller@example.com', DeliveryStore=personal_store),
        SimpleNamespace(
            SmtpAddress='seller@microsoft.com',
            DeliveryStore=corporate_store,
        ),
    ]

    result = outlook_calendar._corporate_calendar_items(
        SimpleNamespace(Accounts=accounts),
    )

    assert result is corporate_items
    personal_store.GetDefaultFolder.assert_not_called()
    corporate_store.GetDefaultFolder.assert_called_once_with(9)


def test_non_corporate_accounts_are_rejected():
    """A configured personal Outlook profile must route to WorkIQ."""
    accounts = Mock()
    accounts.Count = 1
    accounts.Item.return_value = SimpleNamespace(
        SmtpAddress='seller@example.com',
        DeliveryStore=Mock(),
    )

    with pytest.raises(
        outlook_calendar.OutlookCalendarUnavailable,
        match='no configured Microsoft corporate account',
    ):
        outlook_calendar._corporate_calendar_items(
            SimpleNamespace(Accounts=accounts),
        )


def test_outlook_timestamp_preserves_wall_clock_with_local_offset():
    """Outlook's UTC-labeled COM value must retain its local wall clock."""
    value = datetime(2026, 8, 17, 9, 30, tzinfo=timezone.utc)

    result = datetime.fromisoformat(outlook_calendar._outlook_datetime_iso(value))

    assert result.replace(tzinfo=None) == datetime(2026, 8, 17, 9, 30)
    assert result.utcoffset() is not None