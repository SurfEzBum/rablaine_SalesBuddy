"""
Parallel Import Test Routes.

Temporary experimental endpoint for testing parallel MSX API calls.
Uses ThreadPoolExecutor with 2 workers to run batch queries concurrently,
reporting progress via SSE (Server-Sent Events).

This is a non-destructive test -- it does NOT write to the database.
It only queries MSX and reports timing/results for comparison.

Related: GitHub issue #20 (Perf: Parallelize data imports)
"""

import json
import math
import queue
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Blueprint, Response, g, stream_with_context, render_template
import logging

from app.services.msx_auth import get_msx_token, is_vpn_blocked
from app.services.msx_api import (
    scan_init,
    query_entity,
    batch_query_account_teams,
)

logger = logging.getLogger(__name__)

parallel_import_bp = Blueprint(
    'parallel_import', __name__, url_prefix='/api/parallel-import'
)

NUM_WORKERS = 2
ACCOUNT_BATCH_SIZE = 15
TEAM_BATCH_SIZE = 3


def _sse(data: dict) -> str:
    """Format a dict as an SSE data line."""
    return "data: " + json.dumps(data) + "\n\n"


def _query_accounts_chunk(
    account_ids: list,
    batch_size: int,
    progress_queue: queue.Queue,
    worker_id: int,
) -> dict:
    """
    Worker function: query account details for a chunk of IDs.

    Puts progress dicts into *progress_queue* so the main generator can
    yield SSE events while workers are running.

    Returns:
        Dict mapping account_id -> account record.
    """
    accounts = {}
    batches = math.ceil(len(account_ids) / batch_size)
    for batch_num, i in enumerate(range(0, len(account_ids), batch_size), start=1):
        batch = account_ids[i:i + batch_size]
        filter_parts = [f"accountid eq {aid}" for aid in batch]
        filter_query = " or ".join(filter_parts)
        result = query_entity(
            "accounts",
            select=[
                "accountid", "name", "msp_mstopparentid",
                "_territoryid_value", "msp_verticalcode",
                "msp_verticalcategorycode",
            ],
            filter_query=filter_query,
            top=batch_size + 5,
        )
        if result.get("success"):
            for rec in result.get("records", []):
                acct_id = rec.get("accountid")
                if acct_id:
                    accounts[acct_id] = rec
        progress_queue.put({
            "worker": worker_id,
            "phase": "accounts",
            "batch": batch_num,
            "total_batches": batches,
            "fetched": len(accounts),
        })
    return accounts


def _query_territories_chunk(
    territory_ids: list,
    batch_size: int,
    progress_queue: queue.Queue,
    worker_id: int,
) -> dict:
    """
    Worker function: query territory details for a chunk of IDs.
    """
    territories = {}
    batches = math.ceil(len(territory_ids) / batch_size) if territory_ids else 0
    for batch_num, i in enumerate(range(0, len(territory_ids), batch_size), start=1):
        batch = territory_ids[i:i + batch_size]
        filter_parts = [f"territoryid eq {tid}" for tid in batch]
        filter_query = " or ".join(filter_parts)
        result = query_entity(
            "territories",
            select=[
                "territoryid", "name", "msp_ownerid",
                "msp_salesunitname", "msp_accountteamunitname",
            ],
            filter_query=filter_query,
            top=batch_size + 5,
        )
        if result.get("success"):
            for rec in result.get("records", []):
                tid = rec.get("territoryid")
                if tid:
                    territories[tid] = rec
        progress_queue.put({
            "worker": worker_id,
            "phase": "territories",
            "batch": batch_num,
            "total_batches": batches,
            "fetched": len(territories),
        })
    return territories


