# Marketing Insights from MSX

## Goal

Surface Microsoft Marketing Insights data on customer views so sellers can see which accounts have active marketing engagement (Contact Me requests, trial signups, content downloads, event attendance) and which contacts at those accounts are most engaged - without leaving Sales Buddy.

## What the MSX UI Shows

The Marketing Insights panel in MSX Dynamics 365 displays:

1. **Account-level summary** - aggregate counts across all solution areas:
   - Contact Me requests
   - Trial Signups
   - Content Downloads
   - Events attended
   - High-interaction contacts

2. **Breakdown by Solution Area / Sales Play** - one row per combination showing individual counts and last interaction date

3. **Contact-level detail** - individual contacts at the account with:
   - Email interaction count
   - Meeting interaction count
   - Marketing audience type (e.g. "Business Development User", "IT Decision Maker")
   - Engagement level (e.g. "Engaged", "Not Engaged")
   - Last marketing interaction date
   - Last solution area engaged

## MSX API Data Sources

### 1. Account-Level: `msp_marketinginteractions`

**Entity set:** `msp_marketinginteractions`

**Composite key:** `msp_tpidsolutionareasalesplay` (format: `{tpid}_{solutionareacode}_{salesplaycode}`)

One row per TPID + Solution Area + Sales Play combination. Aggregate to get account totals.

**Query pattern:**
```
GET /api/data/v9.2/msp_marketinginteractions
  ?$select=msp_tpid,msp_salesplaycode,msp_solutionareacode,
           msp_allinteractions,msp_contacts,msp_trialsignups,
           msp_contentdownloads,msp_events,msp_uniquedecisionmakers,
           msp_highinteractioncontacts,msp_highinteractioncount,
           msp_highinteractionuniquedecisionmakers,
           msp_lastinteractiondate,msp_lasthighinteractiondate
  &$filter=msp_tpid eq '{tpid}'
  &$orderby=msp_allinteractions desc
```

**Key fields:**

| Field | Type | Description |
|---|---|---|
| `msp_tpid` | String | Top Parent ID (links to account) |
| `msp_salesplaycode` | Picklist | Sales Play (use FormattedValue for label) |
| `msp_solutionareacode` | Picklist | Solution Area (use FormattedValue for label) |
| `msp_allinteractions` | Integer | Total interaction count |
| `msp_contacts` | Integer | "Contact Me" request count |
| `msp_trialsignups` | Integer | Trial signup count |
| `msp_contentdownloads` | Integer | Content download count |
| `msp_events` | Integer | Event attendance count |
| `msp_uniquedecisionmakers` | Integer | Unique decision makers who interacted |
| `msp_highinteractioncontacts` | Integer | Contacts with high interaction level |
| `msp_highinteractioncount` | Integer | Total high interactions |
| `msp_highinteractionuniquedecisionmakers` | Integer | Unique DMs with high interaction |
| `msp_lastinteractiondate` | DateTime | Last interaction timestamp |
| `msp_lasthighinteractiondate` | DateTime | Last high-interaction timestamp |

**Sample response (one row):**
```json
{
  "msp_tpid": "19931068",
  "msp_solutionareacode": 861980005,
  "msp_solutionareacode@OData.Community.Display.V1.FormattedValue": "Security",
  "msp_salesplaycode": 861980027,
  "msp_salesplaycode@OData.Community.Display.V1.FormattedValue": "Data Security",
  "msp_allinteractions": 1,
  "msp_contacts": 1,
  "msp_trialsignups": 0,
  "msp_contentdownloads": 1,
  "msp_events": 0,
  "msp_uniquedecisionmakers": 1,
  "msp_highinteractioncount": 0,
  "msp_lastinteractiondate": "2024-11-22T16:26:46Z"
}
```

### 2. Contact-Level: `contacts` entity (marketing fields)

No separate contact-interaction entity exists. Marketing data lives on the standard `contact` entity as `msp_*` fields.

**Query requires two steps:**
1. Look up account GUID from TPID: `GET /accounts?$filter=msp_mstopparentid eq '{tpid}'`
2. Query contacts: `GET /contacts?$filter=_parentcustomerid_value eq {account_guid}`

**Query pattern (step 2):**
```
GET /api/data/v9.2/contacts
  ?$select=fullname,emailaddress1,jobtitle,
           msp_lastmarketinginteractiondate,msp_noofmailinteractions,
           msp_noofmeetinginteractions,msp_marketingaudiencecode,
           msp_salesengagementlevel,msp_lastsolutionareaengaged
  &$filter=_parentcustomerid_value eq {account_guid}
  &$orderby=msp_noofmailinteractions desc
  &$top=20
```

