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

Results are read-only. Nothing here writes to the database - the beta test page
renders and audits the findings so coverage can be validated before this feeds
the import pipeline.
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
def _qes_post(session: requests.Session, query: dict, retries: int = 2) -> list[dict]:
    mwc = _mint_mwc(session)
    body = {
        "version": "1.0.0",
        "queries": [{
            "Query": {"Commands": [{"SemanticQueryDataShapeCommand": {
                "Query": query,
                "Binding": {
                    "Primary": {"Groupings": [{"Projections": list(range(len(query["Select"])))}]},
                    "DataReduction": {"DataVolume": 3, "Primary": {"Window": {"Count": 30000}}},
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
            resp = session.post(_qes_url, headers=headers, data=json.dumps(body), timeout=(15, 240))
            if resp.status_code == 401:
                clear_token_cache()  # token expired mid-run - re-mint and retry
                headers["authorization"] = f"MWCToken {_mint_mwc(session)}"
                raise RevenuePullError("QES 401 (token refreshed)")
            if not resp.ok:
                raise RevenuePullError(f"QES {resp.status_code}: {resp.text[:200]}")
            res = json.loads(resp.text.lstrip("\ufeff"))["results"][0]["result"]
            if res.get("error"):
                raise RevenuePullError("QES error: " + json.dumps(res["error"])[:200])
            return _decode(res.get("data") or {})
        except Exception as exc:  # noqa: BLE001 - retry with backoff
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise last  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Public pull
# ---------------------------------------------------------------------------
def _current_fy() -> int:
    t = date.today()
    return t.year + 1 if t.month >= 7 else t.year


def default_fiscal_years() -> list[str]:
    """Current fiscal year plus the prior one (Microsoft FY starts July)."""
    fy = _current_fy()
    return [f"FY{(fy - 1) % 100:02d}", f"FY{fy % 100:02d}"]


def _acr_query(tpids: list[int], fiscal_years: list[str]) -> dict:
    """Customer x bucket x fiscal-month ACR, scoped to the given TPIDs.

    Mirrors the report's required filter context so the parameter-driven
    ``$ ACR`` measure resolves.
    """
    return {
        "Version": 2,
        "From": [
            {"Name": "d", "Entity": "DimDate", "Type": 0},
            {"Name": "d1", "Entity": "DimCustomer", "Type": 0},
            {"Name": "f", "Entity": "Fact ACR Subscription", "Type": 0},
            {"Name": "m", "Entity": "Measures | ACR", "Type": 0},
            {"Name": "p", "Entity": "Parameter | ACR Attributes", "Type": 0},
            {"Name": "p1", "Entity": "Parameter | ACR Measures", "Type": 0},
        ],
        "Select": [
            _col("d1", "TPID", "tpid"), _col("d1", "TPAccountName", "name"),
            _col("d", "FiscalMonth", "fm"), _col("f", "ServiceCompGrouping", "bucket"),
            _mea("m", "$ ACR", "acr"),
        ],
        "Where": [
            _in("d", "FiscalYear", [f"'{fy}'" for fy in fiscal_years]),
            _in("f", "AdjustmentFlag", ["'N/A'"]),
            _in("p", "Parameter | ACR Attributes", ["'ServiceCompGrouping'"]),
            _in("p1", "Parameter | ACR Measures Fields", ["'''Measures | ACR''[$ ACR]'"]),
            _not_in("d1", "HQDS", ["'DS'"]),
            _in("d", "FYRel", ["'FY'", "'FY+1'", "'FY-1'"]),
            _gt("d1", "TPID", "0L"),
            _in("d1", "TPID", [f"{t}L" for t in tpids]),
        ],
    }


def pull_acr_for_customers(tpids: list[int], fiscal_years: Optional[list[str]] = None,
                           chunk: int = 40, max_workers: int = 4) -> list[dict]:
    """Pull ACR (customer x bucket x fiscal-month) for the given customer TPIDs.

    Returns rows with keys: tpid, name, fm, bucket, acr.
    """
    if not tpids:
        return []
    fys = fiscal_years or default_fiscal_years()
    session = requests.Session()
    session.trust_env = False  # ignore env proxy that stalls the capacity handshake
    _mint_mwc(session)  # mint once up front so chunks reuse it

    parts = [tpids[i:i + chunk] for i in range(0, len(tpids), chunk)]

    def run(part: list[int]) -> list[dict]:
        return _qes_post(session, _acr_query(part, fys))

    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for rows in ex.map(run, parts):
            out.extend(rows)
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


# ---------------------------------------------------------------------------
# Audit summary for the beta test page
# ---------------------------------------------------------------------------
def build_audit(rows: list[dict], customers: list[tuple[int, str]]) -> dict:
    """Summarize a pull: coverage stats + a per-customer breakdown to eyeball."""
    names = {t: n for t, n in customers}
    total = len(customers)

    per: dict[int, dict] = {}
    months: set[str] = set()
    buckets: set[str] = set()
    grand_total = 0.0
    for r in rows:
        try:
            tpid = int(r.get("tpid"))
        except (TypeError, ValueError):
            continue
        acr = float(r.get("acr") or 0.0)
        bucket = (r.get("bucket") or "").strip()
        fm = str(r.get("fm") or "").strip()
        if fm:
            months.add(fm)
        if bucket:
            buckets.add(bucket)
        grand_total += acr
        c = per.setdefault(tpid, {"tpid": tpid, "name": r.get("name") or names.get(tpid),
                                  "total_acr": 0.0, "buckets": set(), "months": set()})
        c["total_acr"] += acr
        if bucket:
            c["buckets"].add(bucket)
        if fm:
            c["months"].add(fm)

    with_data = [c for c in per.values() if abs(c["total_acr"]) > 0.005]
    with_data_ids = {c["tpid"] for c in with_data}
    without = [{"tpid": t, "name": n} for t, n in customers if t not in with_data_ids]

    per_customer = sorted(
        ({"tpid": c["tpid"], "name": c["name"], "total_acr": round(c["total_acr"], 2),
          "bucket_count": len(c["buckets"]), "month_count": len(c["months"])}
         for c in with_data),
        key=lambda x: x["total_acr"], reverse=True,
    )

    return {
        "total_customers": total,
        "customers_with_data": len(with_data),
        "customers_without_data": total - len(with_data),
        "coverage_pct": round(100 * len(with_data) / total, 1) if total else 0.0,
        "total_acr": round(grand_total, 2),
        "row_count": len(rows),
        "distinct_months": sorted(months),
        "distinct_buckets": sorted(buckets),
        "per_customer": per_customer,
        "without_data": without[:200],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
