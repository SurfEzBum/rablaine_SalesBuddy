# Plan: Revenue Sync from the MSXI API

**Branch:** `feature/revenue-api-sync`
**Status:** Built and validated. The CSV import, its fuzzy name matching, and the
hidden `/revenue/pull-test` beta page have since been deleted, so the
"keep CSV upload as a fallback" step below no longer reflects the code.
**Date:** August 6, 2026

Replace the manual CSV revenue import with a direct, headless pull from the MSXI
Power BI dataset. Two entry points: a backend sync the scheduler can run, and an
SSE endpoint so the user can watch a live import.

---

## 1. TL;DR of what the audit found

Every number below was measured against the live API and the prod database, not estimated.

| Question | Answer |
|---|---|
| Can we pull **all** buckets, not just the user's? | **Yes.** All 19 buckets, all 210 customers, one call, 11,188 rows, no truncation. |
| Is there a size limit? | **Yes: a hard 30,000-row cap per response**, server-side. Raising the window does nothing. |
| Does it bite us? | Only at product grain (~81,000 rows). Two proven workarounds. |
| Did the buckets change? | **Yes, and all 3 of the user's selected buckets no longer exist.** |
| Will TPID linking help? | **Massively.** 67% of revenue customer names never matched today, and the `tpid` column is empty on all 29,241 rows. |

---

## 2. API scope and size limits (measured)

### The cap is real, and it announces itself

The QES response carries two markers we can rely on:

- `IC` (**IsComplete**) - `false` means the result was truncated.
- `RT` (**RestartToken**) - the cursor to resume from.

Measured results (prod book, 210 TPIDs, FY26 + FY27):

| Query | Rows | Complete? | Time |
|---|---|---|---|
| Bucket grain (`ServiceCompGrouping`), all 19 buckets, one call | 11,188 | `IC=true` | 126s cold |
| Product grain (`ServiceLevel4`), one call, window 30,000 | 30,000 | `IC=false` + `RT` | 79s |
| Product grain, one call, window **200,000** | 30,000 | `IC=false` + `RT` | 5s |

That third row is the key finding: asking for a bigger window changes nothing.
**30,000 rows is enforced by the server.** This is the same ceiling that truncated
the CSV export when the bucket filter was removed.

### Two workarounds, both verified working

**(a) RestartToken pagination** - recommended.

```
page 0: 30,000 rows  IC=false  more=yes
page 1: 30,000 rows  IC=false  more=yes
page 2: 20,957 rows  IC=true   more=no
        -------------------------------
        80,957 rows total, ~10 seconds
```

**(b) TPID chunking** - already how `revenue_pull.py` works.

```
chunk of 25 TPIDs -> 8,034 rows  IC=true
chunk of 25 TPIDs -> 7,689 rows  IC=true
chunk of 25 TPIDs -> 8,643 rows  IC=true
chunk of 25 TPIDs -> 13,562 rows IC=true
```

### Recommendation

**Pull all buckets. Do not scope the pull to the user's selected buckets.**

Use TPID chunking as the primary strategy (it already exists, it parallelizes, and
it bounds each response), and add RestartToken pagination as a safety net so a single
oversized chunk can never silently truncate.

Critically, we must **treat `IC=false` with no pagination follow-up as a hard error**.
Silent truncation is the one failure mode that would corrupt the dataset without
anyone noticing. Never write a partial pull to the database.

Pulling all buckets also means the user's bucket *selection* becomes purely a
display filter, which is what it already is today. Changing their selection will
never require a re-import.

### Volume to expect per full sync

At the recommended 25-month depth (section 2a):

- Bucket grain: ~22,000 rows -> `CustomerRevenueData`
- Product grain: ~160,000 rows -> `ProductRevenueData`

For reference, prod currently holds 29,241 and 191,291 rows respectively across 12 months.
So the new dataset lands in the same ballpark as what is already stored.

---

## 2a. History depth (measured)

**The API gives us 25 months, which is more than double what we hoped for.**

