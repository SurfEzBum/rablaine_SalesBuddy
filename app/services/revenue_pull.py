"""
Programmatic MSXI revenue (ACR) pull - headless, via the caller's ``az login``.

This queries the same Power BI dataset that backs the MSX Insights
"ACR Details by Quarter / Month SL4" report the export CSV comes from, so for the
same measure + grain + fiscal filter the numbers match to the dollar.

Auth flow (no browser, no manual token):
1. Acquire a Power BI token (``analysis.windows.net/powerbi/api``) via the Azure
   CLI credential - the same ``az login`` we use for the AI gateway.
2. GET the report's ``modelsAndExploration``; its response hands back an
   **MWCToken** (the dedicated-capacity workload token) minted for our identity.
3. POST the semantic query to the capacity's QES ``public/query`` endpoint with
   that MWCToken. Row-level security scopes results to our MSX-assigned accounts.

Results are read-only. Nothing here writes to the database - ``revenue_sync``
owns the write side.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


class RevenuePullError(Exception):
    """Raised when a programmatic revenue pull fails."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_CORP_TENANT = "72f988bf-86f1-41af-91ab-2d7cd011db47"
_PBI_RESOURCE = "https://analysis.windows.net/powerbi/api"
_CLUSTER = "https://df-msit-scus-redirect.analysis.windows.net"

# The MSX Insights "AzureSubscriptionDetailsSL4_FY" report + dataset.
_REPORT_ID = "08a38b76-64a9-4dd0-848c-fe97dff1b189"
_DATASET_ID = "7678a8b7-ee5b-4ed8-8951-66c60d456a9c"
_VISUAL_ID = "e3f7b28054eb83b11005"
_MODEL_ID_FALLBACK = 6617391

# ---------------------------------------------------------------------------
# Token acquisition
# ---------------------------------------------------------------------------
_pbi_token: Optional[str] = None
_pbi_expiry: float = 0.0


def _decode_jwt(tok: str) -> dict:
    try:
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _get_pbi_token() -> str:
    """Acquire (and cache) a Power BI AAD token via the Azure CLI credential."""
    global _pbi_token, _pbi_expiry
    if _pbi_token and time.time() < _pbi_expiry - 60:
        return _pbi_token

    from azure.identity import AzureCliCredential, DefaultAzureCredential

    scope = f"{_PBI_RESOURCE}/.default"
    for kwargs in ({"tenant_id": _CORP_TENANT}, {}):
        try:
            tok = AzureCliCredential(**kwargs).get_token(scope)
            _pbi_token, _pbi_expiry = tok.token, tok.expires_on
            return _pbi_token
        except Exception as exc:  # noqa: BLE001 - try next strategy
            logger.warning("AzureCliCredential (%s) failed: %s", kwargs or "default", exc)
    try:
        tok = DefaultAzureCredential().get_token(scope)
        _pbi_token, _pbi_expiry = tok.token, tok.expires_on
        return _pbi_token
    except Exception as exc:  # noqa: BLE001
        raise RevenuePullError(
            "Could not acquire a Power BI token. Run `az login` with your "
            "Microsoft corporate account and make sure you're on the VPN."
        ) from exc


# ---------------------------------------------------------------------------
# MWCToken mint (from modelsAndExploration) + capacity resolution
# ---------------------------------------------------------------------------
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}")

_mwc_token: Optional[str] = None
_mwc_expiry: float = 0.0
_qes_url: Optional[str] = None
_model_id: int = _MODEL_ID_FALLBACK


def _is_mwc(tok: str) -> bool:
    claims = _decode_jwt(tok)
    return claims.get("tokenType") == "MwcToken" or "pbidedicated" in str(claims.get("iss", ""))


def _qes_url_from_mwc(mwc: str) -> str:
    """Build the capacity's QES query URL from the MWCToken's own claims."""
    claims = _decode_jwt(mwc)
    capacity = claims.get("customerCapacityObjectId")
    fqdn = claims.get("rolloutFqdn") or "msit.pbidedicated.windows.net"
    if not capacity:
        raise RevenuePullError("MWCToken missing capacity id")
    suffix = fqdn.split(".", 1)[1] if "." in fqdn else "pbidedicated.windows.net"
    host = capacity.replace("-", "") + "." + suffix
    return (f"https://{host}/webapi/capacities/{capacity}/workloads/QES/"
            f"QueryExecutionService/automatic/public/query")