def _query_teams_chunk(
    account_ids: list,
    batch_size: int,
    progress_queue: queue.Queue,
    worker_id: int,
) -> dict:
    """
    Worker function: query account teams for a chunk of account IDs.
    """
    account_sellers = {}
    unique_sellers = {}
    account_ses = {}
    batches = math.ceil(len(account_ids) / batch_size) if account_ids else 0
    for batch_num, i in enumerate(range(0, len(account_ids), batch_size), start=1):
        batch = account_ids[i:i + batch_size]
        teams_result = batch_query_account_teams(batch, batch_size=len(batch))
        if teams_result.get("success"):
            account_sellers.update(teams_result.get("account_sellers", {}))
            unique_sellers.update(teams_result.get("unique_sellers", {}))
            account_ses.update(teams_result.get("account_ses", {}))
        progress_queue.put({
            "worker": worker_id,
            "phase": "teams",
            "batch": batch_num,
            "total_batches": batches,
            "sellers_found": len(unique_sellers),
        })
    return {
        "account_sellers": account_sellers,
        "unique_sellers": unique_sellers,
        "account_ses": account_ses,
    }


def _drain_queue(q: queue.Queue) -> list:
    """Pull all available events from the queue without blocking."""
    events = []
    while True:
        try:
            events.append(q.get_nowait())
        except queue.Empty:
            break
    return events


@parallel_import_bp.route('/test')
def test_page():
    """Render the parallel import test page."""
    return render_template('parallel_import_test.html')