**Note:** Filter on `_parentcustomerid_value` (the lookup field's raw GUID), NOT `parentcustomerid` (which errors).

**Key fields on contact:**

| Field | Type | Description |
|---|---|---|
| `msp_noofmailinteractions` | Integer | Email interaction count |
| `msp_noofmeetinginteractions` | Integer | Meeting interaction count |
| `msp_marketingaudiencecode` | Picklist | Audience type (e.g. "Business Development User", "IT Decision Maker") |
| `msp_salesengagementlevel` | Picklist | Engagement level (e.g. "Engaged") |
| `msp_lastmarketinginteractiondate` | DateTime | Last marketing interaction date |
| `msp_lastsolutionareaengaged` | Picklist | Last solution area engaged (e.g. "Cloud and AI Platforms") |

**Sample response (one contact):**
```json
{
  "fullname": "Bob Beatty",
  "jobtitle": "Chief Architect",
  "emailaddress1": "bbeatty@3arrows.us",
  "msp_noofmailinteractions": 3,
  "msp_noofmeetinginteractions": 0,
  "msp_marketingaudiencecode@OData.Community.Display.V1.FormattedValue": "Business Development User",
  "msp_salesengagementlevel@OData.Community.Display.V1.FormattedValue": "Engaged",
  "msp_lastmarketinginteractiondate": "2024-07-17T13:43:48Z",
  "msp_lastsolutionareaengaged@OData.Community.Display.V1.FormattedValue": "Cloud and AI Platforms"
}
```

## Implementation Plan

### Phase 1: API Layer

Add to `app/services/msx_api.py`:

- `get_marketing_insights(tpid: str) -> dict` - Fetches `msp_marketinginteractions` rows, returns aggregated summary + per-sales-play breakdown
- `get_marketing_contacts(tpid: str) -> list[dict]` - Two-step query (account lookup then contacts with marketing fields), returns list of contacts with engagement data

Both functions reuse the existing `_make_crm_request()` helper and auth pattern.

### Phase 2: Route + API Endpoint

Add to `app/routes/customers.py` (or `msx.py`):

- `GET /api/customer/<id>/marketing-insights` - Returns JSON with account-level summary and contact-level details. Looks up TPID from the customer record, calls both API functions.

### Phase 3: Customer View UI

Add a "Marketing Insights" card/section to `templates/customer_view.html`:

**Account summary panel:**
- Four stat boxes: Contact Me | Trials | Content | Events (similar to MSX layout)
- Total interactions badge
- High-interaction contacts count

**Sales Play breakdown table:**
- Columns: Solution Area, Sales Play, Interactions, Contact Me, Trials, Content, Events, Last Date
- Sorted by total interactions descending

**Engaged contacts list:**
- Each contact shows: Name, Title, Email/Meeting interaction counts, Engagement level badge, Last interaction date
- Sorted by total interactions descending
- Only show contacts with at least 1 interaction (filter out zeros)

### Phase 4: Note Form Flyout

Add marketing insights to the customer flyout in `templates/note_form.html`:

- Compact version of the summary (just the four stat boxes)
- Top 3-5 engaged contacts
- Link to full customer view for details

## Design Notes

- **No local caching** - Marketing data changes frequently and is read-only. Fetch live from MSX on each view.
- **Loading pattern** - Fetch async after page load (same pattern as existing MSX detail fetches). Show skeleton/spinner while loading.
- **Empty state** - Many accounts will have zero marketing interactions. Show a friendly "No marketing insights available" message, not an error.
- **API cost** - Account-level query is a single call (typically returns <20 rows). Contact-level requires two calls but is bounded by `$top=20`. Total: 2-3 API calls per view.
- **TPID required** - Marketing insights are keyed by TPID. Customers without a TPID linked can't show this data. Hide the section or show "Link a TPID to see marketing insights."
- **FormattedValue annotations** - Always use the OData `FormattedValue` annotation for picklist fields (solution area, sales play, audience, engagement level). The raw values are opaque integer codes.

## Discovery Scripts

These were used to find and validate the API patterns. Keep for reference:

- `scripts/explore_marketing_insights.py` - Entity discovery (searched EntityDefinitions)
- `scripts/explore_marketing_contacts.py` - Contact entity field discovery
- `scripts/query_marketing_insights.py` - Account-level query CLI tool
- `scripts/query_marketing_contacts.py` - Contact-level query CLI tool