def _mint_mwc(session: requests.Session) -> str:
    """Mint an MWCToken by loading the report's models; cache until near expiry."""
    global _mwc_token, _mwc_expiry, _qes_url, _model_id
    if _mwc_token and time.time() < _mwc_expiry - 60 and _qes_url:
        return _mwc_token

    url = f"{_CLUSTER}/explore/reports/{_REPORT_ID}/modelsAndExploration?preferReadOnlySession=true"
    resp = session.get(url, headers={"Authorization": f"Bearer {_get_pbi_token()}"}, timeout=(15, 120))
    if not resp.ok:
        raise RevenuePullError(f"modelsAndExploration {resp.status_code}: {resp.text[:200]}")

    mwc = next((c for c in _JWT_RE.findall(resp.text) if _is_mwc(c)), None)
    if not mwc:
        raise RevenuePullError("No MWCToken in modelsAndExploration response")

    # Resolve the numeric model id for this dataset (fall back to the known one).
    try:
        for m in (resp.json().get("models") or []):
            if m.get("dbName") == _DATASET_ID and m.get("id"):
                _model_id = int(m["id"])
                break
    except Exception:  # noqa: BLE001 - keep the fallback model id
        pass

    _mwc_token = mwc
    _mwc_expiry = float(_decode_jwt(mwc).get("exp") or (time.time() + 1500))
    _qes_url = _qes_url_from_mwc(mwc)
    return _mwc_token


def clear_token_cache() -> None:
    """Clear cached Power BI + MWC tokens (call after a fresh ``az login``)."""
    global _pbi_token, _pbi_expiry, _mwc_token, _mwc_expiry, _qes_url
    _pbi_token = _mwc_token = _qes_url = None
    _pbi_expiry = _mwc_expiry = 0.0


# ---------------------------------------------------------------------------
# Semantic-query builders + DSR decode
# ---------------------------------------------------------------------------
def _col(s: str, p: str, n: str) -> dict:
    return {"Column": {"Expression": {"SourceRef": {"Source": s}}, "Property": p}, "Name": n}


def _mea(s: str, p: str, n: str) -> dict:
    return {"Measure": {"Expression": {"SourceRef": {"Source": s}}, "Property": p}, "Name": n}


def _in(s: str, p: str, vals: list[str]) -> dict:
    return {"Condition": {"In": {
        "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": s}}, "Property": p}}],
        "Values": [[{"Literal": {"Value": v}}] for v in vals]}}}


def _not_in(s: str, p: str, vals: list[str]) -> dict:
    return {"Condition": {"Not": {"Expression": {"In": {
        "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": s}}, "Property": p}}],
        "Values": [[{"Literal": {"Value": v}}] for v in vals]}}}}}


def _gt(s: str, p: str, lit: str) -> dict:
    return {"Condition": {"Comparison": {"ComparisonKind": 1,
            "Left": {"Column": {"Expression": {"SourceRef": {"Source": s}}, "Property": p}},
            "Right": {"Literal": {"Value": lit}}}}}


def _decode(data: dict) -> list[dict]:
    """Expand Power BI's DSR (delta-compressed or object form) into row dicts."""
    ds_list = ((data or {}).get("dsr") or {}).get("DS") or []
    if not ds_list:
        return []
    ds = ds_list[0]
    select = ((data or {}).get("descriptor") or {}).get("Select") or []
    names = [s.get("Name") for s in select]
    ph = ds.get("PH") or []
    if not ph:
        return []
    dm = ph[0].get("DM0") or []
    if not dm:
        return []
    dicts = ds.get("ValueDicts") or {}
    schema = dm[0].get("S") or []
    order = []
    for s in schema:
        idx = next((i for i, d in enumerate(select) if d.get("Value") == s.get("N")), -1)
        order.append(idx if idx >= 0 else 0)

    rows: list[dict] = []
    prev: list[Any] = [None] * len(schema)
    for r in dm:
        c = r.get("C") or []
        reuse = r.get("R") or 0
        nulls = r.get("Ø") or 0
        out: list[Any] = [None] * len(schema)
        ci = 0
        for i in range(len(schema)):
            bit = 1 << i
            key = schema[i].get("N")
            if nulls & bit:
                v = None
            elif reuse & bit:
                v = prev[i]
            elif key in r:
                v = r[key]
            else:
                v = c[ci] if ci < len(c) else None
                ci += 1
            dn = schema[i].get("DN")
            if v is not None and dn and dn in dicts and isinstance(v, int) and not isinstance(v, bool):
                dictionary = dicts[dn]
                if 0 <= v < len(dictionary):
                    v = dictionary[v]
            out[i] = v
        prev = out
        rows.append({names[order[i]]: out[i] for i in range(len(schema))})
    return rows


# ---------------------------------------------------------------------------
# QES query execution
# ---------------------------------------------------------------------------
# The capacity caps every response at 30,000 rows regardless of the window we
# ask for. Truncation is signalled by IC=false plus an RT restart token, which
# we replay to fetch the next page.
_MAX_WINDOW = 30000
_MAX_PAGES = 200