`DimDate.FYRel` exposes `FY-12` through `FY+5`, but actual ACR data stops earlier:

| Query | Months returned | Notes |
|---|---|---|
| FY26 + FY27, `FYRel IN (FY-1, FY, FY+1)` **(current beta)** | **13** | FY26 full year + FY27-Jul |
| FY25 + FY26 + FY27, no `FYRel` filter | **25** | all 25 have non-zero ACR |
| FY24 + FY25 + FY26 + FY27, no `FYRel` filter | **25** | FY24 returns nothing |

Two takeaways:

1. **The `FYRel` filter was the limiter, not the data.** It is a report-level convenience
   filter we inherited from the captured query. Dropping it takes us from 13 to 25 months.
2. **FY25-Jul is the retention floor.** Asking for FY24 adds nothing, so there is no point
   reaching further back.

**Recommendation: pull FY25 + FY26 + FY27 (25 months) and drop the `FYRel` filter.**
That is a full 2 years plus the current partial year, well past the 6 months the trend
analysis started with and past the 12 months the user hoped for.

**Runtime caveat:** the 3-fiscal-year query took **221s cold** and 24s warm. Deeper history
costs real time on the first run of the day, which reinforces the need for heartbeats on
the scheduled path (section 7).

---

## 3. The bucket taxonomy changed (confirmed, and it affects this user today)

The new FY taxonomy is live in the API and it does not match what is stored.

**Stored in prod (old):** `Analytics`, `Core DBs`, `Modern DBs`, `Build Apps`,
`Modernize Apps`, `AI Infra`, `AI Services`, `Developer Platform`, `Github Copilot`,
`XCR ODAA`, `ACR - IP Co-Sell`, a blank-string bucket, plus 17 `PRACR - *` variants.

**Returned by the API (new, 19):** `App Services`, `Azure Virtual Desktop`,
`Copilot Studio`, `Databases`, `Defender for Cloud`, `Developer and GitHub Platform`,
`Fabric`, `Foundry`, `GPU IaaS`, `General Purpose Compute`, `GitHub Copilot`,
`Network Security`, `Other Services`, `Rest of Analytics`, `SAP`, `Sentinel`,
`XCR - AI Consumed Revenue`, `XCR - Power Apps Consumed Revenue`,
`XCR - Power BI Consumed Revenue`.

**The user's saved selection is `["Analytics", "Core DBs", "Modern DBs"]` - all three are gone.**
So the "wipe and re-prompt" path is not hypothetical; it fires on the first sync.

Two traps worth calling out:

1. **`Github Copilot` -> `GitHub Copilot`** is a capitalization-only rename. Comparison
   must be case-sensitive for display but case-insensitive for "did this disappear?",
   or we will wrongly tell the user a bucket vanished.
2. **`PRACR - *` buckets are retired this fiscal year** (confirmed by the user). They are
   ~40% of the stored bucket list and correctly absent from the API. No second pull is
   needed; they simply disappear in the reset.

### Where bucket selection lives today

- Stored in **two** places: `localStorage['salesbuddy_revenue_bucket_filter']` (primary)
  and `UserPreference.compensated_buckets` (JSON string, DB fallback).
- Read/written via `GET|POST /api/revenue/compensated-buckets` in `app/routes/revenue.py`.
- The available list is derived at runtime from `SELECT DISTINCT bucket FROM customer_revenue_data`
  (`GET /api/revenue/buckets`). Nothing is hardcoded, which is good for us.

**Consequence:** clearing the DB value is not enough. A stale `localStorage` copy would
silently win on the next page load. The reconciliation must invalidate the client copy too
(bump a `bucket_taxonomy_version` and have `revenue-bucket-filter.js` drop its cache when
the version changes).

### Proposed reconciliation flow

Runs at the start of every sync, cheaply, comparing the API bucket list against stored buckets.

