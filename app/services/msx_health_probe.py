"""
MSX Account Teams health probe.

Runs a tiny background probe against the ``msp_accountteams`` entity and
emits a ``SalesBuddy.MsxAccountTeamProbe`` custom event so the metrics
dashboard can chart real coverage of the recurring 0x80040224 outage.

Cadence: short delay after boot, then once per hour, with a per-instance
random minute-of-hour offset picked at startup. The probe call uses
``top=1`` to keep network/CPU cost negligible.

Result values (see ``telemetry_shipper.queue_msx_outage``):
    - ``ok``                - probe got HTTP 200
    - ``outage``            - response body contained the 0x80040224 /
                              "header name and value" signature
    - ``skipped_no_token``  - no MSX token cached, or HTTP 401
    - ``skipped_no_vpn``    - app already detected VPN blocking, or
                              HTTP 403 with the IP-blocked code/message
    - ``error``             - any other failure (timeout, connection
                              error, other 4xx/5xx). Up until 2026-05
                              timeouts were silently filed as
                              ``skipped_no_vpn``, which masked real MSX
                              hangs; they're proper failures now.

Implementation note: the probe makes one direct HTTP call with a short
timeout and **no retries**. Going through ``msx_api.query_entity`` would
inherit a 10-attempt retry chain that can stretch a single probe to
several minutes when MSX hangs.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Default cadence: probe every hour. Boot-time delay before the first
# probe so we don't slow down startup or compete with the initial token
# refresh.
DEFAULT_INTERVAL_SECONDS = 3600
DEFAULT_STARTUP_DELAY_SECONDS = 60

_probe_thread: Optional[threading.Thread] = None
_probe_running = False
_probe_offset_seconds: int = 0  # random minute-of-hour offset, picked at startup

_last_probe: dict = {
    'time': None,        # datetime of last attempt
    'result': None,      # last result string
    'error_code': None,  # last error code (if any)
}
_last_probe_lock = threading.Lock()


# Per-attempt timeout for the probe HTTP call. The probe is an up-check,
# not a data fetch -- we'd rather get a fast 'error' than block for
# minutes hoping MSX wakes up.
PROBE_TIMEOUT_SECONDS = 10
LOOKUP_TIMEOUT_SECONDS = 5

# Outage signature from the recurring D365 bug. Body matched
# case-insensitively.
_OUTAGE_MARKERS = ('0x80040224', 'header name and value')


def _classify_response(response) -> tuple[str, Optional[str]]:
    """Map a raw ``requests.Response`` to a probe result + optional code.

    The probe makes one HTTP call with no retries, so we classify directly
    off the response rather than parsing error strings.
    """
    from app.services.msx_api import IP_BLOCKED_CODE, IP_BLOCKED_MESSAGE

    status = response.status_code
    if status == 200:
        return ('ok', None)
    if status == 401:
        return ('skipped_no_token', None)

    # Pull body once for outage / VPN sniffing. Cap to avoid eating logs
    # if MSX returns a huge HTML error page.
    try:
        body = response.text[:2000]
    except Exception:  # noqa: BLE001
        body = ''
    body_lower = body.lower()

    if status == 403 and (IP_BLOCKED_CODE in body or IP_BLOCKED_MESSAGE in body):
        return ('skipped_no_vpn', None)

    for marker in _OUTAGE_MARKERS:
        if marker in body_lower:
            return ('outage', '0x80040224')

    return ('error', f'HTTP_{status}')


def _classify_exception(exc: BaseException) -> tuple[str, Optional[str]]:
    """Map a network-level exception to a probe result + code.

    Timeouts and connection errors used to be silently filed as
    ``skipped_no_vpn``, which masked real MSX hangs. They're real failures
    now and surface as ``error`` with a typed error code.
    """
    return ('error', type(exc).__name__[:64])


def _classify_query_result(result: dict) -> tuple[str, Optional[str]]:
    """Map a ``msx_api.query_entity`` result dict to a probe result.

    Used by ``record_msp_accountteams_call`` so every real call to the
    msp_accountteams endpoint contributes telemetry, not just background
    probes. Mirrors ``_classify_response`` but operates on the friendly
    dict that ``query_entity`` returns instead of a raw HTTP response.
    """
    if result.get('success'):
        return ('ok', None)

    error_msg = (result.get('error') or '').lower()
    if '0x80040224' in error_msg or 'header name and value' in error_msg:
        return ('outage', '0x80040224')
    if result.get('vpn_blocked') or 'ip address is blocked' in error_msg:
        return ('skipped_no_vpn', None)
    if 'not authenticated' in error_msg or "az login" in error_msg:
        return ('skipped_no_token', None)
    return ('error', None)


def record_msp_accountteams_call(result: dict) -> None:
    """Feed telemetry from a real ``msp_accountteams`` query.

    The metrics dashboard and outage alert key off the
    ``SalesBuddy.MsxAccountTeamProbe`` event. Real account syncs and
    territory loads hit the same endpoint hundreds of times per day, so
    using their results as free probes makes the dashboard much denser
    and lets the alert resolve faster than waiting for the next hourly
    background probe. The shipper already dedupes by (result, hour) so
    a 1000-account sync emits at most one event per hour per outcome.
    """
    try:
        from app.services.telemetry_shipper import queue_msx_outage
        outcome, error_code = _classify_query_result(result)
        queue_msx_outage(outcome, error_code=error_code)
    except Exception:  # noqa: BLE001
        # Telemetry must never break the caller.
        logger.exception('record_msp_accountteams_call failed')


def _pick_probe_account_id(token: str) -> tuple[Optional[str], Optional[tuple[str, Optional[str]]]]:
    """Resolve a stable MSX account GUID to filter the probe by.

    Bare ``$top=1`` queries against ``msp_accountteams`` hang in the
    current outage mode -- only filtered queries (matching the shape used
    by the account sync) succeed reliably. So we pick any local customer
    TPID and resolve it to an MSX account GUID via the ``accounts``
    endpoint, which is healthy.

    Returns ``(account_id, None)`` on success, or
    ``(None, (result, error_code))`` when we couldn't resolve a target
    and the caller should bail with that result.
    """
    import requests
    from app.models import Customer
    from app.services.msx_auth import CRM_BASE_URL
    from app.services.msx_api import _get_headers

    customer = Customer.query.filter(Customer.tpid.isnot(None)).first()
    if not customer or not customer.tpid:
        return None, ('error', 'no_customer')

    url = (
        f'{CRM_BASE_URL}/accounts'
        f"?$filter=msp_mstopparentid eq '{customer.tpid}'"
        '&$select=accountid&$top=1'
    )
    try:
        response = requests.get(
            url,
            headers=_get_headers(token),
            timeout=LOOKUP_TIMEOUT_SECONDS,
        )
    except (requests.exceptions.Timeout,
            requests.exceptions.ConnectionError) as e:
        return None, _classify_exception(e)
    except Exception as e:  # noqa: BLE001
        logger.exception('MSX probe account lookup raised unexpectedly')
        return None, _classify_exception(e)

    # 401/403/outage marker etc. on the lookup itself counts as a real
    # signal, not a probe target failure.
    if response.status_code != 200:
        return None, _classify_response(response)

    try:
        records = response.json().get('value', [])
    except Exception:  # noqa: BLE001
        return None, ('error', 'lookup_bad_json')

    if not records:
        # TPID has no top-parent account in MSX. Try once more with a
        # different customer? Keep it simple for now -- if this happens
        # repeatedly the dashboard will show it.
        return None, ('error', 'lookup_no_match')

    return records[0].get('accountid'), None


def run_probe() -> str:
    """Run a single probe and emit the resulting telemetry event.

    Returns the result string that was queued (or would have been queued
    if telemetry is disabled).
    """
    # Local imports keep module load cheap and let tests patch them.
    import requests
    from app.services.msx_auth import (
        get_msx_token, CRM_BASE_URL, is_vpn_blocked,
    )
    from app.services.msx_api import _get_headers
    from app.services.telemetry_shipper import queue_msx_outage

    # Cheap pre-checks before touching the network.
    token = get_msx_token()
    if not token:
        result, error_code = 'skipped_no_token', None
    elif is_vpn_blocked():
        # Another part of the app already proved VPN is blocked. Don't
        # waste a request just to confirm.
        result, error_code = 'skipped_no_vpn', None
    else:
        account_id, bail = _pick_probe_account_id(token)
        if bail is not None:
            result, error_code = bail
        else:
            url = (
                f'{CRM_BASE_URL}/msp_accountteams'
                f'?$filter=_msp_accountid_value eq {account_id}'
                '&$select=msp_accountteamid&$top=1'
            )
            try:
                response = requests.get(
                    url,
                    headers=_get_headers(token),
                    timeout=PROBE_TIMEOUT_SECONDS,
                )
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError) as e:
                result, error_code = _classify_exception(e)
            except Exception as e:  # noqa: BLE001
                logger.exception('MSX probe raised unexpectedly')
                result, error_code = _classify_exception(e)
            else:
                result, error_code = _classify_response(response)

    queue_msx_outage(result, error_code=error_code)

    with _last_probe_lock:
        _last_probe['time'] = datetime.now(timezone.utc)
        _last_probe['result'] = result
        _last_probe['error_code'] = error_code

    logger.info(f'MSX Account Teams probe: {result}')
    return result


def get_last_probe() -> dict:
    """Return a snapshot of the last probe outcome (for diagnostics)."""
    with _last_probe_lock:
        return dict(_last_probe)


def start_probe_thread(
    app=None,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    startup_delay_seconds: int = DEFAULT_STARTUP_DELAY_SECONDS,
) -> None:
    """Start the background probe loop. Idempotent.

    Args:
        app: Flask app used to push an application context around each probe.
            The probe resolves a target via ``Customer.query``, which requires
            an active app context in the background thread.
        interval_seconds: Time between probes (default 1 hour).
        startup_delay_seconds: Wait this long after start before the first
            probe.
    """
    global _probe_thread, _probe_running, _probe_offset_seconds

    if _probe_running:
        logger.info('MSX Account Teams probe already running')
        return

    # Per-instance random offset. Spreads probes from many installs across
    # the hour rather than hammering MSX on the top of every hour.
    _probe_offset_seconds = random.randint(0, max(0, interval_seconds - 1))

    def _run_probe_in_context() -> None:
        """Run one probe inside an app context so DB access works."""
        if app is not None:
            with app.app_context():
                run_probe()
        else:
            run_probe()

    def _loop() -> None:
        global _probe_running
        _probe_running = True
        logger.info(
            f'MSX Account Teams probe started '
            f'(interval={interval_seconds}s, startup_delay={startup_delay_seconds}s, '
            f'offset={_probe_offset_seconds}s)'
        )

        # Initial delay before the first probe.
        for _ in range(startup_delay_seconds):
            if not _probe_running:
                return
            time.sleep(1)

        # First probe.
        try:
            _run_probe_in_context()
        except Exception:
            logger.exception('MSX probe failed')

        # Then sleep (interval + per-instance offset) between probes.
        # The offset is added once on the first cycle so subsequent
        # probes land at a consistent minute-of-hour.
        first_cycle = True
        while _probe_running:
            sleep_for = interval_seconds + (_probe_offset_seconds if first_cycle else 0)
            first_cycle = False
            for _ in range(sleep_for):
                if not _probe_running:
                    return
                time.sleep(1)
            try:
                _run_probe_in_context()
            except Exception:
                logger.exception('MSX probe failed')

    _probe_thread = threading.Thread(target=_loop, daemon=True, name='msx-probe')
    _probe_thread.start()


def stop_probe_thread() -> None:
    """Stop the background probe loop (used by tests)."""
    global _probe_running
    _probe_running = False