def _qes_post_page(session: requests.Session, query: dict,
                   restart: Optional[list] = None,
                   retries: int = 2) -> tuple[list[dict], Optional[list]]:
    """POST one page. Returns (rows, restart_token_for_next_page_or_None)."""
    mwc = _mint_mwc(session)
    window: dict[str, Any] = {"Count": _MAX_WINDOW}
    if restart:
        window["RestartTokens"] = restart
    body = {
        "version": "1.0.0",
        "queries": [{
            "Query": {"Commands": [{"SemanticQueryDataShapeCommand": {
                "Query": query,
                "Binding": {
                    "Primary": {"Groupings": [{"Projections": list(range(len(query["Select"])))}]},
                    "DataReduction": {"DataVolume": 3, "Primary": {"Window": window}},
                    "Version": 1,
                },
                "ExecutionMetricsKind": 1,
            }}]},
            "QueryId": "",
            "ApplicationContext": {"DatasetId": _DATASET_ID,
                                   "Sources": [{"ReportId": _REPORT_ID, "VisualId": _VISUAL_ID}]},
        }],
        "cancelQueries": [], "modelId": _model_id,
        "userPreferredLocale": "en-US", "allowLongRunningQueries": True,
    }
    rid = str(uuid.uuid4())
    headers = {
        "authorization": f"MWCToken {mwc}",
        "content-type": "application/json;charset=UTF-8",
        "activityid": str(uuid.uuid4()), "requestid": rid,
        "x-ms-parent-activity-id": rid, "x-ms-root-activity-id": rid,
        "x-ms-workload-resource-moniker": _DATASET_ID,
        "origin": "https://msit.powerbi.com", "referer": "https://msit.powerbi.com/",
    }
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            resp = session.post(_qes_url, headers=headers, data=json.dumps(body), timeout=(15, 300))
            if resp.status_code == 401:
                clear_token_cache()  # token expired mid-run - re-mint and retry
                headers["authorization"] = f"MWCToken {_mint_mwc(session)}"
                raise RevenuePullError("QES 401 (token refreshed)")
            if not resp.ok:
                raise RevenuePullError(f"QES {resp.status_code}: {resp.text[:200]}")
            res = json.loads(resp.text.lstrip("\ufeff"))["results"][0]["result"]
            if res.get("error"):
                raise RevenuePullError("QES error: " + json.dumps(res["error"])[:200])
            data = res.get("data") or {}
            ds = ((data.get("dsr") or {}).get("DS") or [{}])[0]
            rows = _decode(data)
            # IC (IsComplete) false means more rows exist; RT is the cursor.
            next_rt = ds.get("RT") if ds.get("IC") is False else None
            if ds.get("IC") is False and not next_rt:
                raise RevenuePullError(
                    "QES truncated the result but returned no restart token; "
                    "refusing to use a partial dataset."
                )
            return rows, next_rt
        except Exception as exc:  # noqa: BLE001 - retry with backoff
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise last  # type: ignore[misc]


def _qes_post(session: requests.Session, query: dict, retries: int = 2) -> list[dict]:
    """Run a query to completion, following restart tokens across pages.

    Never returns a partial dataset: a truncated response either paginates or
    raises, so callers can trust that what they get back is everything.
    """
    out: list[dict] = []
    restart: Optional[list] = None
    for _ in range(_MAX_PAGES):
        rows, restart = _qes_post_page(session, query, restart=restart, retries=retries)
        out.extend(rows)
        if not restart:
            return out
    raise RevenuePullError(
        f"QES pagination exceeded {_MAX_PAGES} pages ({len(out)} rows); aborting."
    )


# ---------------------------------------------------------------------------
# Public pull
# ---------------------------------------------------------------------------
def _current_fy() -> int:
    t = date.today()
    return t.year + 1 if t.month >= 7 else t.year


def default_fiscal_years() -> list[str]:
    """The fiscal years worth pulling: current plus the two prior.

    Measured against the live model, this yields 25 months (two full fiscal years
    plus the current partial one). Reaching further back returns nothing, so this
    is the practical retention floor.
    """
    fy = _current_fy()
    return [f"FY{(fy - n) % 100:02d}" for n in (2, 1, 0)]


# Filter context the report applies. The parameter selections are what make the
# `$ ACR` measure resolve at all, so they are not optional.
#
# Note there is deliberately no FYRel filter: it is a report-level convenience
# that clamps results to FY-1..FY+1 and would cut our history from 25 months to 13.
def _acr_from() -> list[dict]:
    return [
        {"Name": "d", "Entity": "DimDate", "Type": 0},
        {"Name": "d1", "Entity": "DimCustomer", "Type": 0},
        {"Name": "f", "Entity": "Fact ACR Subscription", "Type": 0},
        {"Name": "m", "Entity": "Measures | ACR", "Type": 0},
        {"Name": "p", "Entity": "Parameter | ACR Attributes", "Type": 0},
        {"Name": "p1", "Entity": "Parameter | ACR Measures", "Type": 0},
    ]