```
new_buckets  = distinct buckets from this pull
old_buckets  = distinct buckets in CustomerRevenueData
selected     = UserPreference.compensated_buckets

if old_buckets and new_buckets != old_buckets:        # taxonomy shifted
    missing = [b for b in selected if b not in new_buckets]   # case-insensitive
    if missing:
        -> clear selection (DB + localStorage), status = "reset"
        -> tell user exactly which of their buckets disappeared
        -> present the new list so they can re-pick
    else:
        -> keep selection, status = "review"
        -> heads-up that the list changed and they may want to revisit
```

Surface the outcome in the sync result payload and persist it (a small
`UserPreference.bucket_taxonomy_notice` field or a `SyncStatus.details` entry) so the
banner survives the scheduled/headless path where nobody is watching a screen.

---

## 4. Customer linking via TPID (a big win)

Current state in prod, measured:

| Metric | Value |
|---|---|
| `CustomerRevenueData` rows | 29,241 |
| Rows with a `tpid` | **0** |
| Rows with a `customer_id` | 4,158 (14%) |
| Distinct customer names | 395 |
| **Names never matched to a customer** | **263 (67%)** |
| Rows with `seller_name` | 0 |

The `tpid` column exists on the model but the CSV importer never populates it. Matching
relies entirely on fuzzy name logic (`_progressive_word_prefix_match`, acronyms, etc.)
and it is failing for two thirds of accounts.

Because we query the API **by TPID**, every returned row already knows its TPID. We can
set `customer_id` by exact TPID lookup and delete the guessing entirely for this path.

Knock-on benefit: `revenue_analysis.py` derives `seller_name` from
`existing_customer.seller.name` (line ~777). It is empty today because `customer_id` is
mostly unset. Fixing linkage fixes seller attribution for revenue alerts and seller mode
as a side effect.

**Schema note:** the unique constraints are currently name-based
(`customer_name, bucket, month_date`). Keeping them avoids a migration and still works,
since we will write a consistent `TPAccountName` per TPID. Switching the natural key to
TPID is cleaner but is a bigger migration. **Recommendation: keep the existing constraint
for v1, populate `tpid` + `customer_id` on every row, and revisit the key later.**

---

## 5. What gets built

### New service: `app/services/revenue_sync.py`

Mirrors the shape of `milestone_sync.py`, which already solves the
"same logic, two entry points" problem.

- `sync_revenue()` -> dict. Blocking, no UI. Used by the scheduler.
- `sync_revenue_stream()` -> yields SSE events. Used by the live import UI.
- Both delegate to one internal generator so the logic exists once.

Phases (each reports progress):

1. Acquire token + mint MWCToken
2. Reconcile bucket taxonomy (section 3)
3. Pull bucket grain (all buckets, chunked, pagination-safe)
4. Pull product grain (all buckets, chunked, pagination-safe)
5. Validate: refuse to proceed on any incomplete/truncated result
6. Purge prior revenue data if the taxonomy changed (section 6)
7. Upsert `CustomerRevenueData` + `ProductRevenueData` with `tpid` + `customer_id`
8. Write `RevenueImport` record
9. Run the existing revenue analysis
10. Report

### Extend `app/services/revenue_pull.py`

- `IC` / `RT` handling with automatic pagination
- `pull_acr_products()` at `ServiceLevel4` grain
- Return the bucket list as a first-class result for reconciliation

### Routes in `app/routes/revenue.py`

Follow the milestone-sync precedent exactly:

```python
POST /api/revenue/sync
  Accept: text/event-stream  -> SSE live progress
  otherwise                  -> start in a thread, 202 {'status': 'started'}
```

Plus `GET /api/revenue/sync/status` reading `SyncStatus`.

### Scheduling

Register in `app/services/scheduled_sync.py` alongside milestone sync. Revenue moves
monthly, so a daily or Mon/Wed/Fri cadence is plenty. Reuse the existing per-user random
time slot and `_sync_lock` pattern.

New `SyncStatus` key: **`revenue_sync`** (keep `revenue_import` for the legacy CSV path
so history and the dashboard tile do not break).

### UI

