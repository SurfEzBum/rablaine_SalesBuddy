# Import ACR Revenue from MSX Insights

This guide explains how Sales Buddy retrieves Azure Consumed Revenue (ACR)
from the MSX Insights Power BI semantic model and imports it by customer TPID.

**Last verified:** August 24, 2026
**Authentication tenant:** `72f988bf-86f1-41af-91ab-2d7cd011db47`
**Power BI resource:** `https://analysis.windows.net/powerbi/api`
**Report ID:** `4774bb5f-91a6-4e41-8c8a-0cee2142b765`
**Dataset ID:** `f7ecc250-c244-43a6-aea5-7a957f9e9d38`
**Fallback model ID:** `6642435`

## Source of Truth

ACR comes from the semantic model behind the MSX Insights
`ACR Details by Quarter / Month SL4` report. We query the model directly rather
than downloading its CSV export.

The flow is:

```text
az login
  -> Power BI AAD token
  -> report modelsAndExploration
  -> MWCToken + capacity/model details
  -> QES semantic query
  -> Power BI DSR rows
  -> exact TPID-to-customer link
  -> revenue database tables
```

Power BI row-level security remains active. Results are limited to accounts the
signed-in user can access in MSX.

## Authentication

Prerequisites:

- Microsoft corporate account
- Azure CLI
- Access to the MSX Insights report
- VPN or approved corporate network access when required

```powershell
az login --tenant 72f988bf-86f1-41af-91ab-2d7cd011db47
```

Sales Buddy uses `AzureCliCredential` to request this scope:

```text
https://analysis.windows.net/powerbi/api/.default
```

No client secret or separate app registration is used.

## Token and Query Bootstrap

1. Acquire the Power BI AAD token.
2. Call the report bootstrap endpoint:

```http
GET https://df-msit-scus-redirect.analysis.windows.net/
    explore/reports/4774bb5f-91a6-4e41-8c8a-0cee2142b765/
    modelsAndExploration?preferReadOnlySession=true

Authorization: Bearer {power_bi_token}
```

3. Extract the JWT whose `tokenType` claim is `MwcToken`.
4. Find the numeric model ID whose `dbName` equals the dataset ID.
5. Read `customerCapacityObjectId` and `rolloutFqdn` from the MWCToken to build
   the capacity Query Execution Service (QES) URL.
6. POST semantic queries to the QES `automatic/public/query` endpoint with:

```http
Authorization: MWCToken {mwc_token}
x-ms-workload-resource-moniker: f7ecc250-c244-43a6-aea5-7a957f9e9d38
Origin: https://msit.powerbi.com
Referer: https://msit.powerbi.com/
```

Tokens are cached until 60 seconds before expiry. A QES `401` clears both token
caches, mints fresh tokens, and retries.

## Query Shape

Sales Buddy runs two queries for all customer TPIDs.

| Grain | Fields | Destination |
|---|---|---|
| Bucket | TPID, account name, fiscal month, `ServiceCompGrouping`, `$ ACR` | `CustomerRevenueData` |
| Product | TPID, account name, fiscal month, `ServiceCompGrouping`, `ServiceLevel4`, `$ ACR` | `ProductRevenueData` |

Both queries use these semantic-model entities:

```text
DimDate
DimCustomer
Fact ACR Subscription
Measures | ACR
Parameter | ACR Attributes
Parameter | ACR Measures
```

Required filters:

| Field | Filter |
|---|---|
| `DimDate.FiscalYear` | Requested fiscal years |
| `Fact ACR Subscription.AdjustmentFlag` | `N/A` |
| `Parameter | ACR Attributes` | `ServiceCompGrouping` or `ServiceLevel4` |
| `Parameter | ACR Measures Fields` | `'Measures | ACR'[$ ACR]` |
| `DimCustomer.HQDS` | Exclude `DS` |
| `DimCustomer.TPID` | Positive and in requested TPID list |

The parameter selections are required for the `$ ACR` measure to resolve.
There is deliberately no `FYRel` filter because it reduces available history.

Default history is current fiscal year plus two prior fiscal years. Microsoft
fiscal years begin in July. The live model currently returns about 25 months.

## Python Example

This example runs from a Sales Buddy checkout and exports both grains to CSV.
It calls the same tested pull service used by the application, including token
bootstrap, TPID chunking, QES pagination, DSR decoding, retries, and partial-data
protection.

Install dependencies and sign in first:

```powershell
pip install azure-identity requests
az login --tenant 72f988bf-86f1-41af-91ab-2d7cd011db47
```

Save this as `pull_acr_example.py` in the repository root:

```python
"""Export MSX Insights ACR for selected customer TPIDs."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from app.services.revenue_pull import (
    default_fiscal_years,
    pull_acr_for_customers,
    pull_products_for_customers,
)


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    """Write query rows to a UTF-8 CSV file."""
    with path.open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def report_progress(label: str):
    """Build a progress callback for one pull phase."""
    def callback(done: int, total: int, row_count: int) -> None:
        if done == 0:
            print(f"{label}: connected; running {total} batches")
        else:
            print(f"{label}: {done}/{total} batches, {row_count:,} rows")

    return callback


def main() -> None:
    """Pull bucket and product ACR rows for command-line TPIDs."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "tpids",
        nargs="+",
        type=int,
        help="One or more numeric customer TPIDs",
    )
    parser.add_argument(
        "--fiscal-years",
        nargs="+",
        help="Fiscal years such as FY25 FY26 FY27; defaults to latest three",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("acr-output"))
    args = parser.parse_args()

    fiscal_years = args.fiscal_years or default_fiscal_years()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    bucket_rows = pull_acr_for_customers(
        args.tpids,
        fiscal_years=fiscal_years,
        progress=report_progress("Bucket ACR"),
    )
    product_rows = pull_products_for_customers(
        args.tpids,
        fiscal_years=fiscal_years,
        progress=report_progress("Product ACR"),
    )

    if not bucket_rows:
        raise RuntimeError("MSXI returned no bucket ACR rows")

    write_csv(
        args.output_dir / "acr_by_bucket.csv",
        bucket_rows,
        ["tpid", "name", "fm", "bucket", "acr"],
    )
    write_csv(
        args.output_dir / "acr_by_product.csv",
        product_rows,
        ["tpid", "name", "fm", "bucket", "product", "acr"],
    )
    print(
        f"Done: {len(bucket_rows):,} bucket rows and "
        f"{len(product_rows):,} product rows for {len(args.tpids)} TPIDs"
    )


if __name__ == "__main__":
    main()
```

Example invocation:

```powershell
python pull_acr_example.py 1234567 7654321 --fiscal-years FY25 FY26 FY27
```

Returned bucket rows have this shape:

```json
{
  "tpid": 1234567,
  "name": "Contoso",
  "fm": "FY27-Aug",
  "bucket": "Databases",
  "acr": 15342.18
}
```

Product rows add `product`, populated from `ServiceLevel4`.

## Pagination and Completeness

QES caps each response at 30,000 rows regardless of requested window size.
Power BI returns:

- `IC=false` when more data exists
- `RT` as the restart token for the next page

Sales Buddy follows every restart token and removes exact duplicate boundary
rows. If `IC=false` appears without an `RT`, the pull fails. Partial datasets
must never be imported.

Queries are also split by TPID:

- Bucket grain: 40 TPIDs per batch
- Product grain: 25 TPIDs per batch
- Up to four concurrent workers

Product batches are smaller because `ServiceLevel4` produces substantially more
rows.

## Import Semantics

1. Load distinct positive numeric TPIDs from local customers.
2. Pull all bucket rows successfully.
3. Pull all product rows successfully.
4. Stop without changing stored data if either pull fails or bucket rows are
   empty.
5. Compare new bucket taxonomy with stored taxonomy.
6. Archive user review state to timestamped JSON.
7. Purge old revenue rows while preserving valid review state and configuration.
8. Insert bucket and product rows.
9. Link each row to a customer by exact TPID. No fuzzy name matching is used.
10. Re-run trend analysis and restore review state for surviving customer and
    bucket combinations.

The import writes only after both remote pulls finish. A token, network,
pagination, or query failure therefore leaves existing revenue data untouched.

## Important Behavior

- Pull all buckets. User-selected compensated buckets are display filters, not
  query filters.
- Treat TPID as the customer identity. `TPAccountName` is display text.
- Skip blank buckets, blank products, invalid TPIDs, and invalid fiscal months.
- Convert fiscal months such as `FY27-Aug` to calendar month dates.
- Keep `RevenueConfig` and import history during refreshes.
- Bucket comparisons are case-insensitive so capitalization-only changes such
  as `Github Copilot` to `GitHub Copilot` do not retire review notes.
- If a selected bucket disappears, clear the stale selection and notify the
  user to review the new taxonomy.
- Scheduled refresh runs weekly because ACR changes monthly.

## Sales Buddy Implementation

- Remote transport and semantic queries: `app/services/revenue_pull.py`
- Import orchestration and database writes: `app/services/revenue_sync.py`
- Bucket reconciliation and review preservation:
  `app/services/revenue_taxonomy.py`
- Focused tests: `tests/test_revenue_sync.py`