def _acr_where(tpids: list[int], fiscal_years: list[str], attribute: str) -> list[dict]:
    return [
        _in("d", "FiscalYear", [f"'{fy}'" for fy in fiscal_years]),
        _in("f", "AdjustmentFlag", ["'N/A'"]),
        _in("p", "Parameter | ACR Attributes", [f"'{attribute}'"]),
        _in("p1", "Parameter | ACR Measures Fields", ["'''Measures | ACR''[$ ACR]'"]),
        _not_in("d1", "HQDS", ["'DS'"]),
        _gt("d1", "TPID", "0L"),
        _in("d1", "TPID", [f"{t}L" for t in tpids]),
    ]


def _acr_query(tpids: list[int], fiscal_years: list[str]) -> dict:
    """Customer x bucket x fiscal-month ACR, scoped to the given TPIDs."""
    return {
        "Version": 2,
        "From": _acr_from(),
        "Select": [
            _col("d1", "TPID", "tpid"), _col("d1", "TPAccountName", "name"),
            _col("d", "FiscalMonth", "fm"), _col("f", "ServiceCompGrouping", "bucket"),
            _mea("m", "$ ACR", "acr"),
        ],
        "Where": _acr_where(tpids, fiscal_years, "ServiceCompGrouping"),
    }


def _product_query(tpids: list[int], fiscal_years: list[str]) -> dict:
    """Customer x bucket x product x fiscal-month ACR (the ServiceLevel4 grain)."""
    return {
        "Version": 2,
        "From": _acr_from(),
        "Select": [
            _col("d1", "TPID", "tpid"), _col("d1", "TPAccountName", "name"),
            _col("d", "FiscalMonth", "fm"), _col("f", "ServiceCompGrouping", "bucket"),
            _col("f", "ServiceLevel4", "product"), _mea("m", "$ ACR", "acr"),
        ],
        "Where": _acr_where(tpids, fiscal_years, "ServiceLevel4"),
    }


def pull_acr_for_customers(tpids: list[int], fiscal_years: Optional[list[str]] = None,
                           chunk: int = 40, max_workers: int = 4) -> list[dict]:
    """Pull ACR (customer x bucket x fiscal-month) for the given customer TPIDs.

    Returns rows with keys: tpid, name, fm, bucket, acr.
    """
    return _pull(_acr_query, tpids, fiscal_years, chunk, max_workers)


def pull_products_for_customers(tpids: list[int], fiscal_years: Optional[list[str]] = None,
                                chunk: int = 25, max_workers: int = 4,
                                progress=None) -> list[dict]:
    """Pull ACR at product grain (customer x bucket x product x fiscal-month).

    Chunks smaller than the bucket-grain pull because this grain is roughly
    seven times denser and would otherwise page repeatedly against the 30k cap.

    Returns rows with keys: tpid, name, fm, bucket, product, acr.
    """
    return _pull(_product_query, tpids, fiscal_years, chunk, max_workers, progress)


def _pull(query_builder, tpids: list[int], fiscal_years: Optional[list[str]],
          chunk: int, max_workers: int, progress=None) -> list[dict]:
    if not tpids:
        return []
    fys = fiscal_years or default_fiscal_years()
    session = requests.Session()
    session.trust_env = False  # ignore env proxy that stalls the capacity handshake
    _mint_mwc(session)  # mint once up front so chunks reuse it

    parts = [tpids[i:i + chunk] for i in range(0, len(tpids), chunk)]

    def run(part: list[int]) -> list[dict]:
        return _qes_post(session, query_builder(part, fys))

    out: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for rows in ex.map(run, parts):
            out.extend(rows)
            done += 1
            if progress:
                progress(done, len(parts), len(out))
    return out


def get_customer_tpids(limit: Optional[int] = None) -> list[tuple[int, str]]:
    """Return (tpid, name) for customers in the configured DB (numeric, deduped)."""
    from app.models import Customer

    q = Customer.query.with_entities(Customer.tpid, Customer.name).filter(Customer.tpid.isnot(None))
    seen: list[tuple[int, str]] = []
    dedupe: set[int] = set()
    for tpid, name in q.all():
        try:
            n = int(tpid)
        except (TypeError, ValueError):
            continue
        if n > 0 and n not in dedupe:
            dedupe.add(n)
            seen.append((n, name))
            if limit and len(seen) >= limit:
                break
    return seen