Add a "Sync revenue now" control with a live progress bar to the revenue import page,
reusing the existing reader-based SSE consumer already in `templates/revenue_import.html`.
Keep manual CSV upload as a fallback.

---

## 6. The data reset

When the taxonomy changes, previously imported rows are tied to buckets that no longer
exist and cannot be reconciled. They must go.

Delete `RevenueReviewNote` -> `RevenueAnalysis` -> `ProductRevenueData` ->
`CustomerRevenueData` -> `RevenueImport`. **Do not delete `RevenueConfig`** (the user's
thresholds) and do not touch `Customer` rows.

Note that the existing `/api/admin/clear-revenue` route *does* delete `RevenueConfig`, so
we need a narrower purge helper rather than reusing it as-is.

Guard rails:

- Only purge when the taxonomy actually changed, or on an explicit "full refresh".
- Purge and re-import inside one transaction where practical, so a mid-sync failure
  cannot leave the user with no revenue data at all.
- **Back up the database before the first destructive sync** (`backup_database()` already exists).
- Snapshot and re-attach review data first (section 6a).

### 6a. Preserving review notes

**Decision:** keep review data where it can still be displayed; archive the rest to JSON
before deleting it.

```
1. Snapshot BEFORE any delete:
   - every RevenueAnalysis row carrying user state
     (review_notes non-empty OR review_status not in ('new',''))
   - the full RevenueReviewNote history for those rows
   -> write <data_dir>/revenue_review_archive_<UTC timestamp>.json  (always, for posterity)

2. Purge + re-import + re-run analysis (regenerates RevenueAnalysis rows)

3. Re-attach a snapshot entry only if BOTH still resolve:
   - the customer exists   (match on customer_id, else normalized customer_name)
   - the bucket exists     (case-insensitive match against the new bucket list)
   Restore review_status, review_notes, reviewed_at, previous_* and the
   RevenueReviewNote history against the new analysis id.

4. Anything that cannot be re-attached stays only in the JSON archive.
   Report the counts (restored / archived-only) in the sync result.
```

The JSON archive is written unconditionally, even when everything re-attaches cleanly, so
there is always a rollback artifact. It lives in the resolved data dir (next to the
database, outside the install dir) so an upgrade cannot delete it.

**Measured scale in the plan author's prod DB - but this is NOT the whole story:**

| Metric | Value |
|---|---|
| `RevenueAnalysis` rows | 1,650 |
| Rows with status `new` (untouched) | 1,649 |
| Rows with non-empty `review_notes` | 0 |
| Rows the user has actually touched | 1 (`to_be_reviewed`, bucket `Core DBs`) |
| `RevenueReviewNote` history rows | 0 |

**Do not treat this as low risk.** Review notes were requested by, and are actively used
by, the tester - not the plan author. This database simply is not the one that holds the
data. The preservation logic must be correct and tested against a database that actually
has notes, and the tester should be asked to confirm restoration after the first sync.
Build it properly; do not shortcut it because the numbers above look empty.

---

## 7. Risks and open questions

### Resolved

- **`PRACR - *` buckets:** retired this fiscal year. No second pull needed; they drop out
  with the reset.
- **Review notes:** preserve where the customer and bucket both still resolve, archive the
  rest to JSON in the data folder, then delete. See section 6a. Owned by the tester, so
  correctness matters even though the author's DB looks empty.
- **History depth:** pull **FY25 + FY26 + FY27 = 25 months**, dropping the `FYRel` filter.
  FY24 returns nothing, so that is the practical floor. See section 2a.
- **False churn alerts:** an account is only eligible to be flagged as declining or churned
  if its pull **succeeded** and it **previously had data**. Absence of rows means "not
  covered by MSX right now", never "revenue went to zero". Accounts that drop out of
  coverage keep their prior state and are excluded from analysis rather than alerted on.
- **Blank bucket:** rows with an empty or whitespace-only `ServiceCompGrouping` are skipped
  on write. They exist in the old CSV-sourced data and carry no meaning.

### Still open

1. **Runtime.** The 3-fiscal-year pull took 221s cold, 24s warm. The scheduled path must
   tolerate multi-minute runs and keep `SyncStatus.update_heartbeat()` alive so the
   supervisor does not consider it hung.

2. **Token expiry mid-sync.** MWCToken is ~30 minutes. `revenue_pull.py` already re-mints
   on 401; needs testing under a long run at 25-month depth.

---

## 8. Work breakdown

| # | Task | Depends on |
|---|---|---|
| 1 | `revenue_pull.py`: IC/RT pagination + truncation guard | - |
| 2 | `revenue_pull.py`: product-grain pull | 1 |
| 3 | Bucket reconciliation service + `localStorage` invalidation | - |
| 4 | Review-note archive + re-attach helper (section 6a) | - |
| 5 | Narrow purge helper (preserves `RevenueConfig`) | 4 |
| 6 | `revenue_sync.py` core generator + TPID/customer_id linking | 1-5 |
| 7 | `sync_revenue()` + `sync_revenue_stream()` wrappers | 6 |
| 8 | Routes: `POST /api/revenue/sync` (SSE + JSON) and status | 7 |
| 9 | Scheduler registration | 7 |
| 10 | UI: sync button, progress, bucket-change banner | 8 |
| 11 | Tests | 6-9 |
| 12 | Changelog + docs | all |

### Testing

- Unit: bucket reconciliation (disappeared / renamed-case / unchanged), truncation guard,
  fiscal-month parsing, TPID linking.
- Integration: full sync against a temp DB; verify row counts and that
  `tpid`/`customer_id` are populated on 100% of rows.
- Parity: re-run the CSV cross-reference that already validated at
  **$789,964 vs $789,964, 94/94 customers exact**, and require it to still match.
- Failure paths: truncated response must abort without writing; VPN/token failure must
  not purge data.

### Test fixture already in place (dev database)

The dev database now holds a **copy of prod** (210 customers, 29,241 revenue rows,
1,650 analyses) so development runs against real data. The original FY26 dev database is
preserved at:

```
data/DEV_FY26_BACKUP_2026-08-06.db      (305 customers, 262 notes, 1,645 analyses)

restore with:
Copy-Item data\DEV_FY26_BACKUP_2026-08-06.db data\salesbuddy.db -Force
```

Eight review notes plus three history rows are seeded, tagged `[TEST-x]`, covering every
preservation path. **These are the acceptance criteria for section 6a:**

| Path | Analysis id | Bucket | Customer resolves | Expected |
|---|---|---|---|---|
| A | 885 | `Sentinel` | yes | **RESTORE** |
| A | 631 | `Defender for Cloud` | yes | **RESTORE** |
| A | 471 | `Other Services` | yes | **RESTORE** |
| B | 964 | `Github Copilot` -> `GitHub Copilot` | yes | **RESTORE** (case-insensitive) |
| C | 1 | `Core DBs` (retired) | yes | archive only |
| C | 27 | `Analytics` (retired) | yes | archive only |
| D | 605 | `PRACR - Core DBs` (retired) | yes | archive only |
| E | 480 | `Sentinel` | **no** | archive only |

Path F: analysis 885 carries two `RevenueReviewNote` history rows and analysis 1 carries
one. The history on 885 must survive the migration; the history on 1 must end up in the
archive only.

Path B is the important one. `Github Copilot` was renamed to `GitHub Copilot` (capital H
only). A case-sensitive comparison would wrongly report it as retired and throw the note
away, so this case must pass before the reset is considered safe.

---

## 9. Recommendation

The mechanism is proven and dollar-accurate, and every blocking question is now resolved.
The scope landed better than expected on two fronts: we can pull **all** buckets rather
than scoping to the user's, and we get **25 months** of history instead of the 12 we hoped
for.

The two things to get right are the **destructive reset** (guarded by a DB backup and a
JSON archive) and **review-note preservation** (the tester's feature, so it needs real
testing against a database that actually has notes).

**Ready to build on approval.**