@parallel_import_bp.route('/stream')
def parallel_import_stream():
    """
    SSE endpoint that runs the MSX import queries using 2 parallel workers.

    Read-only -- does NOT write to the database. Reports timings for each
    phase so we can compare parallel vs sequential performance.
    """
    user_id = g.user.id if g.user else None
    if not user_id:
        return Response(
            _sse({"error": "No user found"}),
            mimetype='text/event-stream',
        )

    def generate():
        total_start = time.time()
        phase = "initializing"

        try:
            # --- Pre-flight: token check ---
            yield _sse({"message": "Checking MSX authentication...", "progress": 0})
            token = get_msx_token()
            if not token:
                yield _sse({"error": "Not authenticated to MSX. Please sign in first."})
                return

            if is_vpn_blocked():
                yield _sse({"error": "VPN blocked -- connect to corpnet and retry.",
                            "vpn_blocked": True})
                return

            # --- Phase 1: scan_init (single-threaded, fast) ---
            phase = "scan_init"
            yield _sse({"message": "Running scan_init to get account list...", "progress": 1})
            t0 = time.time()
            init_result = scan_init()
            scan_time = round(time.time() - t0, 2)

            if not init_result.get("success"):
                yield _sse({"error": f"scan_init failed: {init_result.get('error')}"})
                return

            account_ids = init_result.get("account_ids", [])
            user_info = init_result.get("user", {})
            detected_role = init_result.get("role", "Unknown")
            yield _sse({
                "message": (
                    f"scan_init done in {scan_time}s -- "
                    f"{user_info.get('name')} ({detected_role}), "
                    f"{len(account_ids)} accounts"
                ),
                "progress": 2,
                "timing": {"phase": "scan_init", "seconds": scan_time},
            })

            if not account_ids:
                yield _sse({"error": "No accounts found for this user."})
                return

            # -------------------------------------------------------
            # Phase 2: Parallel account queries (2 workers)
            # -------------------------------------------------------
            phase = "querying accounts (parallel)"
            mid = len(account_ids) // 2
            chunks = [account_ids[:mid], account_ids[mid:]]
            yield _sse({
                "message": (
                    f"Querying {len(account_ids)} accounts in parallel "
                    f"({NUM_WORKERS} workers, batches of {ACCOUNT_BATCH_SIZE})..."
                ),
                "progress": 3,
            })

            progress_q: queue.Queue = queue.Queue()
            accounts_raw: dict = {}
            t0 = time.time()

            with ThreadPoolExecutor(max_workers=NUM_WORKERS) as pool:
                futures = [
                    pool.submit(
                        _query_accounts_chunk, chunk,
                        ACCOUNT_BATCH_SIZE, progress_q, idx + 1,
                    )
                    for idx, chunk in enumerate(chunks) if chunk
                ]

                # Poll for progress while workers run
                while not all(f.done() for f in futures):
                    time.sleep(0.3)
                    for evt in _drain_queue(progress_q):
                        pct = 3 + int(
                            (evt["batch"] / max(evt["total_batches"], 1)) * 9
                        )
                        yield _sse({
                            "message": (
                                f"[W{evt['worker']}] Accounts batch "
                                f"{evt['batch']}/{evt['total_batches']} "
                                f"({evt['fetched']} fetched)"
                            ),
                            "progress": min(pct, 12),
                            "worker": evt["worker"],
                        })

                # Drain remaining events
                for evt in _drain_queue(progress_q):
                    yield _sse({
                        "message": (
                            f"[W{evt['worker']}] Accounts batch "
                            f"{evt['batch']}/{evt['total_batches']} "
                            f"({evt['fetched']} fetched)"
                        ),
                        "progress": 12,
                        "worker": evt["worker"],
                    })

                # Merge results
                for f in futures:
                    result = f.result()
                    accounts_raw.update(result)

            accounts_time = round(time.time() - t0, 2)
            yield _sse({
                "message": (
                    f"Account queries done in {accounts_time}s -- "
                    f"{len(accounts_raw)} accounts fetched"
                ),
                "progress": 13,
                "timing": {"phase": "accounts_parallel", "seconds": accounts_time},
            })

            if not accounts_raw:
                yield _sse({"error": "Failed to query any accounts."})
                return

            # -------------------------------------------------------
            # Phase 3: Parallel territory queries (2 workers)
            # -------------------------------------------------------
            phase = "querying territories (parallel)"
            territory_ids = list({
                acct.get("_territoryid_value")
                for acct in accounts_raw.values()
                if acct.get("_territoryid_value")
            })
            yield _sse({
                "message": (
                    f"Querying {len(territory_ids)} territories in parallel..."
                ),
                "progress": 14,
            })

            territories_raw: dict = {}
            t0 = time.time()

            if territory_ids:
                mid_t = len(territory_ids) // 2
                t_chunks = [territory_ids[:mid_t], territory_ids[mid_t:]]

                with ThreadPoolExecutor(max_workers=NUM_WORKERS) as pool:
                    futures = [
                        pool.submit(
                            _query_territories_chunk, chunk,
                            ACCOUNT_BATCH_SIZE, progress_q, idx + 1,
                        )
                        for idx, chunk in enumerate(t_chunks) if chunk
                    ]

                    while not all(f.done() for f in futures):
                        time.sleep(0.3)
                        for evt in _drain_queue(progress_q):
                            pct = 14 + int(
                                (evt["batch"] / max(evt["total_batches"], 1)) * 6
                            )
                            yield _sse({
                                "message": (
                                    f"[W{evt['worker']}] Territories batch "
                                    f"{evt['batch']}/{evt['total_batches']} "
                                    f"({evt['fetched']} fetched)"
                                ),
                                "progress": min(pct, 20),
                                "worker": evt["worker"],
                            })

                    for evt in _drain_queue(progress_q):
                        yield _sse({
                            "message": (
                                f"[W{evt['worker']}] Territories batch "
                                f"{evt['batch']}/{evt['total_batches']} "
                                f"({evt['fetched']} fetched)"
                            ),
                            "progress": 20,
                            "worker": evt["worker"],
                        })

                    for f in futures:
                        territories_raw.update(f.result())

            territories_time = round(time.time() - t0, 2)
            yield _sse({
                "message": (
                    f"Territory queries done in {territories_time}s -- "
                    f"{len(territories_raw)} territories fetched"
                ),
                "progress": 21,
                "timing": {"phase": "territories_parallel", "seconds": territories_time},
            })

            # -------------------------------------------------------
            # Phase 4: Parallel account team queries (2 workers)
            # This is the big one -- 25-85% of total import time.
            # -------------------------------------------------------
            phase = "querying account teams (parallel)"
            all_account_ids = list(accounts_raw.keys())
            mid_a = len(all_account_ids) // 2
            a_chunks = [all_account_ids[:mid_a], all_account_ids[mid_a:]]
            total_team_batches = math.ceil(len(all_account_ids) / TEAM_BATCH_SIZE)

            yield _sse({
                "message": (
                    f"Querying teams for {len(all_account_ids)} accounts in parallel "
                    f"({NUM_WORKERS} workers, batches of {TEAM_BATCH_SIZE}, "
                    f"~{total_team_batches} total batches)..."
                ),
                "progress": 25,
            })

            account_sellers: dict = {}
            unique_sellers: dict = {}
            account_ses: dict = {}
            t0 = time.time()

            with ThreadPoolExecutor(max_workers=NUM_WORKERS) as pool:
                futures = [
                    pool.submit(
                        _query_teams_chunk, chunk,
                        TEAM_BATCH_SIZE, progress_q, idx + 1,
                    )
                    for idx, chunk in enumerate(a_chunks) if chunk
                ]

                while not all(f.done() for f in futures):
                    time.sleep(0.3)
                    for evt in _drain_queue(progress_q):
                        # Total progress across both workers
                        pct = 25 + int(
                            (evt["batch"] / max(evt["total_batches"], 1)) * 30
                        )
                        yield _sse({
                            "message": (
                                f"[W{evt['worker']}] Teams batch "
                                f"{evt['batch']}/{evt['total_batches']} "
                                f"({evt['sellers_found']} sellers)"
                            ),
                            "progress": min(pct, 85),
                            "worker": evt["worker"],
                        })

                for evt in _drain_queue(progress_q):
                    yield _sse({
                        "message": (
                            f"[W{evt['worker']}] Teams batch "
                            f"{evt['batch']}/{evt['total_batches']} "
                            f"({evt['sellers_found']} sellers)"
                        ),
                        "progress": 85,
                        "worker": evt["worker"],
                    })

                for f in futures:
                    result = f.result()
                    account_sellers.update(result["account_sellers"])
                    unique_sellers.update(result["unique_sellers"])
                    account_ses.update(result["account_ses"])

            teams_time = round(time.time() - t0, 2)
            yield _sse({
                "message": (
                    f"Team queries done in {teams_time}s -- "
                    f"{len(unique_sellers)} sellers, "
                    f"{len(account_ses)} accounts with SEs"
                ),
                "progress": 86,
                "timing": {"phase": "teams_parallel", "seconds": teams_time},
            })

            # -------------------------------------------------------
            # Summary (no DB writes)
            # -------------------------------------------------------
            total_time = round(time.time() - total_start, 2)
            yield _sse({
                "message": f"Parallel import test complete in {total_time}s!",
                "progress": 100,
                "complete": True,
                "summary": {
                    "total_accounts": len(accounts_raw),
                    "total_territories": len(territories_raw),
                    "total_sellers": len(unique_sellers),
                    "accounts_with_sellers": len(account_sellers),
                    "accounts_with_ses": len(account_ses),
                    "timings": {
                        "scan_init": scan_time,
                        "accounts_parallel": accounts_time,
                        "territories_parallel": territories_time,
                        "teams_parallel": teams_time,
                        "total": total_time,
                    },
                },
            })

            logger.info(
                f"Parallel import test complete: {len(accounts_raw)} accounts, "
                f"{len(unique_sellers)} sellers in {total_time}s "
                f"(accounts={accounts_time}s, territories={territories_time}s, "
                f"teams={teams_time}s)"
            )

        except Exception as e:
            error_detail = f"[{type(e).__name__}] {e}"
            logger.exception(
                f"Error during parallel import test (phase: {phase}): {error_detail}"
            )
            yield _sse({"error": f"Failed during '{phase}': {error_detail}"})

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )
