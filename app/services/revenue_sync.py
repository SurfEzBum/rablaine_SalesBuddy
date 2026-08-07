"""
Revenue sync from the MSXI API.

Pulls ACR straight from the Power BI dataset using the caller's ``az login``
and upserts it into the revenue tables.

Two entry points share one implementation, mirroring ``milestone_sync``:

- :func:`sync_revenue` blocks and returns a summary. Used by the scheduler.
- :func:`sync_revenue_stream` yields SSE events. Used by the live import UI.

Two things make this more than a data load:

- The MSXI bucket taxonomy changes at the fiscal-year boundary, which strands
  previously imported rows and forces a purge (see ``revenue_taxonomy``).
- Because we query *by* TPID, every row links to a customer exactly, with no
  name matching involved.
"""
from __future__ import annotations

import json
import logging
import queue
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Generator, Optional

from app.models import (
    db, Customer, CustomerRevenueData, ProductRevenueData, RevenueImport, SyncStatus,
)
from app.services import revenue_pull, revenue_taxonomy
from app.services.revenue_import import fiscal_month_to_date

logger = logging.getLogger(__name__)

SYNC_TYPE = "revenue_sync"


def _sse(event_type: str, data: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def has_revenue_data() -> bool:
    """True once revenue has loaded.

    Installs that predate the API sync recorded a ``revenue_import`` status
    instead. That row still describes real data sitting in their tables, so
    keep honouring it or those users get re-prompted to onboard.
    """
    return SyncStatus.is_complete(SYNC_TYPE) or SyncStatus.is_complete("revenue_import")


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
def _pull_with_progress(fn, tpids, fys, phase, lo, hi, label, heartbeat):
    """Run a pull on a worker thread, yielding progress as its batches land.

    The pull splits TPIDs into batches and reports each one, but its callback
    fires on a worker thread and so cannot yield. It hands events to a queue
    that this generator drains instead. Returns the pulled rows, so callers use
    ``rows = yield from _pull_with_progress(...)``.

    Blocking on the queue with a timeout (rather than polling ``done()`` behind
    a sleep) keeps event latency at the speed of the batches while still exiting
    if a worker dies without reporting.
    """
    events: queue.Queue = queue.Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            fn, tpids, fiscal_years=fys,
            progress=lambda done, total, rows: events.put((done, total, rows)),
        )
        while True:
            try:
                done, total, rows = events.get(timeout=0.4)
            except queue.Empty:
                if future.done():
                    break
                continue
            heartbeat()
            if done == 0:
                message = f"{label}: connected to MSXI, running {total} batches..."
            else:
                message = f"{label}: {rows:,} rows from {done}/{total} batches..."
            yield {
                "phase": phase,
                "progress": lo + int((hi - lo) * done / max(total, 1)),
                "message": message,
            }
    # Re-raises anything the pull threw, so the caller's handler sees it.
    return future.result()


def _sync_generator(fiscal_years: Optional[list[str]] = None,
                    run_analysis: bool = True) -> Generator[dict[str, Any], None, None]:
    """Run the sync, yielding progress dicts and finally a ``complete`` dict.

    Nothing is deleted until every pull has completed successfully, so a network
    or token failure mid-sync leaves the existing data untouched.
    """
    started = time.time()
    SyncStatus.mark_started(SYNC_TYPE)

    def heartbeat():
        try:
            SyncStatus.update_heartbeat(SYNC_TYPE)
        except Exception:  # noqa: BLE001 - heartbeat is best effort
            pass

    try:
        # --- 1. who are we pulling for -------------------------------------
        yield {"phase": "customers", "message": "Loading accounts..."}
        customers = revenue_pull.get_customer_tpids()
        if not customers:
            yield {"error": "No customers with a TPID found. Import accounts first."}
            SyncStatus.mark_completed(SYNC_TYPE, success=False, details="no customers")
            return
        tpids = [t for t, _ in customers]
        fys = fiscal_years or revenue_pull.default_fiscal_years()
        yield {"phase": "customers", "progress": 5,
               "message": f"Syncing {len(tpids)} accounts across {', '.join(fys)}..."}
        heartbeat()

        # --- 2. pull bucket grain ------------------------------------------
        yield {"phase": "pull_buckets", "progress": 8,
               "message": "Pulling revenue by bucket from MSXI..."}
        bucket_rows = yield from _pull_with_progress(
            revenue_pull.pull_acr_for_customers, tpids, fys,
            "pull_buckets", 8, 30, "Pulling revenue by bucket", heartbeat)
        heartbeat()
        yield {"phase": "pull_buckets", "progress": 30,
               "message": f"Retrieved {len(bucket_rows):,} bucket rows."}

        # --- 3. pull product grain -----------------------------------------
        yield {"phase": "pull_products", "progress": 32,
               "message": "Pulling revenue by product..."}
        product_rows = yield from _pull_with_progress(
            revenue_pull.pull_products_for_customers, tpids, fys,
            "pull_products", 32, 55, "Pulling revenue by product", heartbeat)
        heartbeat()
        yield {"phase": "pull_products", "progress": 55,
               "message": f"Retrieved {len(product_rows):,} product rows."}

        if not bucket_rows:
            yield {"error": "MSXI returned no revenue rows. Nothing was changed."}
            SyncStatus.mark_completed(SYNC_TYPE, success=False, details="empty pull")
            return

        # --- 4. reconcile the bucket taxonomy -------------------------------
        new_buckets = {(r.get("bucket") or "").strip()
                       for r in bucket_rows if (r.get("bucket") or "").strip()}
        notice = revenue_taxonomy.reconcile_buckets(new_buckets)
        taxonomy_changed = notice["status"] in ("reset", "review")
        yield {"phase": "taxonomy", "progress": 58,
               "message": ("Bucket list changed since the last import."
                           if taxonomy_changed else "Bucket list unchanged."),
               "notice": notice}

        # --- 5. preserve review notes, then purge ---------------------------
        snapshot = revenue_taxonomy.snapshot_review_state()
        archive_path = None
        classified = {"keep_analysis_ids": set(), "kept_count": 0, "dropped_count": 0,
                      "dropped": []}
        if snapshot:
            archive_path = revenue_taxonomy.archive_review_state(
                snapshot, reason=notice["status"])
            classified = revenue_taxonomy.classify_review_state(snapshot, new_buckets)
            yield {"phase": "archive", "progress": 60,
                   "message": (f"Archived {len(snapshot)} review note(s); keeping "
                               f"{classified['kept_count']}, retiring {classified['dropped_count']}.")}

        yield {"phase": "purge", "progress": 62, "message": "Clearing previous revenue data..."}
        purged = revenue_taxonomy.purge_revenue_data(classified["keep_analysis_ids"])
        heartbeat()

        # --- 6. write the new data ------------------------------------------
        yield {"phase": "write", "progress": 65, "message": "Writing revenue data..."}
        stats = _write_rows(bucket_rows, product_rows, customers)
        heartbeat()
        yield {"phase": "write", "progress": 85,
               "message": (f"Wrote {stats['bucket_rows']:,} bucket and "
                           f"{stats['product_rows']:,} product rows.")}

        # --- 7. analysis -----------------------------------------------------
        # Preserved analyses are upserted on (customer_name, bucket), so their
        # review state refreshes in place rather than needing re-attachment.
        analysis_stats = None
        if run_analysis:
            yield {"phase": "analysis", "progress": 88, "message": "Analyzing revenue trends..."}
            from app.services.revenue_analysis import run_analysis_streaming
            last_pct = 88
            for update in run_analysis_streaming():
                if update.get("complete"):
                    analysis_stats = update["stats"]
                    continue
                current, total = update["current"], update["total"]
                pct = 88 + int(8 * current / max(total, 1))
                if pct == last_pct:
                    continue  # ~1650 accounts, so only emit when the bar moves
                last_pct = pct
                heartbeat()
                yield {"phase": "analysis", "progress": pct,
                       "message": f"Analyzing account {current:,} of {total:,}..."}
            heartbeat()

        if snapshot:
            yield {"phase": "restore", "progress": 96,
                   "message": (f"Kept {classified['kept_count']} review note(s); "
                               f"{classified['dropped_count']} retired with their buckets.")}

        revenue_taxonomy.apply_bucket_notice(notice)

        result = {
            "customers": len(tpids),
            "customers_with_data": stats["customers_with_data"],
            "bucket_rows": stats["bucket_rows"],
            "product_rows": stats["product_rows"],
            "months": stats["months"],
            "fiscal_years": fys,
            "linked_pct": stats["linked_pct"],
            "taxonomy": notice,
            "purged": purged,
            "review_kept": classified["kept_count"],
            "review_retired": classified["dropped_count"],
            "review_retired_entries": classified["dropped"],
            "archive_path": str(archive_path) if archive_path else None,
            "analysis": analysis_stats,
            "duration_s": round(time.time() - started, 1),
        }
        SyncStatus.mark_completed(
            SYNC_TYPE, success=True,
            items_synced=stats["bucket_rows"] + stats["product_rows"],
            details=json.dumps({k: v for k, v in result.items() if k != "analysis"}, default=str),
        )
        yield {"complete": True, "progress": 100, "result": result}

    except Exception as exc:  # noqa: BLE001 - surface a clean failure to the caller
        logger.error("revenue sync failed: %s", exc, exc_info=True)
        db.session.rollback()
        SyncStatus.mark_completed(SYNC_TYPE, success=False, details=str(exc)[:500])
        yield {"error": str(exc)}


def _write_rows(bucket_rows: list[dict], product_rows: list[dict],
                customers: list[tuple[int, str]]) -> dict[str, Any]:
    """Insert pulled rows, linking every one to its customer by TPID.

    The tables are empty at this point (the purge ran first), so this is a plain
    insert rather than an upsert.
    """
    tpid_to_customer: dict[int, int] = {}
    for cid, tpid in Customer.query.with_entities(Customer.id, Customer.tpid).all():
        try:
            tpid_to_customer[int(tpid)] = cid
        except (TypeError, ValueError):
            continue

    import_record = RevenueImport(
        filename=f"MSXI API sync {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
        record_count=len(bucket_rows),
    )
    db.session.add(import_record)
    db.session.flush()

    months: set = set()
    linked = 0
    seen_customers: set[int] = set()

    def prepare(row: dict) -> Optional[tuple]:
        bucket = (row.get("bucket") or "").strip()
        if not bucket:  # blank ServiceCompGrouping carries no meaning
            return None
        month_date = fiscal_month_to_date(str(row.get("fm") or ""))
        if month_date is None:
            return None
        try:
            tpid = int(row.get("tpid"))
        except (TypeError, ValueError):
            return None
        return bucket, month_date, tpid

    bucket_objs = []
    for row in bucket_rows:
        prepared = prepare(row)
        if not prepared:
            continue
        bucket, month_date, tpid = prepared
        months.add(month_date)
        seen_customers.add(tpid)
        cid = tpid_to_customer.get(tpid)
        if cid:
            linked += 1
        bucket_objs.append(CustomerRevenueData(
            customer_name=(row.get("name") or "").strip(),
            tpid=str(tpid),
            bucket=bucket,
            customer_id=cid,
            fiscal_month=str(row.get("fm")),
            month_date=month_date,
            revenue=float(row.get("acr") or 0.0),
            last_import_id=import_record.id,
        ))

    product_objs = []
    for row in product_rows:
        prepared = prepare(row)
        if not prepared:
            continue
        bucket, month_date, tpid = prepared
        product = (row.get("product") or "").strip()
        if not product:
            continue
        product_objs.append(ProductRevenueData(
            customer_name=(row.get("name") or "").strip(),
            bucket=bucket,
            product=product,
            customer_id=tpid_to_customer.get(tpid),
            fiscal_month=str(row.get("fm")),
            month_date=month_date,
            revenue=float(row.get("acr") or 0.0),
            last_import_id=import_record.id,
        ))

    db.session.bulk_save_objects(bucket_objs)
    db.session.bulk_save_objects(product_objs)

    import_record.records_created = len(bucket_objs) + len(product_objs)
    import_record.new_months_added = len(months)
    if months:
        import_record.earliest_month = min(months)
        import_record.latest_month = max(months)
    db.session.commit()

    return {
        "bucket_rows": len(bucket_objs),
        "product_rows": len(product_objs),
        "months": len(months),
        "customers_with_data": len(seen_customers),
        "linked_pct": round(100 * linked / len(bucket_objs), 1) if bucket_objs else 0.0,
    }


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def sync_revenue(fiscal_years: Optional[list[str]] = None,
                 run_analysis: bool = True,
                 progress: Optional[Callable[[dict], None]] = None) -> dict[str, Any]:
    """Run a full revenue sync. Blocking; used by the scheduler and scripts."""
    result: dict[str, Any] = {"success": False}
    for update in _sync_generator(fiscal_years, run_analysis):
        if update.get("complete"):
            result = {"success": True, **update["result"]}
        elif update.get("error"):
            result = {"success": False, "error": update["error"]}
        elif progress:
            progress(update)
    return result


def sync_revenue_stream(fiscal_years: Optional[list[str]] = None,
                        run_analysis: bool = True) -> Generator[str, None, None]:
    """Run a full revenue sync, yielding SSE events for a live progress UI."""
    yield _sse("start", {"message": "Starting revenue sync..."})
    for update in _sync_generator(fiscal_years, run_analysis):
        if update.get("complete"):
            yield _sse("complete", update["result"])
        elif update.get("error"):
            yield _sse("error", {"error": update["error"]})
        else:
            yield _sse("progress", update)
