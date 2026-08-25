# Get Customer V-Team and Seller Details from MSX

This guide shows how to retrieve a customer's current seller and core solution
engineers from the native MSX account access team.

**Last verified:** August 24, 2026
**API:** `https://microsoftsales.crm.dynamics.com/api/data/v9.2`
**Tenant:** `72f988bf-86f1-41af-91ab-2d7cd011db47`
**Account access team template:** `3fcc1cfc-3e43-e311-9405-00155db3ba1e`

## Source of Truth

Use the native Dynamics account access team:

```text
account
  -> team
    -> teammembership
      -> systemuser
```

Do not use `msp_accountteams`, account owner, or territory owner. Those sources
can contain stale assignments, overlay roles, account executives, or managers.

The input is the customer's MSX `accountid` GUID. If starting from a TPID,
resolve it first:

```http
GET /accounts?$select=accountid,name,msp_mstopparentid
    &$filter=msp_mstopparentid eq '{tpid}'
```

## Authentication

MSX requires a token for the Dynamics resource in the Microsoft corporate
tenant. VPN access may also be required.

```powershell
az login --tenant 72f988bf-86f1-41af-91ab-2d7cd011db47

$token = az account get-access-token `
  --resource https://microsoftsales.crm.dynamics.com `
  --tenant 72f988bf-86f1-41af-91ab-2d7cd011db47 `
  --query accessToken `
  --output tsv
```

Send these headers:

```http
Authorization: Bearer {token}
Accept: application/json
OData-MaxVersion: 4.0
OData-Version: 4.0
```

## V-Team Query

Use FetchXML against `/teams`. It selects the account's access team, joins its
membership to current user records, and returns identity plus role metadata.

```xml
<fetch distinct="true">
  <entity name="team">
    <attribute name="teamid" />
    <attribute name="regardingobjectid" />
    <filter type="and">
      <condition attribute="teamtype" operator="eq" value="1" />
      <condition attribute="teamtemplateid" operator="eq"
                 value="3fcc1cfc-3e43-e311-9405-00155db3ba1e" />
      <condition attribute="regardingobjectid" operator="eq"
                 value="{account_guid}" />
    </filter>
    <link-entity name="teammembership"
                 from="teamid"
                 to="teamid"
                 intersect="true">
      <link-entity name="systemuser"
                   from="systemuserid"
                   to="systemuserid"
                   alias="member">
        <attribute name="systemuserid" />
        <attribute name="fullname" />
        <attribute name="internalemailaddress" />
        <attribute name="domainname" />
        <attribute name="title" />
        <attribute name="msp_qualifier1" />
        <attribute name="msp_qualifier2" />
        <attribute name="isdisabled" />
        <filter type="and">
          <condition attribute="isdisabled" operator="eq" value="0" />
          <condition attribute="msp_qualifier1" operator="eq"
                     value="Corporate" />
          <filter type="or">
            <filter type="and">
              <condition attribute="title" operator="like"
                         value="%Digital Specialist%" />
              <condition attribute="title" operator="not-like"
                         value="%Dir%" />
              <condition attribute="title" operator="not-like"
                         value="%Manager%" />
              <condition attribute="msp_qualifier2" operator="in">
                <value>Cloud &amp; AI</value>
                <value>Cloud &amp; AI-Acq</value>
              </condition>
            </filter>
            <filter type="and">
              <condition attribute="title" operator="like"
                         value="%Sol Engineer%" />
              <condition attribute="msp_qualifier2" operator="in">
                <value>Cloud &amp; AI Data</value>
                <value>Cloud &amp; AI Infrastructure</value>
                <value>Cloud &amp; AI Apps</value>
              </condition>
            </filter>
          </filter>
        </filter>
      </link-entity>
    </link-entity>
  </entity>
</fetch>
```

URL-encode the XML as the `fetchXml` query parameter:

```http
GET https://microsoftsales.crm.dynamics.com/api/data/v9.2/teams?fetchXml={encoded_fetchxml}
```

Follow `@odata.nextLink` until no next page remains.

## Classify Results

FetchXML aliases linked-user fields with `member.`, for example
`member.fullname` and `member.msp_qualifier2`.

Seller rules:

| Field | Rule |
|---|---|
| `member.isdisabled` | Must be false |
| `member.msp_qualifier1` | Must be `Corporate` |
| `member.title` | Contains `Digital Specialist` |
| `member.title` | Excludes `Dir` and `Manager` |
| `member.msp_qualifier2` | `Cloud & AI` = Growth seller |
| `member.msp_qualifier2` | `Cloud & AI-Acq` = Acquisition seller |

Core solution engineer rules:

| `member.msp_qualifier2` | Classification |
|---|---|
| `Cloud & AI Data` | Data SE |
| `Cloud & AI Infrastructure` | Infrastructure SE |
| `Cloud & AI Apps` | Apps SE |

SE titles must contain `Sol Engineer`. Extract email alias from
`internalemailaddress`, falling back to `domainname`:

```python
email = row.get("member.internalemailaddress") or row.get("member.domainname") or ""
alias = email.split("@", 1)[0] if email else None
```

Expected normalized result:

```json
{
  "seller": {
    "name": "Seller Name",
    "user_id": "systemuser-guid",
    "alias": "selleralias",
    "type": "Growth"
  },
  "solution_engineers": {
    "data": [],
    "infrastructure": [],
    "apps": []
  }
}
```

## Important Behavior

- Exactly one valid seller candidate: assign that seller.
- No valid seller: return no assignment. Do not infer one from ownership.
- Multiple valid sellers: return an ambiguous result. Do not select by response
  order.
- Deduplicate team members by `systemuserid`.
- Treat title comparisons case-insensitively when validating client-side.
- Use exact access-team template ID. Users also belong to opportunity,
  milestone, and account-plan access teams.
- A `403` containing `0x80095ffe` or `IP address is blocked` usually means VPN
  or approved network access is missing.

## Sales Buddy Implementation

Production implementation is `batch_query_account_teams()` in
`app/services/msx_api.py`. Focused behavior tests are in
`tests/test_native_account_vteam.py`.